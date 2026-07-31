import asyncio
import io
import logging
import re
from time import perf_counter

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from database.database import (
    batched_database_writes,
    delete_game_by_name,
    finalise_obsolete_steam_demo,
    get_all_artwork_records,
    get_all_game_cache_records,
    get_all_store_replacements,
    get_game_cache_record,
    get_game_metadata_audit_records,
    get_latest_history_entry,
    reset_all_play_history,
    sync_game,
    undo_latest_history_entry,
)
from settings import SUGGESTION_THREAD_ID
from utils.artwork_cache import (
    delete_local_game_artwork,
    maintain_local_artwork,
    prepare_local_game_artwork,
)
from utils.metadata import (
    epic_info_from_embeds,
    release_info_from_embeds,
)
from utils.metadata_audit import (
    build_metadata_audit_report,
)
from utils.igdb import (
    enrich_missing_player_metadata,
)
from utils.store import (
    clear_store_metadata_cache,
    detect_store,
    find_supported_store_links,
    get_game_info_from_url,
)
from utils.steam_api import clear_steam_details_cache
from utils.time_utils import format_display_datetime


LOGGER = logging.getLogger(__name__)
SYNC_PREFETCH_BATCH_SIZE = 50

def _steam_app_id_from_url(
    store_link,
) -> str | None:
    match = re.search(
        r"store\.steampowered\.com/(?:agecheck/)?app/(\d+)",
        str(store_link or ""),
        re.IGNORECASE,
    )

    if not match:
        return None

    return match.group(1)


def _sync_index_value(value) -> str:
    return str(value or "").strip().casefold()


def _index_game_cache_records(
    records: list[dict],
) -> dict[str, dict]:
    indexes = {
        "external": {},
        "link": {},
        "name": {},
    }

    for record in records:
        store = _sync_index_value(
            record.get("store")
        )
        external_id = _sync_index_value(
            record.get("external_id")
        )
        store_link = _sync_index_value(
            record.get("store_link")
        )
        name = _sync_index_value(
            record.get("name")
        )

        if store and external_id:
            indexes["external"].setdefault(
                (store, external_id),
                record,
            )

        if store_link:
            indexes["link"].setdefault(
                store_link,
                record,
            )

        if name:
            indexes["name"].setdefault(
                name,
                record,
            )

    return indexes


def _find_indexed_game_record(
    indexes: dict[str, dict],
    *,
    name: str | None,
    store: str | None,
    store_links,
    external_id: str | None,
) -> dict | None:
    store_key = _sync_index_value(store)
    external_key = _sync_index_value(
        external_id
    )

    if store_key and external_key:
        record = indexes["external"].get(
            (store_key, external_key)
        )

        if record:
            return record

    for store_link in store_links:
        link_key = _sync_index_value(
            store_link
        )

        if link_key:
            record = indexes["link"].get(
                link_key
            )

            if record:
                return record

    name_key = _sync_index_value(name)

    if name_key:
        return indexes["name"].get(
            name_key
        )

    return None


def _index_store_replacements(
    records: list[dict],
) -> dict[str, set]:
    indexes = {
        "external": set(),
        "link": set(),
    }

    for record in records:
        store = _sync_index_value(
            record.get("store")
        )
        external_id = _sync_index_value(
            record.get("old_external_id")
        )
        store_link = _sync_index_value(
            record.get("old_store_link")
        )

        if store and external_id:
            indexes["external"].add(
                (store, external_id)
            )

        if store and store_link:
            indexes["link"].add(
                (store, store_link)
            )

    return indexes


def _has_indexed_store_replacement(
    indexes: dict[str, set],
    *,
    store: str,
    external_id: str | None,
    store_link: str | None,
) -> bool:
    store_key = _sync_index_value(store)
    external_key = _sync_index_value(
        external_id
    )
    link_key = _sync_index_value(store_link)

    return bool(
        (
            external_key
            and (
                store_key,
                external_key,
            ) in indexes["external"]
        )
        or (
            link_key
            and (
                store_key,
                link_key,
            ) in indexes["link"]
        )
    )


KNOWN_OBSOLETE_DEMO_NAMES = {
    "4018900": "Triangle - Cursed Town Demo",
    "3911640": "Roadside Research Demo",
    "3957630": (
        "S.E.M.I. - Side Effects May Include Demo"
    ),
}


KNOWN_OBSOLETE_DEMO_FULL_GAMES = {
    "4018900": "3266950",
    "3911640": "3643170",
    "3957630": "3957560",
}


KNOWN_OBSOLETE_FULL_GAME_NAMES = {
    "3266950": "Triangle - Cursed Town",
    "3643170": "Roadside Research",
    "3957560": (
        "S.E.M.I. – Side Effects May Include..."
    ),
}


def _display_name_for_store_link(
    store_link,
    fallback_name=None,
) -> str:
    app_id = _steam_app_id_from_url(
        store_link
    )

    if (
        app_id
        and app_id
        in KNOWN_OBSOLETE_DEMO_NAMES
    ):
        return KNOWN_OBSOLETE_DEMO_NAMES[
            app_id
        ]

    cleaned_fallback = str(
        fallback_name or ""
    ).strip()

    if (
        cleaned_fallback
        and cleaned_fallback != "Unknown Game"
    ):
        return cleaned_fallback

    return str(
        store_link or "Unknown Store Entry"
    ).strip()


def _format_name_section(
    title: str,
    names: list[str],
    limit: int = 15,
) -> str | None:
    if not names:
        return None

    visible_names = names[:limit]

    lines = [
        f"• **{name}**"
        for name in visible_names
    ]

    remaining = len(names) - len(
        visible_names
    )

    if remaining > 0:
        lines.append(
            f"• …and **{remaining} more**"
        )

    return (
        f"## {title}\n"
        + "\n".join(
            lines
        )
    )


def _verification_label(
    game_name: str,
    store: str,
    http_status,
    error,
) -> str:
    details = []

    if store:
        details.append(
            str(store)
        )

    if http_status is not None:
        details.append(
            f"HTTP {http_status}"
        )

    elif error:
        details.append(
            str(error)[:80]
        )

    if not details:
        return game_name

    return (
        f"{game_name} — "
        + ", ".join(
            details
        )
    )


def _split_report_sections(
    sections: list[str],
    limit: int = 1900,
) -> list[str]:
    messages = []
    current = ""

    for section in sections:
        candidate = (
            section
            if not current
            else f"{current}\n\n{section}"
        )

        if (
            len(candidate) <= limit
        ):
            current = candidate
            continue

        if current:
            messages.append(
                current
            )

        current = section

    if current:
        messages.append(
            current
        )

    return messages


async def _prefetch_store_link(
    store_link: str,
    *,
    session: aiohttp.ClientSession,
    steam_semaphore: asyncio.Semaphore,
    epic_semaphore: asyncio.Semaphore,
) -> dict:
    store = detect_store(
        store_link
    )

    semaphore = (
        epic_semaphore
        if store == "Epic Games Store"
        else steam_semaphore
    )

    async with semaphore:
        try:
            game_info = (
                await get_game_info_from_url(
                    session,
                    store_link,
                    force_refresh=True,
                )
            )

            return {
                "game_info": game_info,
                "error": None,
            }

        except Exception as error:
            return {
                "game_info": None,
                "error": error,
            }


class ResetPlayCountsView(discord.ui.View):
    def __init__(
        self,
        author_id: int,
    ):
        super().__init__(
            timeout=60
        )

        self.author_id = author_id
        self.completed = False

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the moderator who started this "
                "reset can use these buttons.",
                ephemeral=True,
            )
            return False

        return True

    def _disable_buttons(self) -> None:
        for child in self.children:
            child.disabled = True

    @discord.ui.button(
        label="Reset All Play Data",
        emoji="🗑️",
        style=discord.ButtonStyle.danger,
    )
    async def confirm_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        self.completed = True
        self._disable_buttons()

        try:
            result = await reset_all_play_history()

        except Exception:
            LOGGER.exception(
                "Failed to reset all play history"
            )

            await interaction.response.edit_message(
                content=(
                    "❌ The play data reset failed. "
                    "No success could be confirmed. Check "
                    "the hosting console for details."
                ),
                view=self,
            )
            return

        await interaction.response.edit_message(
            content=(
                "## ✅ Play Data Reset Complete\n\n"
                f"🎮 Games reset: "
                f"**{result['games_reset']}**\n"
                f"🧹 History entries deleted: "
                f"**{result['history_entries_deleted']}**\n\n"
                "All games now show **Played: Never**, "
                "their last-played dates are cleared, and "
                "the testing history has been removed."
            ),
            view=self,
        )

        self.stop()

    @discord.ui.button(
        label="Cancel",
        emoji="✖️",
        style=discord.ButtonStyle.secondary,
    )
    async def cancel_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        self.completed = True
        self._disable_buttons()

        await interaction.response.edit_message(
            content=(
                "Reset cancelled. "
                "No play data was changed."
            ),
            view=self,
        )

        self.stop()

    async def on_timeout(self):
        if self.completed:
            return

        self._disable_buttons()


class UndoLastGameView(discord.ui.View):
    def __init__(
        self,
        *,
        author_id: int,
        history_entry: dict,
    ):
        super().__init__(
            timeout=60
        )

        self.author_id = author_id
        self.history_entry = history_entry
        self.completed = False

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the moderator who started this "
                "undo can use these buttons.",
                ephemeral=True,
            )
            return False

        return True

    def _disable_buttons(self) -> None:
        for child in self.children:
            child.disabled = True

    @discord.ui.button(
        label="Undo This Lock-In",
        emoji="↩️",
        style=discord.ButtonStyle.danger,
    )
    async def confirm_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        self.completed = True
        self._disable_buttons()

        try:
            result = await undo_latest_history_entry(
                expected_history_id=(
                    self.history_entry["history_id"]
                )
            )

        except Exception:
            LOGGER.exception(
                "Failed to undo history entry %s",
                self.history_entry["history_id"],
            )

            await interaction.response.edit_message(
                content=(
                    "❌ The lock-in could not be undone. "
                    "No success was recorded; check the "
                    "hosting console for details."
                ),
                view=self,
            )
            return

        if result["status"] == "empty":
            message = (
                "There is no Game Night history left to undo."
            )

        elif result["status"] == "stale":
            message = (
                "⚠️ A newer game was locked in while this "
                "confirmation was open. Nothing was changed. "
                "Run `/undo` again to review the latest entry."
            )

        else:
            message = (
                "## ✅ Lock-In Undone\n\n"
                f"🎮 **{result['name']}**\n"
                f"🔒 Originally locked in by "
                f"**{result['locked_by']}**\n"
                f"🎯 Remaining play count: "
                f"**{result['times_played']}**\n\n"
                "Only the latest history entry was removed."
            )

        await interaction.response.edit_message(
            content=message,
            view=self,
        )

        self.stop()

    @discord.ui.button(
        label="Cancel",
        emoji="✖️",
        style=discord.ButtonStyle.secondary,
    )
    async def cancel_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        self.completed = True
        self._disable_buttons()

        await interaction.response.edit_message(
            content=(
                "Undo cancelled. No play data was changed."
            ),
            view=self,
        )

        self.stop()

    async def on_timeout(self):
        if self.completed:
            return

        self._disable_buttons()


class Admin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="auditgames",
        description=(
            "List every game and any metadata it is missing"
        ),
    )
    @app_commands.checks.has_permissions(
        manage_guild=True
    )
    async def auditgames(
        self,
        interaction: discord.Interaction,
    ):
        await interaction.response.defer(
            ephemeral=True
        )

        try:
            games = (
                await get_game_metadata_audit_records()
            )
            report, summary = await asyncio.to_thread(
                build_metadata_audit_report,
                games,
            )

        except Exception:
            LOGGER.exception(
                "Failed to build the game metadata audit"
            )
            await interaction.followup.send(
                "❌ The metadata audit could not be created. "
                "Check the hosting console for details.",
                ephemeral=True,
            )
            return

        if not games:
            await interaction.followup.send(
                "There are no games in the database to audit.",
                ephemeral=True,
            )
            return

        gap_counts = summary["gap_counts"]
        common_gaps = sorted(
            gap_counts.items(),
            key=lambda item: (
                -item[1],
                item[0].casefold(),
            ),
        )
        gap_lines = [
            f"• **{label}:** {count}"
            for label, count in common_gaps[:8]
        ]

        if len(common_gaps) > 8:
            gap_lines.append(
                "• …plus "
                f"**{len(common_gaps) - 8} other gap types**"
            )

        if not gap_lines:
            gap_lines.append(
                "• No missing metadata detected"
            )

        report_file = discord.File(
            io.BytesIO(
                report.encode("utf-8-sig")
            ),
            filename="game-metadata-audit.csv",
            description=(
                "Every saved game and its missing metadata"
            ),
        )
        await interaction.followup.send(
            "## 🔎 Game Metadata Audit\n\n"
            f"🎮 Games scanned: **{summary['total']}**\n"
            f"✅ Complete: **{summary['complete']}**\n"
            "⚠️ Need metadata: "
            f"**{summary['incomplete']}**\n\n"
            "### Most common gaps\n"
            + "\n".join(gap_lines)
            + "\n\nThe attached CSV contains **every game**, "
            "its current player information, and exactly "
            "which fields are missing.",
            file=report_file,
            ephemeral=True,
        )

    @auditgames.error
    async def auditgames_error(
        self,
        interaction: discord.Interaction,
        error,
    ):
        if isinstance(
            error,
            app_commands.errors.MissingPermissions,
        ):
            message = (
                "❌ You need moderator permissions "
                "to use `/auditgames`."
            )

            if interaction.response.is_done():
                await interaction.followup.send(
                    message,
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    message,
                    ephemeral=True,
                )

            return

        raise error

    @app_commands.command(
        name="undo",
        description="Undo the most recent locked-in game",
    )
    @app_commands.checks.has_permissions(
        manage_guild=True
    )
    async def undo(
        self,
        interaction: discord.Interaction,
    ):
        try:
            history_entry = (
                await get_latest_history_entry()
            )

        except Exception:
            LOGGER.exception(
                "Failed to read the latest history entry"
            )
            await interaction.response.send_message(
                "❌ I could not read the latest Game Night "
                "history. Check the hosting console for details.",
                ephemeral=True,
            )
            return

        if history_entry is None:
            await interaction.response.send_message(
                "There is no Game Night history to undo.",
                ephemeral=True,
            )
            return

        played_date = format_display_datetime(
            history_entry["played_date"],
        )

        view = UndoLastGameView(
            author_id=interaction.user.id,
            history_entry=history_entry,
        )

        await interaction.response.send_message(
            "## ↩️ Undo Latest Lock-In?\n\n"
            f"🎮 Game: **{history_entry['name']}**\n"
            f"🔒 Locked in by: "
            f"**{history_entry['locked_by']}**\n"
            f"🕒 Recorded: `{played_date}`\n\n"
            "This removes only this history entry and "
            "recalculates that game's play count and "
            "last-played date.",
            view=view,
            ephemeral=True,
        )

    @app_commands.command(
        name="resetplaycounts",
        description=(
            "Reset all play counts, dates and game history"
        ),
    )
    @app_commands.checks.has_permissions(
        manage_guild=True
    )
    async def resetplaycounts(
        self,
        interaction: discord.Interaction,
    ):
        view = ResetPlayCountsView(
            author_id=interaction.user.id,
        )

        await interaction.response.send_message(
            "## ⚠️ Reset All Play Data?\n\n"
            "This will permanently:\n"
            "• set every game’s play count to **0**\n"
            "• clear every game’s last-played date\n"
            "• delete every entry from `/history`\n\n"
            "This is intended to remove testing data before "
            "the bot goes live.",
            view=view,
            ephemeral=True,
        )

    @resetplaycounts.error
    async def resetplaycounts_error(
        self,
        interaction: discord.Interaction,
        error,
    ):
        if isinstance(
            error,
            app_commands.errors.MissingPermissions,
        ):
            message = (
                "❌ You need moderator permissions "
                "to use this command."
            )

            if interaction.response.is_done():
                await interaction.followup.send(
                    message,
                    ephemeral=True,
                )

            else:
                await interaction.response.send_message(
                    message,
                    ephemeral=True,
                )

            return

        raise error

    @app_commands.command(
        name="syncgames",
        description=(
            "Refresh games, links and artwork from "
            "the Game Night suggestions thread"
        ),
    )
    @app_commands.checks.has_permissions(
        manage_guild=True
    )
    async def syncgames(
        self,
        interaction: discord.Interaction,
    ):
        maintenance_lock = self.bot.maintenance_lock

        if maintenance_lock.locked():
            await interaction.response.send_message(
                "⏳ Another maintenance task is already running. "
                "Please wait for the current maintenance "
                "task to finish before starting a sync.",
                ephemeral=True,
            )
            return

        async with maintenance_lock:
            try:
                async with batched_database_writes(
                    batch_size=25
                ):
                    await self._run_syncgames(
                        interaction
                    )

            finally:
                steam_entries = (
                    clear_steam_details_cache()
                )
                store_entries = (
                    await clear_store_metadata_cache()
                )
                LOGGER.debug(
                    "Released metadata caches after /syncgames: "
                    "steam=%s store=%s",
                    steam_entries,
                    store_entries,
                )

    async def _run_syncgames(
        self,
        interaction: discord.Interaction,
    ):
        sync_started_at = perf_counter()

        await interaction.response.defer(
            ephemeral=True
        )

        try:
            thread = await self.bot.fetch_channel(
                SUGGESTION_THREAD_ID
            )

        except discord.DiscordException as error:
            await interaction.followup.send(
                "❌ I could not access the Game Night "
                f"suggestions thread.\n\n`{error}`",
                ephemeral=True,
            )
            return

        found = 0
        added = 0
        updated = 0
        unchanged = 0
        unavailable = 0
        unverified = 0
        failed = 0

        wishlisted = 0
        wishlist_updated = 0
        wishlist_waiting = 0
        promoted = 0
        moved_to_wishlist = 0
        promoted_names = []

        singleplayer_added = 0
        moved_to_singleplayer = 0
        moved_to_multiplayer = 0
        singleplayer_added_names = []
        moved_to_singleplayer_names = []
        moved_to_multiplayer_names = []

        demos_upgraded = 0
        obsolete_demo_links_skipped = 0
        demo_upgrade_names = []
        obsolete_demo_names = []

        added_names = []
        updated_names = []
        wishlisted_names = []
        wishlist_updated_names = []
        moved_to_wishlist_names = []
        unavailable_names = []
        unverified_names = []
        failed_names = []

        artwork_cached = 0
        artwork_reused = 0
        artwork_missing = 0
        artwork_failed = 0
        igdb_player_limits = 0

        processed_links = set()
        message_entries = []
        artwork_jobs = []
        checked_artwork_game_ids = set()

        async for message in thread.history(
            limit=None,
            oldest_first=True,
        ):
            if message.author.bot:
                continue

            store_links = find_supported_store_links(
                message.content
            )

            found += len(
                store_links
            )

            for original_store_link in store_links:
                comparison_link = (
                    original_store_link
                    .strip()
                    .casefold()
                )

                if comparison_link in processed_links:
                    continue

                processed_links.add(
                    comparison_link
                )

                message_entries.append(
                    (
                        str(message.author),
                        tuple(message.embeds),
                        original_store_link,
                    )
                )

        game_cache_indexes = (
            _index_game_cache_records(
                await get_all_game_cache_records()
            )
        )
        replacement_indexes = (
            _index_store_replacements(
                await get_all_store_replacements(
                    store="Steam"
                )
            )
        )

        steam_semaphore = asyncio.Semaphore(
            5
        )
        epic_semaphore = asyncio.Semaphore(
            2
        )

        prefetch_links = []

        for (
            _suggested_by,
            _message_embeds,
            original_store_link,
        ) in message_entries:
            original_app_id = (
                _steam_app_id_from_url(
                    original_store_link
                )
            )

            if (
                original_app_id
                and original_app_id
                in KNOWN_OBSOLETE_DEMO_NAMES
            ):
                continue

            prefetch_links.append(
                original_store_link
            )

        prefetched_results = {}

        for batch_start in range(
            0,
            len(prefetch_links),
            SYNC_PREFETCH_BATCH_SIZE,
        ):
            batch_links = prefetch_links[
                batch_start:
                batch_start + SYNC_PREFETCH_BATCH_SIZE
            ]
            fetched_values = await asyncio.gather(
                *(
                    _prefetch_store_link(
                        store_link,
                        session=self.bot.http_session,
                        steam_semaphore=(
                            steam_semaphore
                        ),
                        epic_semaphore=(
                            epic_semaphore
                        ),
                    )
                    for store_link in batch_links
                )
            )

            prefetched_results.update(
                zip(batch_links, fetched_values)
            )

        try:
            igdb_player_limits = (
                await enrich_missing_player_metadata(
                    self.bot.http_session,
                    [
                        prefetched_result.get(
                            "game_info"
                        )
                        for prefetched_result
                        in prefetched_results.values()
                        if isinstance(
                            prefetched_result,
                            dict,
                        )
                    ],
                    force_refresh=False,
                )
            )

        except Exception:
            LOGGER.exception(
                "IGDB enrichment failed during /syncgames; "
                "store metadata was retained"
            )

        for (
            suggested_by,
            message_embeds,
            original_store_link,
        ) in message_entries:
            original_app_id = (
                _steam_app_id_from_url(
                    original_store_link
                )
            )

            if original_app_id:
                has_replacement = (
                    _has_indexed_store_replacement(
                        replacement_indexes,
                        store="Steam",
                        external_id=original_app_id,
                        store_link=original_store_link,
                    )
                )

                if has_replacement:
                    obsolete_demo_links_skipped += 1
                    continue

                known_full_game_app_id = (
                    KNOWN_OBSOLETE_DEMO_FULL_GAMES.get(
                        original_app_id
                    )
                )

                if known_full_game_app_id:
                    full_game_link = (
                        "https://store.steampowered.com/"
                        f"app/{known_full_game_app_id}/"
                    )

                    expected_full_name = (
                        KNOWN_OBSOLETE_FULL_GAME_NAMES[
                            known_full_game_app_id
                        ]
                    )

                    try:
                        full_game_info = (
                            await get_game_info_from_url(
                                self.bot.http_session,
                                full_game_link,
                                force_refresh=True,
                            )
                        )

                    except Exception as error:
                        full_game_info = None

                        LOGGER.warning(
                            "Full-game lookup failed for "
                            "obsolete demo %s: %s: %s",
                            original_app_id,
                            type(error).__name__,
                            error,
                        )

                    full_game_name = expected_full_name
                    full_game_image = None
                    full_release_date = None
                    full_max_players = None
                    full_max_players_source = None

                    if full_game_info:
                        returned_name = str(
                            full_game_info.get(
                                "name",
                                "",
                            )
                        ).strip()

                        if (
                            returned_name
                            and returned_name
                            != "Unknown Game"
                        ):
                            full_game_name = returned_name

                        full_game_image = (
                            full_game_info.get(
                                "image_url"
                            )
                        )

                        full_release_date = (
                            full_game_info.get(
                                "release_date"
                            )
                        )

                        full_max_players = (
                            full_game_info.get(
                                "max_players"
                            )
                        )
                        full_max_players_source = (
                            full_game_info.get(
                                "max_players_source"
                            )
                        )

                    full_sync_result = await sync_game(
                        name=full_game_name,
                        store_link=full_game_link,
                        source_link=full_game_link,
                        store="Steam",
                        suggested_by=suggested_by,
                        image_url=full_game_image,
                        external_id=(
                            known_full_game_app_id
                        ),
                        link_status="live",
                        http_status=200,
                        availability_status="released",
                        release_date=full_release_date,
                        coming_soon=False,
                        max_players=full_max_players,
                        max_players_source=(
                            full_max_players_source
                        ),
                    )

                    upgrade_result = (
                        await finalise_obsolete_steam_demo(
                            old_app_id=original_app_id,
                            old_store_link=(
                                original_store_link
                            ),
                            old_name=(
                                KNOWN_OBSOLETE_DEMO_NAMES[
                                    original_app_id
                                ]
                            ),
                            new_app_id=(
                                known_full_game_app_id
                            ),
                            new_store_link=(
                                full_game_link
                            ),
                            new_name=full_game_name,
                            new_image_url=(
                                full_game_image
                            ),
                            release_date=(
                                full_release_date
                            ),
                        )
                    )

                    if (
                        upgrade_result.get(
                            "status"
                        )
                        == "upgraded"
                    ):
                        demos_upgraded += 1

                        demo_upgrade_names.append(
                            (
                                KNOWN_OBSOLETE_DEMO_NAMES[
                                    original_app_id
                                ]
                            )
                            + " → "
                            + full_game_name
                        )

                        upgraded_record = (
                            await get_game_cache_record(
                                name=full_game_name,
                                store="Steam",
                                store_link=full_game_link,
                                external_id=(
                                    known_full_game_app_id
                                ),
                            )
                        )

                        if upgraded_record:
                            checked_artwork_game_ids.add(
                                int(upgraded_record["id"])
                            )
                            cache_result = (
                                await prepare_local_game_artwork(
                                    bot=self.bot,
                                    game_record=(
                                        upgraded_record
                                    ),
                                    refresh=True,
                                )
                            )

                            if cache_result == "cached":
                                artwork_cached += 1

                            elif (
                                cache_result
                                == "already_cached"
                            ):
                                artwork_reused += 1

                            elif (
                                cache_result
                                == "no_artwork"
                            ):
                                artwork_missing += 1

                            else:
                                artwork_failed += 1

                        continue

                likely_demo_link = bool(
                    re.search(
                        r"(?:^|[/_-])demo(?:[/_?&#-]|$)",
                        original_store_link,
                        flags=re.IGNORECASE,
                    )
                )

                if likely_demo_link:
                    repair_cog = self.bot.get_cog(
                        "RepairGames"
                    )

                    if repair_cog:
                        demo_result = (
                            await repair_cog
                            .auto_upgrade_demo_by_app_id(
                                original_app_id
                            )
                        )

                        if (
                            demo_result.get(
                                "status"
                            )
                            == "upgraded"
                        ):
                            demos_upgraded += 1
                            demo_upgrade_names.append(
                                _display_name_for_store_link(
                                    original_store_link,
                                    demo_result.get(
                                        "old_name"
                                    ),
                                )
                                + " → "
                                + demo_result.get(
                                    "new_name",
                                    "Full game",
                                )
                            )
                            continue

            prefetched_result = (
                prefetched_results.get(
                    original_store_link
                )
            )

            if prefetched_result is None:
                prefetched_result = (
                    await _prefetch_store_link(
                        original_store_link,
                        session=self.bot.http_session,
                        steam_semaphore=(
                            steam_semaphore
                        ),
                        epic_semaphore=(
                            epic_semaphore
                        ),
                    )
                )

            prefetch_error = (
                prefetched_result.get(
                    "error"
                )
            )

            if prefetch_error:
                failed += 1
                failed_names.append(
                    original_store_link
                )

                LOGGER.warning(
                    "Store lookup error for %s: %s: %s",
                    original_store_link,
                    type(prefetch_error).__name__,
                    prefetch_error,
                )
                continue

            game_info = (
                prefetched_result.get(
                    "game_info"
                )
            )

            if not game_info:
                failed += 1
                failed_names.append(
                    original_store_link
                )

                LOGGER.warning(
                    "No game information returned for %s",
                    original_store_link,
                )
                continue

            game_name = game_info.get(
                "name"
            )

            refreshed_store_link = game_info.get(
                "store_link"
            ) or original_store_link

            source_link = game_info.get(
                "source_link"
            ) or original_store_link

            store = game_info.get(
                "store"
            ) or "Unknown Store"

            image_url = game_info.get(
                "image_url"
            )

            external_id = game_info.get(
                "external_id"
            )

            link_status = game_info.get(
                "link_status"
            ) or "unknown"

            http_status = game_info.get(
                "http_status"
            )

            availability_status = game_info.get(
                "availability_status",
                "released",
            )

            release_date = game_info.get(
                "release_date"
            )

            coming_soon = game_info.get(
                "coming_soon",
                False,
            )

            max_players = game_info.get(
                "max_players"
            )
            max_players_source = game_info.get(
                "max_players_source"
            )
            igdb_id = game_info.get("igdb_id")
            multiplayer_support = game_info.get(
                "multiplayer_support"
            )
            genres = game_info.get("genres")
            themes = game_info.get("themes")
            game_modes = game_info.get("game_modes")

            embed_release_info = (
                release_info_from_embeds(
                    message_embeds,
                    refreshed_store_link,
                )
            )

            if (
                embed_release_info[
                    "coming_soon"
                ]
                and not game_info.get(
                    "availability_verified",
                    False,
                )
            ):
                coming_soon = True
                availability_status = (
                    "coming_soon"
                )

            if (
                not release_date
                and embed_release_info[
                    "release_date"
                ]
            ):
                release_date = (
                    embed_release_info[
                        "release_date"
                    ]
                )

            existing_record = (
                _find_indexed_game_record(
                    game_cache_indexes,
                    name=(
                        None
                        if game_name == "Unknown Game"
                        else game_name
                    ),
                    store=store,
                    store_links=(
                        refreshed_store_link,
                        source_link,
                        original_store_link,
                    ),
                    external_id=external_id,
                )
            )

            availability_verified = bool(
                game_info.get(
                    "availability_verified",
                    False,
                )
            )

            if (
                store == "Steam"
                and existing_record
                and existing_record.get(
                    "availability_status"
                ) == "coming_soon"
                and not availability_verified
            ):
                # Never promote a wishlist game based on
                # incomplete or contradictory Steam data.
                availability_status = "coming_soon"
                coming_soon = True
                release_date = (
                    release_date
                    or existing_record.get(
                        "release_date"
                    )
                )

            if (
                store == "Steam"
                and existing_record
                and (
                    not game_name
                    or game_name == "Unknown Game"
                    or str(game_name).startswith(
                        ("http://", "https://")
                    )
                )
            ):
                game_name = (
                    existing_record.get(
                        "name"
                    )
                    or game_name
                )

            epic_verification_status = (
                game_info.get(
                    "verification_status"
                )
                if store
                == "Epic Games Store"
                else None
            )

            if epic_verification_status in {
                "blocked",
                "complete",
                "unverified",
            }:
                epic_embed_info = (
                    epic_info_from_embeds(
                        message_embeds,
                        original_store_link,
                    )
                )

                if epic_embed_info.get(
                    "name"
                ):
                    game_name = (
                        epic_embed_info[
                            "name"
                        ]
                    )

                elif existing_record:
                    game_name = (
                        existing_record.get(
                            "name"
                        )
                        or game_name
                    )

                if (
                    not image_url
                    and epic_embed_info.get(
                        "image_url"
                    )
                ):
                    image_url = (
                        epic_embed_info[
                            "image_url"
                        ]
                    )

                elif (
                    not image_url
                    and existing_record
                ):
                    image_url = (
                        existing_record.get(
                            "image_url"
                        )
                    )

                if existing_record:
                    refreshed_store_link = (
                        existing_record.get(
                            "store_link"
                        )
                        or refreshed_store_link
                    )

                    link_status = (
                        existing_record.get(
                            "link_status"
                        )
                        or "live"
                    )

                    http_status = (
                        existing_record.get(
                            "http_status"
                        )
                    )

                    availability_status = (
                        existing_record.get(
                            "availability_status"
                        )
                        or availability_status
                    )

                    release_date = (
                        existing_record.get(
                            "release_date"
                        )
                        or release_date
                    )

                    coming_soon = bool(
                        existing_record.get(
                            "coming_soon"
                        )
                    )

                    max_players = (
                        existing_record.get(
                            "max_players"
                        )
                        or max_players
                    )
                    max_players_source = (
                        existing_record.get(
                            "max_players_source"
                        )
                        or max_players_source
                    )

                else:
                    link_status = "live"

                if epic_verification_status == "complete":
                    link_status = "live"

            report_name = (
                existing_record.get(
                    "name"
                )
                if existing_record
                else game_name
            )

            report_name = (
                _display_name_for_store_link(
                    original_store_link,
                    report_name,
                )
            )

            invalid_game_name = bool(
                not game_name
                or game_name == "Unknown Game"
                or str(game_name).startswith(
                    ("http://", "https://")
                )
            )

            if invalid_game_name:
                failed += 1
                failed_names.append(
                    original_store_link
                )

                LOGGER.warning(
                    "Refusing to add or rename a game from "
                    "unverified fallback data: %s",
                    original_store_link,
                )
                continue

            try:
                result = await sync_game(
                    name=game_name,
                    store_link=refreshed_store_link,
                    source_link=source_link,
                    store=store,
                    suggested_by=suggested_by,
                    image_url=image_url,
                    external_id=external_id,
                    link_status=link_status,
                    http_status=http_status,
                    availability_status=(
                        availability_status
                    ),
                    release_date=release_date,
                    coming_soon=coming_soon,
                    max_players=max_players,
                    max_players_source=(
                        max_players_source
                    ),
                    igdb_id=igdb_id,
                    multiplayer_support=(
                        multiplayer_support
                    ),
                    genres=genres,
                    themes=themes,
                    game_modes=game_modes,
                )

            except Exception:
                failed += 1
                failed_names.append(
                    report_name
                )

                LOGGER.exception(
                    "Database error while syncing %s",
                    original_store_link,
                )
                continue

            if result == "added":
                added += 1
                added_names.append(
                    report_name
                )

            elif result == "singleplayer_added":
                singleplayer_added += 1
                singleplayer_added_names.append(
                    report_name
                )

            elif result == "moved_to_singleplayer":
                moved_to_singleplayer += 1
                moved_to_singleplayer_names.append(
                    report_name
                )

            elif result == "moved_to_multiplayer":
                moved_to_multiplayer += 1
                moved_to_multiplayer_names.append(
                    report_name
                )

            elif result == "updated":
                updated += 1
                updated_names.append(
                    report_name
                )

            elif result == "unchanged":
                unchanged += 1

            elif result == "wishlisted":
                wishlisted += 1
                wishlisted_names.append(
                    report_name
                )

            elif result == "wishlist_updated":
                wishlist_updated += 1
                wishlist_updated_names.append(
                    report_name
                )

            elif result == "moved_to_wishlist":
                moved_to_wishlist += 1
                moved_to_wishlist_names.append(
                    report_name
                )

            elif result == "wishlist_unchanged":
                wishlist_waiting += 1

            elif result == "promoted":
                promoted += 1
                promoted_names.append(
                    report_name
                )

            elif result == "unavailable":
                unavailable += 1
                unavailable_names.append(
                    report_name
                )

            else:
                failed += 1

                LOGGER.error(
                    "Unknown database result %r for %s",
                    result,
                    original_store_link,
                )
                continue

            epic_incomplete = bool(
                store == "Epic Games Store"
                and epic_verification_status
                in {
                    "blocked",
                    "unverified",
                }
            )

            if epic_incomplete or (
                link_status == "unknown"
                and not epic_verification_status
            ):
                unverified += 1
                unverified_names.append(
                    _verification_label(
                        report_name,
                        store,
                        http_status,
                        game_info.get(
                            "error"
                        ),
                    )
                )

            if result == "unavailable":
                continue

            if existing_record:
                game_record = {
                    **existing_record,
                    "name": (
                        game_name
                        or existing_record.get("name")
                    ),
                    "store_link": (
                        refreshed_store_link
                        or existing_record.get(
                            "store_link"
                        )
                    ),
                    "store": (
                        store
                        or existing_record.get("store")
                    ),
                    "image_url": (
                        image_url
                        or existing_record.get(
                            "image_url"
                        )
                    ),
                    "external_id": (
                        external_id
                        or existing_record.get(
                            "external_id"
                        )
                    ),
                }

            else:
                # Rows added during this sync were not present
                # in the one-time cache snapshot, so only those
                # need a follow-up lookup for their database ID.
                game_record = await get_game_cache_record(
                    name=game_name,
                    store=store,
                    store_link=refreshed_store_link,
                    external_id=external_id,
                )

            if not game_record:
                artwork_failed += 1
                continue

            artwork_jobs.append(
                (
                    game_record,
                    result not in {
                        "unchanged",
                        "wishlist_unchanged",
                    },
                )
            )
            checked_artwork_game_ids.add(
                int(game_record["id"])
            )

        if artwork_jobs:
            local_artwork_limit = asyncio.Semaphore(
                5
            )

            async def prepare_artwork(
                game_record: dict,
                refresh: bool,
            ) -> str:
                async with local_artwork_limit:
                    return (
                        await prepare_local_game_artwork(
                            bot=self.bot,
                            game_record=game_record,
                            refresh=refresh,
                        )
                    )

            artwork_results = await asyncio.gather(
                *(
                    prepare_artwork(
                        game_record,
                        refresh,
                    )
                    for game_record, refresh in artwork_jobs
                )
            )

            for cache_result in artwork_results:
                if cache_result == "cached":
                    artwork_cached += 1

                elif cache_result == "already_cached":
                    artwork_reused += 1

                elif cache_result == "no_artwork":
                    artwork_missing += 1

                else:
                    artwork_failed += 1

        artwork_maintenance = await maintain_local_artwork(
            bot=self.bot,
            game_records=await get_all_artwork_records(),
            checked_game_ids=checked_artwork_game_ids,
        )

        LOGGER.info(
            "Local artwork maintenance complete: repaired=%s "
            "corrupt=%s orphaned=%s cards_pruned=%s",
            artwork_maintenance["repaired"],
            artwork_maintenance["corrupt"],
            artwork_maintenance["orphaned"],
            artwork_maintenance["cards_pruned"],
        )

        unique_processed = len(
            processed_links
        )

        duplicate_count = max(
            found - unique_processed,
            0,
        )

        sync_duration = (
            perf_counter()
            - sync_started_at
        )

        summary_lines = [
            "## 🔍 Game Sync Complete",
            "",
            f"🔗 Store links found: **{found}**",
        ]

        summary_items = (
            (
                duplicate_count,
                "🧹 Duplicate links skipped",
            ),
            (
                added,
                "🎮 New games added to multiplayer wheel",
            ),
            (
                singleplayer_added,
                "🧍 New games added to Single Player wheel",
            ),
            (
                moved_to_singleplayer,
                "🧍 Moved to Single Player wheel",
            ),
            (
                moved_to_multiplayer,
                "👥 Moved back to multiplayer wheel",
            ),
            (
                wishlisted,
                "🌠 New wishlist games",
            ),
            (
                wishlist_updated,
                "📝 Wishlist entries updated",
            ),
            (
                wishlist_waiting,
                "⏳ Still coming soon",
            ),
            (
                moved_to_wishlist,
                "↩️ Moved onto wishlist",
            ),
            (
                promoted,
                "🎉 Released and moved to wheel",
            ),
            (
                demos_upgraded,
                "⬆️ Demos upgraded to full games",
            ),
            (
                obsolete_demo_links_skipped,
                "⏭️ Obsolete demo links skipped",
            ),
            (
                updated,
                "♻️ Existing wheel games updated",
            ),
            (
                igdb_player_limits,
                "👥 Player limits filled by IGDB",
            ),
            (
                unchanged,
                "✅ Already current",
            ),
            (
                unavailable,
                "🚫 Confirmed unavailable",
            ),
            (
                unverified,
                "⚠️ Could not fully verify",
            ),
            (
                failed,
                "❌ Unexpected failures",
            ),
        )

        for value, label in summary_items:
            if value:
                summary_lines.append(
                    f"{label}: **{value}**"
                )

        artwork_items = (
            (
                artwork_cached,
                "📥 Downloaded locally",
            ),
            (
                artwork_reused,
                "⚡ Existing local artwork reused",
            ),
            (
                artwork_missing,
                "➖ No artwork available",
            ),
            (
                artwork_failed,
                "❌ Local artwork failures",
            ),
        )

        visible_artwork_items = [
            (
                value,
                label,
            )
            for value, label in artwork_items
            if value
        ]

        if visible_artwork_items:
            summary_lines.extend(
                [
                    "",
                    "## 🖼️ Local Artwork",
                    "",
                ]
            )

            for (
                value,
                label,
            ) in visible_artwork_items:
                summary_lines.append(
                    f"{label}: **{value}**"
                )

        summary_lines.extend(
            [
                "",
                (
                    f"*{found} links scanned • "
                    f"{unique_processed} unique links • "
                    f"{sync_duration:.1f}s*"
                ),
            ]
        )

        await interaction.followup.send(
            "\n".join(
                summary_lines
            ),
            ephemeral=True,
        )

        detail_sections = [
            section
            for section in (
                _format_name_section(
                    "🎮 Added to Multiplayer Wheel",
                    added_names,
                ),
                _format_name_section(
                    "🧍 Added to Single Player Wheel",
                    singleplayer_added_names,
                ),
                _format_name_section(
                    "🧍 Moved to Single Player Wheel",
                    moved_to_singleplayer_names,
                ),
                _format_name_section(
                    "👥 Moved Back to Multiplayer Wheel",
                    moved_to_multiplayer_names,
                ),
                _format_name_section(
                    "🌠 Added to Wishlist",
                    wishlisted_names,
                ),
                _format_name_section(
                    "↩️ Moved to Wishlist",
                    moved_to_wishlist_names,
                ),
                _format_name_section(
                    "🎉 Released and Moved to Wheel",
                    promoted_names,
                ),
                _format_name_section(
                    "⬆️ Demos Upgraded to Full Games",
                    demo_upgrade_names,
                ),
                _format_name_section(
                    "♻️ Wheel Metadata Updated",
                    updated_names,
                ),
                _format_name_section(
                    "📝 Wishlist Metadata Updated",
                    wishlist_updated_names,
                ),
                _format_name_section(
                    "🚫 Confirmed Unavailable",
                    unavailable_names,
                ),
                _format_name_section(
                    "⚠️ Could Not Fully Verify",
                    unverified_names,
                ),
                _format_name_section(
                    "❌ Unexpected Failures",
                    failed_names,
                ),
            )
            if section
        ]

        if detail_sections:
            for report_message in _split_report_sections(
                detail_sections
            ):
                await interaction.followup.send(
                    report_message,
                    ephemeral=True,
                )

    @syncgames.error
    async def syncgames_error(
        self,
        interaction: discord.Interaction,
        error,
    ):
        if isinstance(
            error,
            app_commands.errors.MissingPermissions,
        ):
            message = (
                "❌ You need moderator permissions "
                "to use this command."
            )

            if interaction.response.is_done():
                await interaction.followup.send(
                    message,
                    ephemeral=True,
                )

            else:
                await interaction.response.send_message(
                    message,
                    ephemeral=True,
                )

            return

        raise error

    @app_commands.command(
        name="deletegame",
        description=(
            "Delete an incorrectly imported game"
        ),
    )
    @app_commands.describe(
        game_name=(
            "The exact name of the game to delete"
        )
    )
    @app_commands.checks.has_permissions(
        manage_guild=True
    )
    async def deletegame(
        self,
        interaction: discord.Interaction,
        game_name: str,
    ):
        await interaction.response.defer(
            ephemeral=True
        )

        try:
            game_record = await get_game_cache_record(
                name=game_name
            )

            deleted = await delete_game_by_name(
                game_name
            )

            if deleted and game_record:
                await delete_local_game_artwork(
                    game_record["id"],
                )

        except Exception:
            LOGGER.exception(
                "Failed to delete game %r",
                game_name,
            )

            await interaction.followup.send(
                "❌ The game could not be deleted. Check "
                "the hosting console for details.",
                ephemeral=True,
            )
            return

        if not deleted:
            await interaction.followup.send(
                f"❌ I could not find a game named "
                f"**{game_name}**.\n\n"
                "The name must exactly match the name "
                "shown by `/games`.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"✅ Deleted **{game_name}** from the "
            "game database and removed its associated "
            "play history.",
            ephemeral=True,
        )

    @deletegame.error
    async def deletegame_error(
        self,
        interaction: discord.Interaction,
        error,
    ):
        if isinstance(
            error,
            app_commands.errors.MissingPermissions,
        ):
            message = (
                "❌ You need moderator permissions "
                "to use this command."
            )

            if interaction.response.is_done():
                await interaction.followup.send(
                    message,
                    ephemeral=True,
                )

            else:
                await interaction.response.send_message(
                    message,
                    ephemeral=True,
                )

            return

        raise error


async def setup(bot):
    await bot.add_cog(
        Admin(bot)
    )
