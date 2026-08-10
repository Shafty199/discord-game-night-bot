import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database.database import (
    close_game_night_checkin,
    get_game_night_checkins,
    get_game_night_week,
    save_game_night_poll,
    save_game_night_checkin_message,
    save_game_night_result,
    set_game_night_checkin,
    set_game_night_reminders,
)
from settings import (
    GAME_NIGHT_CHANNEL_ID,
    GAME_NIGHT_ROLE_ID,
    GAME_NIGHT_TIMEZONE,
    GAME_NIGHT_VOICE_CHANNEL_ID,
)
from utils.time_utils import parse_stored_datetime


LOGGER = logging.getLogger(__name__)

POLL_POST_HOUR = 11
POLL_DURATION = timedelta(hours=48)
POLL_LATEST_START_WEEKDAY = 1
POLL_LATEST_START_HOUR = 21
EVENT_START_HOUR = 21
SCHEDULER_INTERVAL_MINUTES = 5
REMINDER_HOURS = (24, 6, 1)
EVENT_NAME = "Game Night Roulette"

EVENT_DESCRIPTION = """Welcome to Game Night Roulette!

Every Monday, vote for Friday or Saturday night. The winning one-off event starts at 9:00 PM.

🎯 Suggest games in the configured suggestions thread.
🎲 Join the configured voice channel, then use /session start when everyone is ready.
📊 Use /games, /history and /stats to see what is on the wheel and what we have been playing.

Less deciding. More gaming."""


def _timezone() -> ZoneInfo:
    try:
        return ZoneInfo(GAME_NIGHT_TIMEZONE)
    except ZoneInfoNotFoundError:
        LOGGER.error(
            "Unknown GAME_NIGHT_TIMEZONE %r; using UTC",
            GAME_NIGHT_TIMEZONE,
        )
        return ZoneInfo("UTC")


def week_start_for(local_datetime: datetime) -> date:
    return (
        local_datetime.date()
        - timedelta(days=local_datetime.weekday())
    )


def event_start_for(
    week_start: date,
    winner_day: str,
    event_timezone: ZoneInfo,
) -> datetime:
    day_offset = 4 if winner_day == "friday" else 5
    return datetime.combine(
        week_start + timedelta(days=day_offset),
        time(hour=EVENT_START_HOUR),
        tzinfo=event_timezone,
    )


def choose_winner(
    friday_votes: int,
    saturday_votes: int,
) -> tuple[str, str]:
    friday_votes = max(0, int(friday_votes))
    saturday_votes = max(0, int(saturday_votes))

    if friday_votes > saturday_votes:
        return "friday", "votes"

    if saturday_votes > friday_votes:
        return "saturday", "votes"

    if friday_votes == 0:
        return "saturday", "no_votes"

    return "saturday", "tie"


def poll_creation_allowed(local_now: datetime) -> bool:
    weekday = local_now.weekday()

    if weekday == 0:
        return local_now.time() >= time(
            hour=POLL_POST_HOUR
        )

    return (
        weekday == POLL_LATEST_START_WEEKDAY
        and local_now.time()
        <= time(hour=POLL_LATEST_START_HOUR)
    )


def fallback_event_due(local_now: datetime) -> bool:
    return local_now >= datetime.combine(
        week_start_for(local_now) + timedelta(days=2),
        time(hour=POLL_POST_HOUR),
        tzinfo=local_now.tzinfo,
    )


def next_poll_at(local_now: datetime) -> datetime:
    monday = week_start_for(local_now)
    candidate = datetime.combine(
        monday,
        time(hour=POLL_POST_HOUR),
        tzinfo=local_now.tzinfo,
    )

    if local_now > candidate:
        candidate += timedelta(days=7)

    return candidate


def reminder_hours_due(
    record: dict,
    now_utc: datetime,
) -> tuple[int, ...]:
    event_start = parse_stored_datetime(
        record.get("event_start_at")
    )

    if event_start is None or now_utc >= event_start:
        return ()

    remaining = event_start - now_utc
    due = []

    for hours in REMINDER_HOURS:
        if (
            remaining <= timedelta(hours=hours)
            and not record.get(
                f"reminder_{hours}h_sent"
            )
        ):
            due.append(hours)

    return tuple(due)


def _discord_timestamp(
    value: datetime,
    style: str,
) -> str:
    return f"<t:{int(value.timestamp())}:{style}>"


def _poll_marker(week_start: date) -> str:
    return (
        "Game Night vote — week of "
        f"{week_start.day} {week_start.strftime('%B')}"
    )


def _poll_answer_text(
    day_name: str,
    event_start: datetime,
) -> str:
    return (
        f"{day_name} — {event_start.day} "
        f"{event_start.strftime('%B')} at 9:00 PM"
    )


def build_weekly_poll(
    week_start: date,
    event_timezone: ZoneInfo,
) -> discord.Poll:
    friday_start = event_start_for(
        week_start,
        "friday",
        event_timezone,
    )
    saturday_start = event_start_for(
        week_start,
        "saturday",
        event_timezone,
    )
    poll = discord.Poll(
        question="When should Game Night Roulette happen?",
        duration=POLL_DURATION,
        multiple=False,
    )
    poll.add_answer(
        text=_poll_answer_text(
            "Friday",
            friday_start,
        ),
        emoji="🌙",
    )
    poll.add_answer(
        text=_poll_answer_text(
            "Saturday",
            saturday_start,
        ),
        emoji="🎮",
    )
    return poll


class GameNightCheckInView(discord.ui.View):
    def __init__(
        self,
        cog,
        week_start: str,
        *,
        disabled: bool = False,
    ) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.week_start = str(week_start)

        for child in self.children:
            child.custom_id = (
                f"game-night-checkin:{self.week_start}:{child.custom_id}"
            )
            child.disabled = disabled

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        record = await get_game_night_week(self.week_start)

        if record is None or record.get("checkin_closed"):
            await interaction.response.send_message(
                "This Game Night check-in is closed.",
                ephemeral=True,
            )
            return False

        return True

    @discord.ui.button(
        label="Playing",
        emoji="🎮",
        style=discord.ButtonStyle.success,
        custom_id="playing",
    )
    async def playing(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.cog.record_checkin(
            interaction,
            self.week_start,
            "playing",
        )

    @discord.ui.button(
        label="Maybe",
        emoji="🤔",
        style=discord.ButtonStyle.primary,
        custom_id="maybe",
    )
    async def maybe(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.cog.record_checkin(
            interaction,
            self.week_start,
            "maybe",
        )

    @discord.ui.button(
        label="Can't Make It",
        emoji="❌",
        style=discord.ButtonStyle.secondary,
        custom_id="cant_make_it",
    )
    async def cant_make_it(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.cog.record_checkin(
            interaction,
            self.week_start,
            "cant_make_it",
        )


class GameNightEvents(
    commands.GroupCog,
    group_name="game-night",
    group_description="Weekly Game Night voting and event schedule",
):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.event_timezone = _timezone()
        self._cycle_lock = asyncio.Lock()

    async def cog_load(self) -> None:
        if (
            GAME_NIGHT_CHANNEL_ID is None
            or GAME_NIGHT_ROLE_ID is None
            or GAME_NIGHT_VOICE_CHANNEL_ID is None
        ):
            LOGGER.warning(
                "Weekly Game Night events are disabled because the "
                "channel, role and voice-channel IDs are not all configured"
            )
            return

        local_now = datetime.now(timezone.utc).astimezone(
            self.event_timezone
        )
        record = await get_game_night_week(
            week_start_for(local_now).isoformat()
        )

        if (
            record is not None
            and record.get("checkin_message_id")
            and not record.get("checkin_closed")
        ):
            self.bot.add_view(
                GameNightCheckInView(
                    self,
                    record["week_start"],
                ),
                message_id=int(record["checkin_message_id"]),
            )

        self.scheduler.start()

    async def cog_unload(self) -> None:
        self.scheduler.cancel()

    async def _fetch_channel(self, channel_id: int):
        channel = self.bot.get_channel(int(channel_id))

        if channel is None:
            channel = await self.bot.fetch_channel(
                int(channel_id)
            )

        return channel

    async def _configured_channels(self):
        if (
            GAME_NIGHT_CHANNEL_ID is None
            or GAME_NIGHT_VOICE_CHANNEL_ID is None
        ):
            raise RuntimeError(
                "Weekly Game Night channels are not configured"
            )

        poll_channel = await self._fetch_channel(
            GAME_NIGHT_CHANNEL_ID
        )
        voice_channel = await self._fetch_channel(
            GAME_NIGHT_VOICE_CHANNEL_ID
        )

        if not isinstance(
            poll_channel,
            (discord.TextChannel, discord.Thread),
        ):
            raise RuntimeError(
                "GAME_NIGHT_CHANNEL_ID is not a text channel"
            )

        if not isinstance(
            voice_channel,
            (discord.VoiceChannel, discord.StageChannel),
        ):
            raise RuntimeError(
                "GAME_NIGHT_VOICE_CHANNEL_ID is not a voice channel"
            )

        if poll_channel.guild.id != voice_channel.guild.id:
            raise RuntimeError(
                "The Game Night text and voice channels are in "
                "different servers"
            )

        return poll_channel, voice_channel

    async def _event_interest_count(
        self,
        record: dict,
    ) -> int | None:
        event_id = record.get("scheduled_event_id")

        if event_id is None:
            return None

        try:
            poll_channel, _ = await self._configured_channels()
            event = await poll_channel.guild.fetch_scheduled_event(
                int(event_id),
                with_counts=True,
            )
            count = getattr(event, "user_count", None)
            return int(count) if count is not None else None
        except discord.DiscordException:
            LOGGER.warning(
                "Could not read interested-user count for event %s",
                event_id,
            )
            return None

    async def _build_checkin_embed(
        self,
        record: dict,
        *,
        closed: bool | None = None,
    ) -> discord.Embed:
        checkins = await get_game_night_checkins(
            record["week_start"]
        )
        counts = checkins["counts"]
        event_start = parse_stored_datetime(
            record.get("event_start_at")
        )
        interested = await self._event_interest_count(record)
        embed = discord.Embed(
            title="🎮 Game Night Check-In",
            description=(
                "Let the group know where you stand. You can change your "
                "answer until the wheel session begins."
            ),
            colour=discord.Colour.green(),
        )

        if event_start is not None:
            embed.add_field(
                name="Starts",
                value=(
                    f"{_discord_timestamp(event_start, 'R')}\n"
                    f"{_discord_timestamp(event_start, 'F')}"
                ),
                inline=False,
            )

        embed.add_field(
            name="Playing",
            value=f"**{counts['playing']}**",
            inline=True,
        )
        embed.add_field(
            name="Maybe",
            value=f"**{counts['maybe']}**",
            inline=True,
        )
        embed.add_field(
            name="Can't Make It",
            value=f"**{counts['cant_make_it']}**",
            inline=True,
        )

        if interested is not None:
            embed.add_field(
                name="Interested in Discord Event",
                value=f"**{interested}**",
                inline=False,
            )

        is_closed = (
            bool(record.get("checkin_closed"))
            if closed is None
            else bool(closed)
        )
        embed.set_footer(
            text=(
                "Check-in closed — the session has started."
                if is_closed
                else "Press a button again if your plans change."
            )
        )
        return embed

    async def record_checkin(
        self,
        interaction: discord.Interaction,
        week_start: str,
        response: str,
    ) -> None:
        await interaction.response.defer()
        result = await set_game_night_checkin(
            week_start,
            user_id=interaction.user.id,
            display_name=interaction.user.display_name,
            response=response,
        )

        if result is None:
            await interaction.followup.send(
                "This Game Night check-in is closed.",
                ephemeral=True,
            )
            return

        record = await get_game_night_week(week_start)
        labels = {
            "playing": "Playing",
            "maybe": "Maybe",
            "cant_make_it": "Can't Make It",
        }
        await interaction.edit_original_response(
            embed=await self._build_checkin_embed(record),
            view=GameNightCheckInView(self, week_start),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await interaction.followup.send(
            f"Your response is now **{labels[response]}**.",
            ephemeral=True,
        )

    async def close_checkin_for_session(
        self,
        session: dict,
    ) -> bool:
        local_now = datetime.now(timezone.utc).astimezone(
            self.event_timezone
        )
        week_start = week_start_for(local_now).isoformat()
        record = await get_game_night_week(week_start)

        if (
            record is None
            or not record.get("checkin_message_id")
            or record.get("checkin_closed")
        ):
            return False

        record = await close_game_night_checkin(week_start)
        channel = await self._fetch_channel(
            int(record["checkin_channel_id"])
        )

        try:
            message = await channel.fetch_message(
                int(record["checkin_message_id"])
            )
            await message.edit(
                embed=await self._build_checkin_embed(
                    record,
                    closed=True,
                ),
                view=GameNightCheckInView(
                    self,
                    week_start,
                    disabled=True,
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.DiscordException:
            LOGGER.warning(
                "Could not close Game Night check-in message %s",
                record["checkin_message_id"],
            )

        return True

    def _role_content(self, text: str) -> str:
        if GAME_NIGHT_ROLE_ID is None:
            return text

        return f"<@&{GAME_NIGHT_ROLE_ID}> {text}"

    @staticmethod
    def _role_mentions() -> discord.AllowedMentions:
        return discord.AllowedMentions(
            roles=True,
            users=False,
            everyone=False,
            replied_user=False,
        )

    async def _find_existing_poll(
        self,
        channel,
        week_start: date,
    ):
        marker = _poll_marker(week_start)

        async for message in channel.history(limit=100):
            if (
                self.bot.user is not None
                and message.author.id == self.bot.user.id
                and marker in message.content
                and message.poll is not None
            ):
                return message

        return None

    async def _post_poll(
        self,
        local_now: datetime,
    ) -> dict:
        poll_channel, _ = await self._configured_channels()
        week_start = week_start_for(local_now)
        existing_message = await self._find_existing_poll(
            poll_channel,
            week_start,
        )

        if existing_message is not None:
            closes_at = (
                existing_message.created_at
                + POLL_DURATION
            )
            return await save_game_night_poll(
                week_start=week_start.isoformat(),
                channel_id=poll_channel.id,
                message_id=existing_message.id,
                created_at=(
                    existing_message.created_at.isoformat()
                ),
                closes_at=closes_at.isoformat(),
            )

        poll = build_weekly_poll(
            week_start,
            self.event_timezone,
        )
        marker = _poll_marker(week_start)
        closes_at = datetime.now(timezone.utc) + POLL_DURATION
        message = await poll_channel.send(
            self._role_content(
                f"🗳️ **{marker}**\n"
                "Vote for this week's game night. The poll closes "
                f"{_discord_timestamp(closes_at, 'R')}. "
                "Saturday is the fallback if the vote is tied or empty."
            ),
            poll=poll,
            allowed_mentions=self._role_mentions(),
        )
        closes_at = message.created_at + POLL_DURATION
        LOGGER.info(
            "Created weekly Game Night poll for %s: message=%s closes=%s",
            week_start,
            message.id,
            closes_at.isoformat(),
        )
        return await save_game_night_poll(
            week_start=week_start.isoformat(),
            channel_id=poll_channel.id,
            message_id=message.id,
            created_at=message.created_at.isoformat(),
            closes_at=closes_at.isoformat(),
        )

    async def _read_poll_votes(
        self,
        record: dict,
    ) -> tuple[int, int]:
        channel = await self._fetch_channel(
            int(record["poll_channel_id"])
        )
        message = await channel.fetch_message(
            int(record["poll_message_id"])
        )
        poll = message.poll

        if poll is None:
            raise RuntimeError(
                "The saved Game Night poll message has no poll"
            )

        if not poll.is_finalised():
            poll = await poll.end()

        friday_votes = 0
        saturday_votes = 0

        for answer in poll.answers:
            answer_text = answer.text.casefold()

            if answer_text.startswith("friday"):
                friday_votes = int(answer.vote_count or 0)
            elif answer_text.startswith("saturday"):
                saturday_votes = int(answer.vote_count or 0)

        return friday_votes, saturday_votes

    async def _find_existing_event(
        self,
        guild: discord.Guild,
        event_start_utc: datetime,
    ):
        events = await guild.fetch_scheduled_events(
            with_counts=False
        )

        for event in events:
            if (
                event.name == EVENT_NAME
                and abs(
                    (
                        event.start_time.astimezone(timezone.utc)
                        - event_start_utc
                    ).total_seconds()
                )
                <= 60
            ):
                return event

        return None

    async def _create_event(
        self,
        week_start: date,
        winner_day: str,
    ):
        poll_channel, voice_channel = (
            await self._configured_channels()
        )
        local_start = event_start_for(
            week_start,
            winner_day,
            self.event_timezone,
        )
        event_start_utc = local_start.astimezone(
            timezone.utc
        )
        event = await self._find_existing_event(
            poll_channel.guild,
            event_start_utc,
        )

        if event is None:
            event = await poll_channel.guild.create_scheduled_event(
                name=EVENT_NAME,
                start_time=event_start_utc,
                channel=voice_channel,
                description=EVENT_DESCRIPTION,
                privacy_level=discord.PrivacyLevel.guild_only,
                reason="Automatic result of the weekly Game Night vote",
            )
            LOGGER.info(
                "Created Game Night event %s for %s",
                event.id,
                event_start_utc.isoformat(),
            )
        else:
            LOGGER.info(
                "Reused existing Game Night event %s for %s",
                event.id,
                event_start_utc.isoformat(),
            )

        return event, event_start_utc

    async def _save_and_announce_result(
        self,
        *,
        week_start: date,
        winner_day: str,
        winner_reason: str,
        friday_votes: int,
        saturday_votes: int,
    ) -> dict:
        poll_channel, _ = await self._configured_channels()
        event, event_start = await self._create_event(
            week_start,
            winner_day,
        )
        record = await save_game_night_result(
            week_start=week_start.isoformat(),
            winner_day=winner_day,
            winner_reason=winner_reason,
            friday_votes=friday_votes,
            saturday_votes=saturday_votes,
            scheduled_event_id=event.id,
            event_start_at=event_start.isoformat(),
        )
        event_url = (
            "https://discord.com/events/"
            f"{poll_channel.guild.id}/{event.id}"
        )
        day_name = winner_day.title()

        if winner_reason == "tie":
            result_line = (
                "The vote was tied, so the Saturday tiebreaker was used."
            )
        elif winner_reason == "no_votes":
            result_line = (
                "No votes were cast, so the Saturday fallback was used."
            )
        elif winner_reason == "missed_poll":
            result_line = (
                "The poll could not run in time, so Saturday was chosen "
                "for this week."
            )
        else:
            result_line = f"**{day_name} won this week's vote!**"

        await poll_channel.send(
            "✅ **Game Night event created**\n"
            f"{result_line}\n"
            f"Friday: **{friday_votes}** • Saturday: "
            f"**{saturday_votes}**\n"
            f"🕘 {_discord_timestamp(event_start, 'F')}\n"
            f"[View the Discord event]({event_url})",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return record

    async def _resolve_poll(
        self,
        record: dict,
        week_start: date,
    ) -> dict:
        try:
            friday_votes, saturday_votes = (
                await self._read_poll_votes(record)
            )
            winner_day, winner_reason = choose_winner(
                friday_votes,
                saturday_votes,
            )
        except (discord.NotFound, RuntimeError):
            LOGGER.warning(
                "Weekly Game Night poll for %s was unavailable; "
                "using the Saturday fallback",
                week_start,
                exc_info=True,
            )
            friday_votes = 0
            saturday_votes = 0
            winner_day = "saturday"
            winner_reason = "no_votes"

        return await self._save_and_announce_result(
            week_start=week_start,
            winner_day=winner_day,
            winner_reason=winner_reason,
            friday_votes=friday_votes,
            saturday_votes=saturday_votes,
        )

    async def _send_due_reminder(
        self,
        record: dict,
        now_utc: datetime,
    ) -> None:
        due = reminder_hours_due(record, now_utc)

        if not due:
            return

        reminder_hour = min(due)
        await set_game_night_reminders(
            record["week_start"],
            due,
            sent=True,
        )

        try:
            poll_channel, voice_channel = (
                await self._configured_channels()
            )
            event_start = parse_stored_datetime(
                record["event_start_at"]
            )
            assert event_start is not None
            event_url = (
                "https://discord.com/events/"
                f"{poll_channel.guild.id}/"
                f"{record['scheduled_event_id']}"
            )
            headings = {
                24: "Game night is tomorrow!",
                6: "Game night starts later today!",
                1: "Game night starts soon!",
            }

            if reminder_hour == 6:
                message = await poll_channel.send(
                    content=self._role_content(
                        f"🎮 **{headings[reminder_hour]}** "
                        "Check in below so everyone knows the likely squad."
                    ),
                    embed=await self._build_checkin_embed(record),
                    view=GameNightCheckInView(
                        self,
                        record["week_start"],
                    ),
                    allowed_mentions=self._role_mentions(),
                )
                record = await save_game_night_checkin_message(
                    record["week_start"],
                    channel_id=poll_channel.id,
                    message_id=message.id,
                )

            elif reminder_hour == 1:
                checkins = await get_game_night_checkins(
                    record["week_start"]
                )
                counts = checkins["counts"]
                embed = discord.Embed(
                    title="🎮 Game Night starts soon!",
                    description=(
                        f"Starts {_discord_timestamp(event_start, 'R')} "
                        f"({_discord_timestamp(event_start, 'F')})."
                    ),
                    colour=discord.Colour.blurple(),
                )
                embed.add_field(
                    name="Latest Check-In",
                    value=(
                        f"🎮 Playing: **{counts['playing']}**\n"
                        f"🤔 Maybe: **{counts['maybe']}**\n"
                        f"❌ Can't Make It: "
                        f"**{counts['cant_make_it']}**"
                    ),
                    inline=False,
                )
                embed.add_field(
                    name="Event",
                    value=f"[View the Discord event]({event_url})",
                    inline=False,
                )
                join_view = discord.ui.View(timeout=None)
                join_view.add_item(
                    discord.ui.Button(
                        label="Join Voice",
                        emoji="🔊",
                        style=discord.ButtonStyle.link,
                        url=(
                            "https://discord.com/channels/"
                            f"{poll_channel.guild.id}/{voice_channel.id}"
                        ),
                    )
                )
                await poll_channel.send(
                    content=self._role_content(
                        f"🎮 **{headings[reminder_hour]}**"
                    ),
                    embed=embed,
                    view=join_view,
                    allowed_mentions=self._role_mentions(),
                )

            else:
                await poll_channel.send(
                    self._role_content(
                        f"🎮 **{headings[reminder_hour]}**\n"
                        f"Game Night Roulette starts "
                        f"{_discord_timestamp(event_start, 'R')} "
                        f"({_discord_timestamp(event_start, 'F')}).\n"
                        f"🔊 Join {voice_channel.mention}\n"
                        f"[View the Discord event]({event_url})"
                    ),
                    allowed_mentions=self._role_mentions(),
                )
            LOGGER.info(
                "Sent Game Night reminder: week=%s threshold=%sh",
                record["week_start"],
                reminder_hour,
            )

        except Exception:
            await set_game_night_reminders(
                record["week_start"],
                due,
                sent=False,
            )
            raise

    async def run_scheduler_cycle(
        self,
        *,
        now_utc: datetime | None = None,
    ) -> None:
        now_utc = now_utc or datetime.now(timezone.utc)
        local_now = now_utc.astimezone(
            self.event_timezone
        )
        week_start = week_start_for(local_now)
        record = await get_game_night_week(
            week_start.isoformat()
        )

        if record is None and poll_creation_allowed(local_now):
            record = await self._post_poll(local_now)

        if record is None and fallback_event_due(local_now):
            event_start = event_start_for(
                week_start,
                "saturday",
                self.event_timezone,
            )

            if local_now < event_start:
                record = await self._save_and_announce_result(
                    week_start=week_start,
                    winner_day="saturday",
                    winner_reason="missed_poll",
                    friday_votes=0,
                    saturday_votes=0,
                )

        if (
            record is not None
            and record.get("scheduled_event_id") is None
            and record.get("poll_closes_at")
        ):
            closes_at = parse_stored_datetime(
                record["poll_closes_at"]
            )

            if closes_at is not None and now_utc >= closes_at:
                record = await self._resolve_poll(
                    record,
                    week_start,
                )

        if (
            record is not None
            and record.get("scheduled_event_id") is not None
        ):
            await self._send_due_reminder(
                record,
                now_utc,
            )

    @tasks.loop(minutes=SCHEDULER_INTERVAL_MINUTES)
    async def scheduler(self) -> None:
        if self._cycle_lock.locked():
            return

        async with self._cycle_lock:
            try:
                await self.run_scheduler_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception(
                    "Weekly Game Night scheduler cycle failed"
                )

    @scheduler.before_loop
    async def before_scheduler(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(
        name="preview",
        description="Preview next week's poll and reminders without pinging",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def preview(
        self,
        interaction: discord.Interaction,
    ) -> None:
        local_now = datetime.now(timezone.utc).astimezone(
            self.event_timezone
        )
        poll_at = next_poll_at(local_now)
        week_start = week_start_for(poll_at)
        friday = event_start_for(
            week_start,
            "friday",
            self.event_timezone,
        )
        saturday = event_start_for(
            week_start,
            "saturday",
            self.event_timezone,
        )
        await interaction.response.send_message(
            "🧪 **Weekly Game Night preview — no role ping sent**\n"
            f"Poll: {_discord_timestamp(poll_at, 'F')} for 48 hours\n"
            f"Option 1: {_discord_timestamp(friday, 'F')}\n"
            f"Option 2: {_discord_timestamp(saturday, 'F')}\n"
            "Winner: one-off Game Night Roulette event\n"
            "Reminders: 24 hours, 6 hours and 1 hour before",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(
        name="status",
        description="Show this week's Game Night automation status",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status(
        self,
        interaction: discord.Interaction,
    ) -> None:
        local_now = datetime.now(timezone.utc).astimezone(
            self.event_timezone
        )
        week_start = week_start_for(local_now)
        record = await get_game_night_week(
            week_start.isoformat()
        )

        if record is None:
            poll_at = next_poll_at(local_now)
            message = (
                "No poll or event is recorded for this week.\n"
                f"Next poll: {_discord_timestamp(poll_at, 'F')}"
            )
        elif record.get("scheduled_event_id"):
            event_start = parse_stored_datetime(
                record["event_start_at"]
            )
            message = (
                f"Winner: **{record['winner_day'].title()}**\n"
                f"Votes: Friday **{record['friday_votes']}** • "
                f"Saturday **{record['saturday_votes']}**\n"
                f"Event: {_discord_timestamp(event_start, 'F')}\n"
                "Reminders sent: "
                f"24h **{record['reminder_24h_sent']}**, "
                f"6h **{record['reminder_6h_sent']}**, "
                f"1h **{record['reminder_1h_sent']}**"
            )
        else:
            closes_at = parse_stored_datetime(
                record["poll_closes_at"]
            )
            message = (
                "This week's poll is open.\n"
                f"Closes: {_discord_timestamp(closes_at, 'F')}"
            )

        await interaction.response.send_message(
            "📅 **Game Night automation status**\n" + message,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GameNightEvents(bot))
