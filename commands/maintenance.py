import asyncio
import logging

import discord
from discord.ext import commands, tasks

from database.database import (
    create_automatic_backup,
    get_games_missing_igdb_metadata,
    save_refreshed_igdb_metadata,
)
from settings import LOGGING_CHANNEL_ID
from utils.igdb import (
    enrich_missing_player_metadata,
    igdb_is_configured,
)


LOGGER = logging.getLogger(__name__)

BACKUP_CHECK_INTERVAL_HOURS = 6
BACKUP_MINIMUM_INTERVAL_HOURS = 24
AUTOMATIC_BACKUP_RETENTION = 7
IGDB_RETRY_INTERVAL_HOURS = 24
IGDB_RETRY_STARTUP_DELAY_SECONDS = 300


class Maintenance(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
    ):
        self.bot = bot

    async def cog_load(self) -> None:
        self.automatic_backup.start()
        self.daily_igdb_retry.start()

    async def cog_unload(self) -> None:
        self.automatic_backup.cancel()
        self.daily_igdb_retry.cancel()

    async def _retry_missing_igdb_metadata(self) -> dict:
        if not igdb_is_configured():
            return {
                "status": "disabled",
                "checked": 0,
                "updated": 0,
            }

        session = getattr(
            self.bot,
            "http_session",
            None,
        )

        if session is None or session.closed:
            return {
                "status": "unavailable",
                "checked": 0,
                "updated": 0,
            }

        games = await get_games_missing_igdb_metadata()

        if not games:
            return {
                "status": "complete",
                "checked": 0,
                "updated": 0,
            }

        await enrich_missing_player_metadata(
            session,
            games,
            force_refresh=True,
        )

        updated_games = []

        for game in games:
            if game.get("igdb_id") is None:
                continue

            if await save_refreshed_igdb_metadata(
                game["id"],
                game,
            ):
                updated_games.append(game)

        return {
            "status": "checked",
            "checked": len(games),
            "updated": len(updated_games),
            "updated_games": updated_games,
        }

    async def _announce_igdb_updates(
        self,
        games: list[dict],
    ) -> None:
        if not games or LOGGING_CHANNEL_ID is None:
            return

        channel = self.bot.get_channel(
            LOGGING_CHANNEL_ID
        )

        if channel is None:
            try:
                channel = await self.bot.fetch_channel(
                    LOGGING_CHANNEL_ID
                )

            except discord.HTTPException:
                LOGGER.exception(
                    "Could not fetch the logging channel for "
                    "the daily IGDB update"
                )
                return

        visible_games = games[:10]
        lines = []

        for game in visible_games:
            name = discord.utils.escape_markdown(
                str(game.get("name") or "Unknown game")
            )
            max_players = game.get("max_players")
            capacity = (
                f"up to {max_players} players"
                if max_players is not None
                else "player count still unverified"
            )
            lines.append(f"• **{name}** — {capacity}")

        hidden_count = len(games) - len(visible_games)

        if hidden_count:
            lines.append(
                f"• …and {hidden_count} more"
            )

        game_word = "game" if len(games) == 1 else "games"

        try:
            await channel.send(
                "✅ **Daily IGDB repair** updated "
                f"**{len(games)} {game_word}**:\n"
                + "\n".join(lines),
                allowed_mentions=(
                    discord.AllowedMentions.none()
                ),
            )

        except discord.HTTPException:
            LOGGER.exception(
                "Could not announce daily IGDB updates "
                "in the logging channel"
            )

    @tasks.loop(
        hours=BACKUP_CHECK_INTERVAL_HOURS
    )
    async def automatic_backup(self) -> None:
        try:
            maintenance_lock = getattr(
                self.bot,
                "maintenance_lock",
                None,
            )

            if maintenance_lock is None:
                result = await create_automatic_backup(
                    minimum_interval_hours=(
                        BACKUP_MINIMUM_INTERVAL_HOURS
                    ),
                    retention=AUTOMATIC_BACKUP_RETENTION,
                )

            else:
                async with maintenance_lock:
                    result = await create_automatic_backup(
                        minimum_interval_hours=(
                            BACKUP_MINIMUM_INTERVAL_HOURS
                        ),
                        retention=(
                            AUTOMATIC_BACKUP_RETENTION
                        ),
                    )

        except Exception:
            # Keep the recurring task alive after a transient disk or
            # SQLite error; the next scheduled check will retry it.
            LOGGER.exception(
                "Automatic database backup failed"
            )
            return

        if result["status"] == "created":
            LOGGER.info(
                "Automatic database backup created at %s "
                "(old backups removed: %s)",
                result["path"],
                result["removed"],
            )

        else:
            LOGGER.debug(
                "Automatic database backup is not due; "
                "newest snapshot: %s",
                result["path"],
            )

    @automatic_backup.before_loop
    async def before_automatic_backup(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(hours=IGDB_RETRY_INTERVAL_HOURS)
    async def daily_igdb_retry(self) -> None:
        try:
            maintenance_lock = getattr(
                self.bot,
                "maintenance_lock",
                None,
            )

            if maintenance_lock is None:
                result = (
                    await self._retry_missing_igdb_metadata()
                )

            else:
                async with maintenance_lock:
                    result = (
                        await self._retry_missing_igdb_metadata()
                    )

        except Exception:
            LOGGER.exception(
                "Daily missing IGDB metadata retry failed"
            )
            return

        if result["status"] == "checked":
            LOGGER.info(
                "Daily IGDB retry complete: checked=%s "
                "updated=%s",
                result["checked"],
                result["updated"],
            )
            await self._announce_igdb_updates(
                result["updated_games"]
            )

        elif result["status"] == "complete":
            LOGGER.debug(
                "Daily IGDB retry found no incomplete games"
            )

    @daily_igdb_retry.before_loop
    async def before_daily_igdb_retry(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(
            IGDB_RETRY_STARTUP_DELAY_SECONDS
        )


async def setup(
    bot: commands.Bot,
) -> None:
    await bot.add_cog(
        Maintenance(bot)
    )
