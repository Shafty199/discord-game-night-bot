import logging

from discord.ext import commands, tasks

from database.database import (
    create_automatic_backup,
)


LOGGER = logging.getLogger(__name__)

BACKUP_CHECK_INTERVAL_HOURS = 6
BACKUP_MINIMUM_INTERVAL_HOURS = 24
AUTOMATIC_BACKUP_RETENTION = 7


class Maintenance(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
    ):
        self.bot = bot

    async def cog_load(self) -> None:
        self.automatic_backup.start()

    async def cog_unload(self) -> None:
        self.automatic_backup.cancel()

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


async def setup(
    bot: commands.Bot,
) -> None:
    await bot.add_cog(
        Maintenance(bot)
    )
