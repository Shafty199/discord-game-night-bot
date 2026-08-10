import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

from database.database import (
    activate_gaming_session,
    add_game,
    add_gaming_session_member,
    cancel_gaming_session_game,
    clear_gaming_session_selection,
    configure_gaming_session,
    create_gaming_session,
    end_gaming_session,
    finish_gaming_session_game,
    get_active_gaming_session_for_voice,
    get_active_gaming_sessions,
    get_game_night_week,
    get_gaming_session,
    get_session_wheel_game_ids,
    get_spin_games_by_ids,
    mark_game_played,
    remove_gaming_session_member,
    replace_gaming_session_members,
    search_session_games,
    save_gaming_session_game_lock_message,
    select_gaming_session_game,
    start_gaming_session_game,
    transfer_gaming_session_host,
)
from settings import (
    GAME_NIGHT_TIMEZONE,
    GAME_NIGHT_VOICE_CHANNEL_ID,
    SESSION_CHANNEL_ID,
    SESSION_NOTIFY_ROLE_ID,
)
from utils.game_lookup import find_game_by_title
from utils.igdb import enrich_missing_player_metadata
from utils.store import get_game_info_from_url


LOGGER = logging.getLogger(__name__)
SESSION_MAX_PLAYER_COUNT = 100
SESSION_EVENT_EARLY_START = timedelta(hours=2)
SESSION_EVENT_LATE_START = timedelta(hours=6)


def _human_voice_members(
    voice_channel,
) -> list[tuple[int, str]]:
    return [
        (
            int(member.id),
            member.display_name,
        )
        for member in voice_channel.members
        if not member.bot
    ]


def _selected_game_name(
    session: dict,
    game: tuple | None,
) -> str | None:
    if game is not None:
        return str(game[1])

    custom_name = str(
        session.get("custom_game_name") or ""
    ).strip()
    return custom_name or None


def _format_play_duration(
    started_at: str,
    finished_at: str | None,
) -> str:
    start = datetime.fromisoformat(str(started_at))
    finish = (
        datetime.fromisoformat(str(finished_at))
        if finished_at
        else datetime.now(timezone.utc)
    )

    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)

    if finish.tzinfo is None:
        finish = finish.replace(tzinfo=timezone.utc)

    total_minutes = max(
        1,
        round((finish - start).total_seconds() / 60),
    )
    hours, minutes = divmod(total_minutes, 60)

    if hours and minutes:
        return f"{hours}h {minutes}m"

    if hours:
        return f"{hours}h"

    return f"{minutes}m"


def _wheel_library_changed(sync_result: dict) -> bool:
    """Return False when an add-to-wheel request was a duplicate no-op."""

    return sync_result.get("status") not in {
        "unchanged",
        "wishlist_unchanged",
    }


class PlayerCountModal(discord.ui.Modal):
    def __init__(
        self,
        cog,
        session: dict,
    ) -> None:
        super().__init__(
            title="Set Session Player Count",
            custom_id=(
                f"gaming-session:{session['id']}:player-count"
            ),
            timeout=180,
        )
        self.cog = cog
        self.session_id = int(session["id"])
        self.player_count = discord.ui.TextInput(
            label="Player count or 'auto'",
            placeholder="Example: 7 (or auto to use voice members)",
            default=str(session["effective_player_count"]),
            min_length=1,
            max_length=4,
        )
        self.add_item(self.player_count)

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ) -> None:
        raw_value = str(self.player_count.value).strip()

        if raw_value.casefold() == "auto":
            await self.cog.change_player_count(
                interaction,
                self.session_id,
                player_count=None,
            )
            return

        try:
            player_count = int(raw_value)
        except ValueError:
            await interaction.response.send_message(
                "Enter a whole number or `auto`.",
                ephemeral=True,
            )
            return

        if not 1 <= player_count <= SESSION_MAX_PLAYER_COUNT:
            await interaction.response.send_message(
                "Player count must be between 1 and 100.",
                ephemeral=True,
            )
            return

        await self.cog.change_player_count(
            interaction,
            self.session_id,
            player_count=player_count,
        )


class TransferHostView(discord.ui.View):
    def __init__(
        self,
        cog,
        session_id: int,
        requester_id: int,
    ) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.session_id = int(session_id)
        self.requester_id = int(requester_id)

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who opened this menu can use it.",
                ephemeral=True,
            )
            return False

        return await self.cog.can_control_session(
            interaction,
            self.session_id,
        )

    @discord.ui.select(
        cls=discord.ui.UserSelect,
        placeholder="Choose a joined player",
        min_values=1,
        max_values=1,
    )
    async def choose_host(
        self,
        interaction: discord.Interaction,
        select: discord.ui.UserSelect,
    ) -> None:
        await self.cog.transfer_session_host(
            interaction,
            self.session_id,
            select.values[0],
        )


class FinishedGameView(discord.ui.View):
    def __init__(self, cog, session_id: int) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.session_id = int(session_id)
        self.finished.custom_id = (
            f"gaming-session:{self.session_id}:locked-finished"
        )

    @discord.ui.button(
        label="Finished Playing",
        emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="locked-finished",
    )
    async def finished(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.cog.finish_playing(
            interaction,
            self.session_id,
        )


class SessionView(discord.ui.View):
    def __init__(
        self,
        cog,
        session: dict,
    ) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.session_id = int(session["id"])

        for child in self.children:
            if child.custom_id:
                child.custom_id = (
                    f"gaming-session:{self.session_id}:"
                    f"{child.custom_id}"
                )

        self.unverified.label = (
            "Unverified: On"
            if session["include_unverified"]
            else "Unverified: Off"
        )
        self.normal_wheel.label = (
            "Normal Wheel: On"
            if session["use_normal_wheel"]
            else "Normal Wheel: Off"
        )
        selected = bool(
            session.get("selected_game_id")
            or session.get("custom_game_name")
        )
        self.spin.disabled = selected

        if not selected:
            self.remove_item(self.return_to_wheel)
            self.remove_item(self.finished_playing)

        self.add_item(
            discord.ui.Button(
                label="Join Voice",
                emoji="🔊",
                style=discord.ButtonStyle.link,
                row=0,
                url=(
                    "https://discord.com/channels/"
                    f"{session['guild_id']}/"
                    f"{session['voice_channel_id']}"
                ),
            )
        )

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        session = await get_gaming_session(
            self.session_id
        )

        if (
            session is None
            or session["status"] != "active"
        ):
            await interaction.response.send_message(
                "This gaming session is no longer active.",
                ephemeral=True,
            )
            return False

        return True

    @discord.ui.button(
        label="Join Session",
        emoji="➕",
        style=discord.ButtonStyle.success,
        row=0,
        custom_id="join",
    )
    async def join(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.cog.join_session(
            interaction,
            self.session_id,
        )

    @discord.ui.button(
        label="Leave Session",
        emoji="➖",
        style=discord.ButtonStyle.secondary,
        row=0,
        custom_id="leave",
    )
    async def leave(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.cog.leave_session(
            interaction,
            self.session_id,
        )

    @discord.ui.button(
        label="Spin Wheel",
        emoji="🎡",
        style=discord.ButtonStyle.primary,
        row=0,
        custom_id="spin",
    )
    async def spin(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.cog.spin_session(
            interaction,
            self.session_id,
        )

    @discord.ui.button(
        label="Refresh Voice",
        emoji="🔄",
        style=discord.ButtonStyle.secondary,
        row=0,
        custom_id="refresh",
    )
    async def refresh(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.cog.refresh_voice_members(
            interaction,
            self.session_id,
        )

    @discord.ui.button(
        label="Set Count",
        emoji="👥",
        style=discord.ButtonStyle.secondary,
        row=1,
        custom_id="count",
    )
    async def set_count(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not await self.cog.can_control_session(
            interaction,
            self.session_id,
        ):
            return

        session = await get_gaming_session(
            self.session_id
        )
        await interaction.response.send_modal(
            PlayerCountModal(self.cog, session)
        )

    @discord.ui.button(
        label="Unverified: Off",
        style=discord.ButtonStyle.secondary,
        row=1,
        custom_id="unverified",
    )
    async def unverified(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.cog.toggle_unverified(
            interaction,
            self.session_id,
        )

    @discord.ui.button(
        label="Normal Wheel: Off",
        style=discord.ButtonStyle.secondary,
        row=1,
        custom_id="normal-wheel",
    )
    async def normal_wheel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.cog.toggle_normal_wheel(
            interaction,
            self.session_id,
        )

    @discord.ui.button(
        label="Return to Wheel",
        emoji="🔄",
        style=discord.ButtonStyle.secondary,
        row=1,
        custom_id="return-to-wheel",
    )
    async def return_to_wheel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.cog.rebuild_or_return_to_wheel(
            interaction,
            self.session_id,
        )

    @discord.ui.button(
        label="Finished Playing",
        emoji="✅",
        style=discord.ButtonStyle.success,
        row=2,
        custom_id="finished-playing",
    )
    async def finished_playing(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.cog.finish_playing(
            interaction,
            self.session_id,
        )

    @discord.ui.button(
        label="Transfer Host",
        emoji="👑",
        style=discord.ButtonStyle.secondary,
        row=2,
        custom_id="transfer-host",
    )
    async def transfer_host(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not await self.cog.can_control_session(
            interaction,
            self.session_id,
        ):
            return

        await interaction.response.send_message(
            "Choose a player who has joined this session.",
            view=TransferHostView(
                self.cog,
                self.session_id,
                interaction.user.id,
            ),
            ephemeral=True,
        )

    @discord.ui.button(
        label="End Session",
        emoji="⏹️",
        style=discord.ButtonStyle.danger,
        row=1,
        custom_id="end",
    )
    async def end(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.cog.end_session_from_button(
            interaction,
            self.session_id,
        )


class SessionCacheView(discord.ui.View):
    def __init__(
        self,
        cog,
        session: dict,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.session_id = int(session["id"])
        selected = bool(
            session.get("selected_game_id")
            or session.get("custom_game_name")
        )
        self.rebuild.label = (
            "Return to Wheel"
            if selected
            else "Rebuild Cache"
        )

    @discord.ui.button(
        label="Rebuild Cache",
        emoji="🧹",
        style=discord.ButtonStyle.secondary,
    )
    async def rebuild(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not await self.cog.can_manage_session_cache(
            interaction
        ):
            return

        await self.cog.rebuild_or_return_to_wheel(
            interaction,
            self.session_id,
        )


class ManualGameSelectionView(discord.ui.View):
    def __init__(
        self,
        cog,
        session_id: int,
        game: tuple,
        author_id: int,
    ) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.session_id = int(session_id)
        self.game = game
        self.author_id = int(author_id)

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the host who opened this selection can confirm it.",
                ephemeral=True,
            )
            return False

        return await self.cog.can_control_session(
            interaction,
            self.session_id,
        )

    @discord.ui.button(
        label="Lock It In",
        emoji="✅",
        style=discord.ButtonStyle.success,
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        manager = getattr(
            interaction.client,
            "prepared_spin_manager",
            None,
        )

        if manager is not None:
            await manager.invalidate_game(self.game[0])

        await self.cog.lock_existing_game(
            interaction,
            self.session_id,
            self.game,
        )
        await interaction.edit_original_response(
            content=(
                f"✅ **{self.game[1]}** is locked in for the session."
            ),
            embed=None,
            view=None,
        )

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.secondary,
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.edit_message(
            content="Manual selection cancelled.",
            embed=None,
            view=None,
        )


class CustomGameSelectionView(discord.ui.View):
    def __init__(
        self,
        cog,
        session_id: int,
        game_info: dict,
        author_id: int,
    ) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.session_id = int(session_id)
        self.game_info = dict(game_info)
        self.author_id = int(author_id)

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the host who opened this selection can confirm it.",
                ephemeral=True,
            )
            return False

        return await self.cog.can_control_session(
            interaction,
            self.session_id,
        )

    @discord.ui.button(
        label="Use This Session Only",
        emoji="🎮",
        style=discord.ButtonStyle.success,
    )
    async def session_only(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.cog.lock_custom_game(
            interaction,
            self.session_id,
            self.game_info,
        )
        await interaction.edit_original_response(
            content=(
                f"✅ **{self.game_info['name']}** is locked in "
                "for this session only."
            ),
            embed=None,
            view=None,
        )

    @discord.ui.button(
        label="Add to Wheel and Use",
        emoji="➕",
        style=discord.ButtonStyle.primary,
    )
    async def add_and_use(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        game_info = self.game_info
        sync_result = await add_game(
            name=game_info["name"],
            store_link=game_info.get("store_link") or "",
            store=game_info.get("store") or "Custom",
            suggested_by=interaction.user.display_name,
            image_url=game_info.get("image_url"),
            source_link=game_info.get("source_link"),
            external_id=game_info.get("external_id"),
            link_status=game_info.get("link_status", "unknown"),
            http_status=game_info.get("http_status"),
            availability_status=game_info.get(
                "availability_status",
                "released",
            ),
            release_date=game_info.get("release_date"),
            coming_soon=game_info.get("coming_soon", False),
            max_players=game_info.get("max_players"),
            max_players_source=game_info.get("max_players_source"),
            igdb_id=game_info.get("igdb_id"),
            multiplayer_support=game_info.get("multiplayer_support"),
            genres=game_info.get("genres"),
            themes=game_info.get("themes"),
            game_modes=game_info.get("game_modes"),
            return_details=True,
        )
        record = sync_result.get("record")

        if record is None:
            await interaction.edit_original_response(
                content=(
                    "I couldn't add that game to the permanent wheel. "
                    "You can still choose **Use This Session Only**."
                ),
                view=self,
            )
            return

        games = await get_spin_games_by_ids(
            [record["id"]]
        )
        game = games.get(int(record["id"]))

        if game is None:
            await interaction.edit_original_response(
                content=(
                    "The game was saved, but it is not currently eligible "
                    "for the multiplayer wheel."
                ),
                view=None,
            )
            return

        manager = getattr(
            interaction.client,
            "prepared_spin_manager",
            None,
        )

        wheel_changed = _wheel_library_changed(sync_result)

        if manager is not None and wheel_changed:
            await manager.invalidate_library()

        await self.cog.lock_existing_game(
            interaction,
            self.session_id,
            game,
        )
        await interaction.edit_original_response(
            content=(
                (
                    f"✅ **{game[1]}** was added to the wheel and locked in."
                    if wheel_changed
                    else (
                        f"✅ **{game[1]}** was already on the wheel, so the "
                        "cache was left alone and the game was locked in."
                    )
                )
            ),
            embed=None,
            view=None,
        )

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.secondary,
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.edit_message(
            content="Custom selection cancelled.",
            embed=None,
            view=None,
        )


class Sessions(
    commands.GroupCog,
    group_name="session",
    group_description="Start and control a gaming session",
):
    def __init__(
        self,
        bot: commands.Bot,
    ) -> None:
        self.bot = bot
        self._restored = False

    async def _sync_weekly_scheduled_event(
        self,
        session: dict,
        *,
        start: bool,
        now_utc: datetime | None = None,
    ) -> bool:
        """Best-effort sync between a wheel session and its Discord event."""

        try:
            if (
                GAME_NIGHT_VOICE_CHANNEL_ID is None
                or int(session["voice_channel_id"])
                != int(GAME_NIGHT_VOICE_CHANNEL_ID)
            ):
                return False

            now_utc = now_utc or datetime.now(timezone.utc)

            if now_utc.tzinfo is None:
                now_utc = now_utc.replace(tzinfo=timezone.utc)

            now_utc = now_utc.astimezone(timezone.utc)
            local_now = now_utc.astimezone(
                ZoneInfo(GAME_NIGHT_TIMEZONE)
            )
            week_start = (
                local_now.date()
                - timedelta(days=local_now.weekday())
            )
            record = await get_game_night_week(
                week_start.isoformat()
            )

            if (
                record is None
                or not record.get("scheduled_event_id")
                or not record.get("event_start_at")
            ):
                return False

            event_start = datetime.fromisoformat(
                str(record["event_start_at"])
            )

            if event_start.tzinfo is None:
                event_start = event_start.replace(
                    tzinfo=timezone.utc
                )

            event_start = event_start.astimezone(timezone.utc)

            if start and not (
                event_start - SESSION_EVENT_EARLY_START
                <= now_utc
                <= event_start + SESSION_EVENT_LATE_START
            ):
                return False

            guild = self.bot.get_guild(int(session["guild_id"]))

            if guild is None:
                return False

            event = await guild.fetch_scheduled_event(
                int(record["scheduled_event_id"])
            )

            if int(getattr(event, "channel_id", 0) or 0) != int(
                session["voice_channel_id"]
            ):
                return False

            if start:
                if event.status is not discord.EventStatus.scheduled:
                    return False

                await event.start(
                    reason="Game Night wheel session started"
                )
                action = "started"
            else:
                if event.status is not discord.EventStatus.active:
                    return False

                await event.end(
                    reason="Game Night wheel session ended"
                )
                action = "completed"

            LOGGER.info(
                "Discord Game Night event %s when wheel session %s: "
                "event=%s session=%s",
                action,
                "started" if start else "ended",
                event.id,
                session["id"],
            )
            return True
        except Exception:
            LOGGER.exception(
                "Could not %s the Discord Game Night event for "
                "wheel session %s; the wheel session will continue",
                "start" if start else "complete",
                session.get("id"),
            )
            return False

    async def _get_text_channel(
        self,
        guild: discord.Guild,
        channel_id: int | None,
    ):
        if channel_id is None:
            return None

        channel = guild.get_channel(int(channel_id))

        if channel is None:
            try:
                channel = await self.bot.fetch_channel(
                    int(channel_id)
                )
            except discord.DiscordException:
                return None

        return channel if hasattr(channel, "send") else None

    async def _selected_game(
        self,
        session: dict,
    ) -> tuple | None:
        game_id = session.get("selected_game_id")

        if game_id is None:
            return None

        return (
            await get_spin_games_by_ids([game_id])
        ).get(int(game_id))

    async def _build_embed(
        self,
        session: dict,
    ) -> discord.Embed:
        selected_game = await self._selected_game(session)
        selected_name = _selected_game_name(
            session,
            selected_game,
        )
        member_mentions = [
            f"<@{member['user_id']}>"
            for member in session["members"]
        ]
        member_text = ", ".join(member_mentions) or "Nobody joined yet"

        if len(member_text) > 900:
            member_text = member_text[:897] + "..."

        embed = discord.Embed(
            title=(
                "🎮 Gaming Session — Game Locked In"
                if selected_name
                else "🎮 Active Gaming Session"
            ),
            description=(
                f"Hosted by <@{session['host_id']}> in "
                f"<#{session['voice_channel_id']}>"
            ),
            colour=(
                discord.Colour.green()
                if selected_name
                else discord.Colour.blurple()
            ),
        )
        embed.add_field(
            name="Players",
            value=(
                f"**{len(session['members'])} joined**\n"
                f"{member_text}"
            ),
            inline=False,
        )
        count_label = (
            "Manual override"
            if session["manual_player_count"] is not None
            else "Voice refresh"
        )
        embed.add_field(
            name="Wheel Size",
            value=(
                f"Games supporting **{session['effective_player_count']}+ "
                f"players**\nSource: {count_label}"
            ),
            inline=True,
        )
        eligibility = await get_session_wheel_game_ids(
            session["effective_player_count"],
            include_unverified=bool(
                session["include_unverified"]
            ),
            use_normal_wheel=bool(
                session["use_normal_wheel"]
            ),
        )
        embed.add_field(
            name="Games on Wheel",
            value=(
                f"**{eligibility['eligible_count']} eligible games**"
            ),
            inline=True,
        )

        if selected_name:
            selected_value = f"**{selected_name}**"
            selected_link = (
                session.get("custom_game_link")
                or (
                    selected_game[2]
                    if selected_game is not None
                    else None
                )
            )

            if selected_link:
                selected_value += f"\n[Open game page]({selected_link})"

            embed.add_field(
                name="Selected Game",
                value=selected_value,
                inline=True,
            )

        embed.set_footer(
            text=(
                "The host or a moderator controls spins, refreshes and "
                "ending the session."
            )
        )
        return embed

    async def _session_cache_text(
        self,
        session: dict,
    ) -> str:
        if (
            session.get("selected_game_id")
            or session.get("custom_game_name")
        ):
            return "Released after the game was locked in."

        manager = getattr(
            self.bot,
            "prepared_spin_manager",
            None,
        )
        prepared_cache_enabled = bool(
            manager is not None
            and manager.enabled
        )
        cache_status = (
            await manager.get_session_pool_status(session["id"])
            if manager is not None
            else {"exists": False, "ready_count": 0}
        )
        ready_count = int(
            cache_status.get("ready_count", 0)
        )
        target_size = int(
            cache_status.get("target_size", 5)
        )
        eligible_count = int(
            cache_status.get("eligible_count", 0)
        )
        unverified_count = int(
            cache_status.get("unverified_count", 0)
        )

        if prepared_cache_enabled:
            cache_text = (
                f"**{ready_count}/{target_size} spins ready**\n"
                f"{eligible_count} eligible games"
            )
        else:
            cache_text = (
                "**On-demand wheel active**\n"
                f"{eligible_count} eligible games"
            )

        if unverified_count:
            cache_text += (
                f"\n{unverified_count} games have unverified capacity"
            )

        if session["use_normal_wheel"]:
            cache_text += (
                "\nPlayer filtering is currently disabled."
            )

        return cache_text

    async def _configure_pool(
        self,
        session: dict,
    ) -> dict:
        manager = getattr(
            self.bot,
            "prepared_spin_manager",
            None,
        )

        if manager is None:
            return await get_session_wheel_game_ids(
                session["effective_player_count"],
                include_unverified=bool(
                    session["include_unverified"]
                ),
                use_normal_wheel=bool(
                    session["use_normal_wheel"]
                ),
            )

        return await manager.configure_session_pool(
            session["id"],
            generation=session["cache_generation"],
            player_count=session["effective_player_count"],
            include_unverified=bool(
                session["include_unverified"]
            ),
            use_normal_wheel=bool(
                session["use_normal_wheel"]
            ),
        )

    async def _edit_control_card(
        self,
        session: dict,
    ) -> None:
        if not session.get("control_channel_id"):
            return

        guild = self.bot.get_guild(
            int(session["guild_id"])
        )

        if guild is None:
            return

        channel = await self._get_text_channel(
            guild,
            session["control_channel_id"],
        )

        if channel is None:
            return

        try:
            message = await channel.fetch_message(
                int(session["control_message_id"])
            )
            await message.edit(
                embed=await self._build_embed(session),
                view=SessionView(self, session),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.DiscordException:
            LOGGER.exception(
                "Could not update gaming session card %s",
                session["id"],
            )

    async def can_control_session(
        self,
        interaction: discord.Interaction,
        session_id: int,
        *,
        generation: int | None = None,
    ) -> bool:
        session = await get_gaming_session(session_id)

        if (
            session is None
            or session["status"] != "active"
        ):
            await interaction.response.send_message(
                "This gaming session is no longer active.",
                ephemeral=True,
            )
            return False

        if (
            generation is not None
            and int(session["cache_generation"])
            != int(generation)
        ):
            await interaction.response.send_message(
                "The session wheel changed. Use the current session card "
                "to spin again.",
                ephemeral=True,
            )
            return False

        permissions = getattr(
            interaction.user,
            "guild_permissions",
            None,
        )
        can_manage = bool(
            permissions
            and (
                permissions.manage_guild
                or permissions.administrator
            )
        )

        if (
            interaction.user.id != int(session["host_id"])
            and not can_manage
        ):
            await interaction.response.send_message(
                "Only the session host or a moderator can do that.",
                ephemeral=True,
            )
            return False

        return True

    async def can_manage_session_cache(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        permissions = getattr(
            interaction.user,
            "guild_permissions",
            None,
        )
        can_manage = bool(
            permissions
            and (
                permissions.manage_guild
                or permissions.administrator
            )
        )

        if not can_manage:
            await interaction.response.send_message(
                "Only moderators with Manage Server can view "
                "session cache controls.",
                ephemeral=True,
            )
            return False

        return True

    async def _active_session_for_user(
        self,
        interaction: discord.Interaction,
    ) -> dict | None:
        voice_state = getattr(
            interaction.user,
            "voice",
            None,
        )

        if (
            interaction.guild is None
            or voice_state is None
            or voice_state.channel is None
        ):
            return None

        return await get_active_gaming_session_for_voice(
            interaction.guild.id,
            voice_state.channel.id,
        )

    async def restore_active_sessions(self) -> None:
        if self._restored:
            return

        self._restored = True
        sessions = await get_active_gaming_sessions()

        for session in sessions:
            if session["status"] == "starting":
                await end_gaming_session(session["id"])
                continue

            try:
                self.bot.add_view(
                    SessionView(self, session),
                    message_id=int(session["control_message_id"]),
                )
                active_game = next(
                    (
                        game
                        for game in reversed(
                            session.get("games_played", [])
                        )
                        if game.get("finished_at") is None
                    ),
                    None,
                )

                if (
                    active_game is not None
                    and active_game.get("lock_message_id")
                ):
                    self.bot.add_view(
                        FinishedGameView(self, session["id"]),
                        message_id=int(active_game["lock_message_id"]),
                    )

                await self._configure_pool(session)
                await self._edit_control_card(session)
            except Exception:
                LOGGER.exception(
                    "Could not restore gaming session %s",
                    session["id"],
                )

        if sessions:
            LOGGER.info(
                "Restored %s active gaming session controls",
                len(sessions),
            )

    async def refresh_session_card(
        self,
        session_id: int,
    ) -> None:
        session = await get_gaming_session(session_id)

        if session is not None and session["status"] == "active":
            await self._edit_control_card(session)

    def locked_game_view(
        self,
        session_id: int,
    ) -> FinishedGameView:
        return FinishedGameView(self, session_id)

    async def save_locked_game_message(
        self,
        session_id: int,
        *,
        channel_id: int,
        message_id: int,
    ) -> None:
        await save_gaming_session_game_lock_message(
            session_id,
            channel_id=channel_id,
            message_id=message_id,
        )

    async def _delete_locked_game_message(
        self,
        played: dict | None,
    ) -> None:
        if (
            played is None
            or not played.get("lock_message_channel_id")
            or not played.get("lock_message_id")
        ):
            return

        try:
            channel = await self.bot.fetch_channel(
                int(played["lock_message_channel_id"])
            )
            message = await channel.fetch_message(
                int(played["lock_message_id"])
            )
            await message.delete()
        except discord.NotFound:
            return
        except discord.DiscordException:
            LOGGER.warning(
                "Could not delete locked-game message %s",
                played["lock_message_id"],
            )

    @app_commands.command(
        name="start",
        description="Start a player-count-aware gaming session",
    )
    @app_commands.describe(
        notify="Ping the configured session role when the session starts",
    )
    async def start(
        self,
        interaction: discord.Interaction,
        notify: bool = True,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Gaming sessions can only be started in the server.",
                ephemeral=True,
            )
            return

        voice_state = getattr(interaction.user, "voice", None)

        if voice_state is None or voice_state.channel is None:
            await interaction.response.send_message(
                "Join a Game Room voice channel before starting a session.",
                ephemeral=True,
            )
            return

        if SESSION_CHANNEL_ID is None:
            await interaction.response.send_message(
                "Gaming sessions are not configured yet. Set "
                "`SESSION_CHANNEL_ID` to the multiplayer meetup channel.",
                ephemeral=True,
            )
            return

        existing = await get_active_gaming_session_for_voice(
            interaction.guild.id,
            voice_state.channel.id,
        )

        if existing is not None:
            jump_url = (
                "https://discord.com/channels/"
                f"{existing['guild_id']}/"
                f"{existing['control_channel_id']}/"
                f"{existing['control_message_id']}"
            )
            await interaction.response.send_message(
                f"That voice room already has an active session: {jump_url}",
                ephemeral=True,
            )
            return

        control_channel = await self._get_text_channel(
            interaction.guild,
            SESSION_CHANNEL_ID,
        )

        if control_channel is None:
            await interaction.response.send_message(
                "I cannot access the configured session channel.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        members = _human_voice_members(voice_state.channel)
        session = await create_gaming_session(
            guild_id=interaction.guild.id,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            voice_channel_id=voice_state.channel.id,
            members=members,
        )

        try:
            await self._configure_pool(session)
            role = (
                interaction.guild.get_role(SESSION_NOTIFY_ROLE_ID)
                if (
                    notify
                    and SESSION_NOTIFY_ROLE_ID is not None
                )
                else None
            )
            content = (
                f"{role.mention}\n"
                if role is not None
                else ""
            )
            content += (
                f"🎮 {interaction.user.mention} started a gaming session!"
            )
            control_message = await control_channel.send(
                content=content,
                embed=await self._build_embed(session),
                view=SessionView(self, session),
                allowed_mentions=discord.AllowedMentions(
                    roles=True,
                    users=True,
                    everyone=False,
                ),
            )
            voice_notice_message_id = None

            if voice_state.channel.id != control_channel.id:
                try:
                    voice_notice = await voice_state.channel.send(
                        "🎮 A wheel session is active for this room.\n"
                        f"[Open the session card]({control_message.jump_url})",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    voice_notice_message_id = int(voice_notice.id)
                except discord.DiscordException:
                    LOGGER.warning(
                        "Could not post the session backlink in voice "
                        "channel %s",
                        voice_state.channel.id,
                    )

            session = await activate_gaming_session(
                session["id"],
                control_channel_id=control_channel.id,
                control_message_id=control_message.id,
                voice_notice_message_id=voice_notice_message_id,
            )
            await self._edit_control_card(session)
            event_started = await self._sync_weekly_scheduled_event(
                session,
                start=True,
            )

            if event_started:
                events_cog = self.bot.get_cog("GameNightEvents")

                if events_cog is not None:
                    await events_cog.close_checkin_for_session(session)

        except Exception:
            await end_gaming_session(session["id"])
            manager = getattr(
                self.bot,
                "prepared_spin_manager",
                None,
            )

            if manager is not None:
                await manager.remove_session_pool(session["id"])

            raise

        await interaction.edit_original_response(
            content=(
                "✅ Session created in "
                f"{control_channel.mention}: {control_message.jump_url}"
            )
        )

    @app_commands.command(
        name="cache",
        description="Show staff-only session cache controls",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        session_id=(
            "Optional session number; otherwise uses your voice room"
        ),
    )
    async def cache(
        self,
        interaction: discord.Interaction,
        session_id: int | None = None,
    ) -> None:
        if not await self.can_manage_session_cache(interaction):
            return

        if interaction.guild is None:
            await interaction.response.send_message(
                "Session cache controls are only available in the server.",
                ephemeral=True,
            )
            return

        session = None

        if session_id is not None:
            session = await get_gaming_session(session_id)

        else:
            session = await self._active_session_for_user(
                interaction
            )

            if session is None:
                guild_sessions = [
                    candidate
                    for candidate in (
                        await get_active_gaming_sessions()
                    )
                    if (
                        candidate["status"] == "active"
                        and int(candidate["guild_id"])
                        == int(interaction.guild.id)
                    )
                ]

                if len(guild_sessions) == 1:
                    session = guild_sessions[0]

        if (
            session is None
            or session["status"] != "active"
            or int(session["guild_id"])
            != int(interaction.guild.id)
        ):
            await interaction.response.send_message(
                "I could not identify an active session. Join its voice "
                "room or enter the session number shown on its card.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"Session #{session['id']} Cache",
            description=await self._session_cache_text(session),
            colour=discord.Colour.dark_teal(),
        )
        embed.set_footer(
            text="Staff-only session maintenance"
        )
        await interaction.response.send_message(
            embed=embed,
            view=SessionCacheView(self, session),
            ephemeral=True,
        )

    @app_commands.command(
        name="select",
        description="Manually select a game already on the wheel",
    )
    @app_commands.describe(game="Choose a multiplayer-wheel game")
    async def select(
        self,
        interaction: discord.Interaction,
        game: str,
    ) -> None:
        session = await self._active_session_for_user(interaction)

        if session is None:
            await interaction.response.send_message(
                "Join the voice room for an active session first.",
                ephemeral=True,
            )
            return

        if not await self.can_control_session(
            interaction,
            session["id"],
        ):
            return

        try:
            game_id = int(game)
        except ValueError:
            await interaction.response.send_message(
                "Choose a game from the autocomplete list.",
                ephemeral=True,
            )
            return

        selected_game = (
            await get_spin_games_by_ids([game_id])
        ).get(game_id)

        if selected_game is None:
            await interaction.response.send_message(
                "That game is no longer available on the wheel.",
                ephemeral=True,
            )
            return

        max_players = selected_game[8]
        warning = ""

        if (
            max_players is not None
            and int(max_players) < session["effective_player_count"]
        ):
            warning = (
                f"\n⚠️ This game supports **{max_players} players**, "
                f"while the session wheel is set to "
                f"**{session['effective_player_count']}**."
            )

        embed = discord.Embed(
            title="🎮 Manual Game Selection",
            description=(
                f"## {selected_game[1]}\n"
                f"Store: **{selected_game[3]}**{warning}"
            ),
            colour=discord.Colour.gold(),
        )
        await interaction.response.send_message(
            embed=embed,
            view=ManualGameSelectionView(
                self,
                session["id"],
                selected_game,
                interaction.user.id,
            ),
            ephemeral=True,
        )

    @select.autocomplete("game")
    async def select_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        games = await search_session_games(current)
        return [
            app_commands.Choice(
                name=(
                    f"{game['name']} "
                    f"({game['max_players'] or '?'} players)"
                )[:100],
                value=str(game["id"]),
            )
            for game in games
        ]

    @app_commands.command(
        name="custom",
        description="Choose a game outside the wheel for this session",
    )
    @app_commands.describe(
        name="Game name",
        link="Optional Steam or Epic Games Store link",
    )
    async def custom(
        self,
        interaction: discord.Interaction,
        name: str,
        link: str | None = None,
    ) -> None:
        session = await self._active_session_for_user(interaction)

        if session is None:
            await interaction.response.send_message(
                "Join the voice room for an active session first.",
                ephemeral=True,
            )
            return

        if not await self.can_control_session(
            interaction,
            session["id"],
        ):
            return

        await interaction.response.defer(ephemeral=True)
        game_info = None

        if link:
            game_info = await get_game_info_from_url(
                self.bot.http_session,
                link,
            )

            if game_info is not None:
                await enrich_missing_player_metadata(
                    self.bot.http_session,
                    [game_info],
                )
        else:
            game_info = await find_game_by_title(
                self.bot.http_session,
                name,
            )

        if game_info is None:
            game_info = {
                "name": name.strip(),
                "store_link": link,
                "source_link": link,
                "store": "Custom",
                "link_status": "unknown",
            }
        else:
            game_info["name"] = (
                game_info.get("name")
                or name.strip()
            )
            game_info["store_link"] = (
                game_info.get("store_link")
                or link
            )

        embed = discord.Embed(
            title="🎮 Custom Game Selection",
            description=(
                f"## {game_info['name']}\n"
                "Choose whether this is temporary or should also be "
                "added to the permanent wheel."
            ),
            colour=discord.Colour.gold(),
        )

        lookup_source = game_info.get("title_lookup_source")
        lookup_query = game_info.get("title_lookup_query")

        if lookup_source:
            lookup_value = str(lookup_source)
            lookup_score = game_info.get("title_lookup_score")

            if lookup_score is not None:
                lookup_value += (
                    f" ({round(float(lookup_score) * 100)}% title match)"
                )

            if (
                lookup_query
                and str(lookup_query).casefold()
                != str(game_info["name"]).casefold()
            ):
                lookup_value += f"\nSearched for: **{lookup_query}**"

            embed.add_field(
                name="Match",
                value=lookup_value,
                inline=False,
            )

        max_players = game_info.get("max_players")
        embed.add_field(
            name="Player Capacity",
            value=(
                f"Up to **{max_players} players** "
                f"({game_info.get('max_players_source') or 'verified'})"
                if max_players is not None
                else "Not verified — you can still use this game."
            ),
            inline=True,
        )

        if game_info.get("store_link"):
            embed.add_field(
                name="Store",
                value=(
                    f"[{game_info.get('store') or 'Open page'}]"
                    f"({game_info['store_link']})"
                ),
                inline=True,
            )

        if game_info.get("image_url"):
            embed.set_image(url=game_info["image_url"])

        await interaction.edit_original_response(
            embed=embed,
            view=CustomGameSelectionView(
                self,
                session["id"],
                game_info,
                interaction.user.id,
            ),
        )

    async def join_session(
        self,
        interaction: discord.Interaction,
        session_id: int,
    ) -> None:
        session = await get_gaming_session(session_id)
        voice_state = getattr(interaction.user, "voice", None)

        if (
            voice_state is None
            or voice_state.channel is None
            or int(voice_state.channel.id)
            != int(session["voice_channel_id"])
        ):
            await interaction.response.send_message(
                "Join the session's voice room first, then press Join.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        session = await add_gaming_session_member(
            session_id,
            user_id=interaction.user.id,
            display_name=interaction.user.display_name,
        )
        await self._edit_control_card(session)
        await interaction.edit_original_response(
            content=(
                "✅ You joined the session. The host can press "
                "**Refresh Voice** when the wheel should use the new count."
            )
        )

    async def leave_session(
        self,
        interaction: discord.Interaction,
        session_id: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        session = await remove_gaming_session_member(
            session_id,
            user_id=interaction.user.id,
        )
        await self._edit_control_card(session)
        await interaction.edit_original_response(
            content=(
                "You left the session. The host can refresh the voice "
                "count before the next spin."
            )
        )

    async def spin_session(
        self,
        interaction: discord.Interaction,
        session_id: int,
    ) -> None:
        if not await self.can_control_session(
            interaction,
            session_id,
        ):
            return

        session = await get_gaming_session(session_id)

        if (
            session.get("selected_game_id")
            or session.get("custom_game_name")
        ):
            await interaction.response.send_message(
                "A game is already locked in. Press **Return to Wheel** "
                "on the session card first.",
                ephemeral=True,
            )
            return

        eligibility = await get_session_wheel_game_ids(
            session["effective_player_count"],
            include_unverified=bool(session["include_unverified"]),
            use_normal_wheel=bool(session["use_normal_wheel"]),
        )

        if not eligibility["game_ids"]:
            await interaction.response.send_message(
                "No confirmed games support this player count. Try "
                "**Unverified: On** or **Normal Wheel: On**.",
                ephemeral=True,
            )
            return

        manager = getattr(
            self.bot,
            "prepared_spin_manager",
            None,
        )

        if manager is not None and manager.enabled:
            status = await manager.get_session_pool_status(session_id)

            if not status.get("exists"):
                await self._configure_pool(session)
                status = await manager.get_session_pool_status(session_id)

            if int(status.get("ready_count", 0)) == 0:
                await interaction.response.send_message(
                    "🔄 The player-count wheel is warming up. The first "
                    "prepared spin should be ready shortly.",
                    ephemeral=True,
                )
                return

        games_cog = self.bot.get_cog("Games")

        if games_cog is None:
            await interaction.response.send_message(
                "The wheel command is temporarily unavailable.",
                ephemeral=True,
            )
            return

        await games_cog.run_session_spin(
            interaction,
            {
                "session_id": int(session_id),
                "generation": int(session["cache_generation"]),
                "host_id": int(session["host_id"]),
                "eligible_game_ids": eligibility["game_ids"],
                "player_count": int(
                    session["effective_player_count"]
                ),
            },
        )

    async def refresh_voice_members(
        self,
        interaction: discord.Interaction,
        session_id: int,
    ) -> None:
        if not await self.can_control_session(
            interaction,
            session_id,
        ):
            return

        session = await get_gaming_session(session_id)
        guild = interaction.guild
        voice_channel = guild.get_channel(
            int(session["voice_channel_id"])
        )

        if voice_channel is None:
            await interaction.response.send_message(
                "The session voice channel no longer exists.",
                ephemeral=True,
            )
            return

        old_player_count = int(session["effective_player_count"])
        old_eligibility = await get_session_wheel_game_ids(
            old_player_count,
            include_unverified=bool(session["include_unverified"]),
            use_normal_wheel=bool(session["use_normal_wheel"]),
        )
        await interaction.response.defer(ephemeral=True)
        session = await replace_gaming_session_members(
            session_id,
            _human_voice_members(voice_channel),
        )
        new_eligibility = await self._configure_pool(session)
        await self._edit_control_card(session)
        old_games = int(old_eligibility["eligible_count"])
        new_games = int(new_eligibility["eligible_count"])
        game_delta = new_games - old_games

        if game_delta > 0:
            game_change = f"**{game_delta} more games** are eligible"
        elif game_delta < 0:
            game_change = f"**{abs(game_delta)} fewer games** are eligible"
        else:
            game_change = "the eligible game count is unchanged"

        await interaction.edit_original_response(
            content=(
                f"🔄 Players: **{old_player_count} → "
                f"{session['effective_player_count']}**. {game_change} "
                f"(**{new_games} total**)."
            )
        )

    async def change_player_count(
        self,
        interaction: discord.Interaction,
        session_id: int,
        *,
        player_count: int | None,
    ) -> None:
        if not await self.can_control_session(
            interaction,
            session_id,
        ):
            return

        await interaction.response.defer(ephemeral=True)
        session = await configure_gaming_session(
            session_id,
            manual_player_count=player_count,
            clear_manual_player_count=(player_count is None),
        )
        await self._configure_pool(session)
        await self._edit_control_card(session)
        await interaction.edit_original_response(
            content=(
                "🔄 Manual override cleared; the wheel now uses the last "
                "voice refresh."
                if player_count is None
                else (
                    f"🔄 New spins are being prepared for "
                    f"**{player_count} players**."
                )
            )
        )

    async def toggle_unverified(
        self,
        interaction: discord.Interaction,
        session_id: int,
    ) -> None:
        if not await self.can_control_session(interaction, session_id):
            return

        await interaction.response.defer(ephemeral=True)
        current = await get_gaming_session(session_id)
        session = await configure_gaming_session(
            session_id,
            include_unverified=not bool(
                current["include_unverified"]
            ),
        )
        await self._configure_pool(session)
        await self._edit_control_card(session)
        await interaction.edit_original_response(
            content=(
                "🔄 Unverified-capacity games are now "
                f"**{'included' if session['include_unverified'] else 'excluded'}**."
            )
        )

    async def toggle_normal_wheel(
        self,
        interaction: discord.Interaction,
        session_id: int,
    ) -> None:
        if not await self.can_control_session(interaction, session_id):
            return

        await interaction.response.defer(ephemeral=True)
        current = await get_gaming_session(session_id)
        session = await configure_gaming_session(
            session_id,
            use_normal_wheel=not bool(
                current["use_normal_wheel"]
            ),
        )
        await self._configure_pool(session)
        await self._edit_control_card(session)
        await interaction.edit_original_response(
            content=(
                "🔄 The session now uses the "
                f"**{'normal unfiltered wheel' if session['use_normal_wheel'] else 'player-count-filtered wheel'}**."
            )
        )

    async def rebuild_or_return_to_wheel(
        self,
        interaction: discord.Interaction,
        session_id: int,
    ) -> None:
        if not await self.can_control_session(interaction, session_id):
            return

        await interaction.response.defer(ephemeral=True)
        current = await get_gaming_session(session_id)

        returning = bool(
            current.get("selected_game_id")
            or current.get("custom_game_name")
        )

        if returning:
            cancelled = await cancel_gaming_session_game(session_id)
            await self._delete_locked_game_message(cancelled)
            session = await clear_gaming_session_selection(session_id)
            message = "🔄 The game was cleared and the session wheel is rebuilding."
        else:
            session = await configure_gaming_session(session_id)
            message = "🔄 The temporary session cache is rebuilding."

        await self._configure_pool(session)
        await self._edit_control_card(session)

        if returning:
            await self._restore_voice_session_notice(session)

        await interaction.edit_original_response(content=message)

    async def lock_game_from_spin(
        self,
        interaction: discord.Interaction,
        session_id: int,
        game: tuple,
    ) -> None:
        session = await select_gaming_session_game(
            session_id,
            selected_by_id=interaction.user.id,
            selected_by_name=interaction.user.display_name,
            game_id=game[0],
        )
        session = await start_gaming_session_game(
            session_id,
            game_id=game[0],
            game_name=str(game[1]),
            game_link=game[2],
            selected_by_id=interaction.user.id,
            selected_by_name=interaction.user.display_name,
        )
        await self._release_session_cache(session_id)
        await self._edit_control_card(session)
        await self._post_voice_lock_notice(session, str(game[1]))

    async def lock_existing_game(
        self,
        interaction: discord.Interaction,
        session_id: int,
        game: tuple,
    ) -> None:
        session = await select_gaming_session_game(
            session_id,
            selected_by_id=interaction.user.id,
            selected_by_name=interaction.user.display_name,
            game_id=game[0],
        )
        session = await start_gaming_session_game(
            session_id,
            game_id=game[0],
            game_name=str(game[1]),
            game_link=game[2],
            selected_by_id=interaction.user.id,
            selected_by_name=interaction.user.display_name,
        )
        await self._release_session_cache(session_id)
        await self._edit_control_card(session)
        await self._post_manual_lock_notice(session, str(game[1]))

    async def lock_custom_game(
        self,
        interaction: discord.Interaction,
        session_id: int,
        game_info: dict,
    ) -> None:
        session = await select_gaming_session_game(
            session_id,
            selected_by_id=interaction.user.id,
            selected_by_name=interaction.user.display_name,
            custom_name=game_info["name"],
            custom_link=(
                game_info.get("store_link")
                or game_info.get("source_link")
            ),
            custom_store=game_info.get("store"),
            custom_image_url=game_info.get("image_url"),
        )
        session = await start_gaming_session_game(
            session_id,
            game_name=str(game_info["name"]),
            game_link=(
                game_info.get("store_link")
                or game_info.get("source_link")
            ),
            selected_by_id=interaction.user.id,
            selected_by_name=interaction.user.display_name,
        )
        await self._release_session_cache(session_id)
        await self._edit_control_card(session)
        await self._post_manual_lock_notice(
            session,
            str(game_info["name"]),
        )

    async def _release_session_cache(
        self,
        session_id: int,
    ) -> None:
        manager = getattr(
            self.bot,
            "prepared_spin_manager",
            None,
        )

        if manager is not None:
            await manager.remove_session_pool(session_id)

    async def _delete_voice_session_notice(
        self,
        session: dict,
    ) -> None:
        notice_id = session.get(
            "voice_notice_message_id"
        )

        if notice_id is None:
            return

        guild = self.bot.get_guild(
            int(session["guild_id"])
        )

        if guild is None:
            return

        channel = await self._get_text_channel(
            guild,
            session.get("voice_channel_id"),
        )

        if channel is None or not hasattr(
            channel,
            "fetch_message",
        ):
            return

        try:
            message = await channel.fetch_message(
                int(notice_id)
            )
            await message.delete()

        except discord.NotFound:
            return

        except discord.DiscordException:
            LOGGER.warning(
                "Could not delete voice session notice %s",
                notice_id,
            )

    async def _post_voice_lock_notice(
        self,
        session: dict,
        game_name: str,
    ) -> None:
        guild = self.bot.get_guild(int(session["guild_id"]))
        channel = (
            await self._get_text_channel(
                guild,
                session["voice_channel_id"],
            )
            if guild is not None
            else None
        )
        notice_id = session.get("voice_notice_message_id")

        if channel is None or notice_id is None:
            return

        try:
            message = await channel.fetch_message(int(notice_id))
            await message.edit(
                content=(
                    f"🎉 **LOCKED IN: {game_name}**\n"
                    f"Locked by <@{session['selected_by_id']}>\n"
                    "Use **Finished Playing** on the session card when "
                    "you are ready for another spin."
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.DiscordException:
            LOGGER.warning(
                "Could not update the voice-room session notice %s",
                notice_id,
            )

    async def _post_manual_lock_notice(
        self,
        session: dict,
        game_name: str,
    ) -> None:
        await self._post_voice_lock_notice(session, game_name)

    async def _restore_voice_session_notice(
        self,
        session: dict,
    ) -> None:
        guild = self.bot.get_guild(int(session["guild_id"]))
        channel = (
            await self._get_text_channel(
                guild,
                session["voice_channel_id"],
            )
            if guild is not None
            else None
        )
        notice_id = session.get("voice_notice_message_id")

        if channel is None or notice_id is None:
            return

        jump_url = (
            "https://discord.com/channels/"
            f"{session['guild_id']}/{session['control_channel_id']}/"
            f"{session['control_message_id']}"
        )

        try:
            message = await channel.fetch_message(int(notice_id))
            await message.edit(
                content=(
                    "🎮 A wheel session is active for this room.\n"
                    f"[Open the session card]({jump_url})"
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.DiscordException:
            LOGGER.warning(
                "Could not restore the voice-room session notice %s",
                notice_id,
            )

    async def transfer_session_host(
        self,
        interaction: discord.Interaction,
        session_id: int,
        new_host,
    ) -> None:
        if getattr(new_host, "bot", False):
            await interaction.response.send_message(
                "A bot cannot host the session.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        session = await transfer_gaming_session_host(
            session_id,
            user_id=new_host.id,
            display_name=getattr(
                new_host,
                "display_name",
                new_host.name,
            ),
        )

        if session is None:
            await interaction.edit_original_response(
                content=(
                    "That person has not joined this session yet. Ask them "
                    "to press **Join Session**, then try again."
                ),
                view=None,
            )
            return

        await self._edit_control_card(session)
        await interaction.edit_original_response(
            content=f"👑 <@{new_host.id}> is now the session host.",
            view=None,
        )

    async def finish_playing(
        self,
        interaction: discord.Interaction,
        session_id: int,
    ) -> None:
        if not await self.can_control_session(interaction, session_id):
            return

        current = await get_gaming_session(session_id)

        if not (
            current.get("selected_game_id")
            or current.get("custom_game_name")
        ):
            await interaction.response.send_message(
                "There is no locked-in game to finish.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(
            ephemeral=True,
            thinking=True,
        )
        played = await finish_gaming_session_game(session_id)
        await self._delete_locked_game_message(played)

        if played is not None and played.get("game_id") is not None:
            await mark_game_played(
                game_id=int(played["game_id"]),
                locked_by=(
                    played.get("selected_by_name")
                    or current["host_name"]
                ),
            )
            manager = getattr(
                self.bot,
                "prepared_spin_manager",
                None,
            )

            if manager is not None:
                await manager.invalidate_game(int(played["game_id"]))

        session = await clear_gaming_session_selection(session_id)
        await self._configure_pool(session)
        await self._edit_control_card(session)
        await self._restore_voice_session_notice(session)
        played_name = (
            played.get("game_name")
            if played is not None
            else "The game"
        )
        duration = (
            _format_play_duration(
                played["started_at"],
                played["finished_at"],
            )
            if played is not None
            else None
        )
        await interaction.edit_original_response(
            content=(
                f"✅ **{played_name}** finished"
                f" after **{duration}**. The session card is ready for "
                "another spin."
                if duration
                else "✅ The session card is ready for another spin."
            )
        )

    async def end_session_from_button(
        self,
        interaction: discord.Interaction,
        session_id: int,
    ) -> None:
        if not await self.can_control_session(interaction, session_id):
            return

        session = await end_gaming_session(session_id)
        await self._release_session_cache(session_id)
        await self._delete_voice_session_notice(session)
        active_lock = (
            session.get("games_played", [])[-1]
            if (
                session.get("games_played")
                and (
                    session.get("selected_game_id")
                    or session.get("custom_game_name")
                )
            )
            else None
        )
        await self._delete_locked_game_message(active_lock)
        selected_game = await self._selected_game(session)

        if selected_game is not None:
            await mark_game_played(
                game_id=selected_game[0],
                locked_by=(
                    session.get("selected_by_name")
                    or session["host_name"]
                ),
            )
            manager = getattr(
                self.bot,
                "prepared_spin_manager",
                None,
            )

            if manager is not None:
                await manager.invalidate_game(
                    selected_game[0]
                )

        timeline_lines = []

        for index, played in enumerate(
            session.get("games_played", []),
            start=1,
        ):
            game_name = str(played["game_name"])
            game_link = played.get("game_link")
            label = (
                f"[{game_name}]({game_link})"
                if game_link
                else f"**{game_name}**"
            )
            timeline_lines.append(
                f"{index}. {label} — "
                f"{_format_play_duration(played['started_at'], played['finished_at'])}"
            )

        timeline_text = "\n".join(timeline_lines)

        if len(timeline_text) > 1024:
            timeline_text = timeline_text[:1021] + "..."

        session_duration = _format_play_duration(
            session["created_at"],
            session["ended_at"],
        )
        embed = discord.Embed(
            title="⏹️ Gaming Session Finished",
            description=(
                f"Hosted by <@{session['host_id']}> in "
                f"<#{session['voice_channel_id']}>"
            ),
            colour=discord.Colour.dark_grey(),
        )
        embed.add_field(
            name="Games Played",
            value=timeline_text or "No games were played",
            inline=False,
        )
        embed.add_field(
            name="Players",
            value=str(len(session["members"])),
            inline=True,
        )
        embed.add_field(
            name="Session Length",
            value=session_duration,
            inline=True,
        )
        embed.set_footer(text="Session closed")
        await interaction.response.edit_message(
            embed=embed,
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self._sync_weekly_scheduled_event(
            session,
            start=False,
        )


async def setup(
    bot: commands.Bot,
) -> None:
    await bot.add_cog(Sessions(bot))
