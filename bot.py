import asyncio
import contextlib
import hashlib
import logging
import os
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from database.database import (
    add_game,
    close_database,
    get_game_cache_record,
    setup_database,
)
from settings import (
    DISCORD_TOKEN,
    EPIC_EMOJI,
    STEAM_EMOJI,
    SUGGESTION_THREAD_ID,
)
from ui.embeds import format_sale_text
from utils.artwork_cache import (
    cleanup_temporary_artwork_files,
    flush_artwork_manifest,
    prepare_local_game_artwork,
    remove_redundant_source_artwork,
)
from utils.metadata import (
    epic_info_from_embeds,
    release_info_from_embeds,
)
from utils.igdb import (
    enrich_missing_player_metadata,
    igdb_is_configured,
)
from utils.store import (
    clear_store_metadata_cache,
    find_supported_store_links,
    get_game_info_from_url,
)
from utils.steam_api import clear_steam_details_cache


logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | %(levelname)s | "
        "%(name)s | %(message)s"
    ),
    datefmt="%Y-%m-%d %H:%M:%S",
)

logging.getLogger(
    "discord.http"
).setLevel(
    logging.WARNING
)

LOGGER = logging.getLogger(__name__)


TOKEN = DISCORD_TOKEN
COMMAND_SYNC_HASH_PATH = (
    Path(__file__).resolve().parent
    / "database"
    / "command-schema.sha256"
)


def _command_source_hash() -> str:
    project_directory = Path(__file__).resolve().parent
    command_files = [
        project_directory / "bot.py",
        *sorted(
            (project_directory / "commands").glob("*.py")
        ),
    ]
    digest = hashlib.sha256()

    for source_path in command_files:
        digest.update(
            source_path.relative_to(
                project_directory
            ).as_posix().encode("utf-8")
        )
        digest.update(b"\0")
        digest.update(source_path.read_bytes())
        digest.update(b"\0")

    return digest.hexdigest()


def _read_command_sync_hash() -> str:
    try:
        return COMMAND_SYNC_HASH_PATH.read_text(
            encoding="utf-8"
        ).strip()
    except OSError:
        return ""


def _write_command_sync_hash(command_hash: str) -> None:
    COMMAND_SYNC_HASH_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    temporary_path = COMMAND_SYNC_HASH_PATH.with_suffix(
        ".tmp"
    )

    try:
        temporary_path.write_text(
            command_hash,
            encoding="utf-8",
        )
        os.replace(
            temporary_path,
            COMMAND_SYNC_HASH_PATH,
        )
    finally:
        temporary_path.unlink(missing_ok=True)


intents = discord.Intents.default()
intents.message_content = True


SUGGESTION_QUEUE_MAX_SIZE = 50


class GameNightBot(commands.Bot):
    http_session: aiohttp.ClientSession | None = None
    maintenance_lock: asyncio.Lock | None = None
    suggestion_queue: asyncio.Queue | None = None
    suggestion_worker_task: asyncio.Task | None = None

    async def setup_hook(self):
        asyncio.get_running_loop().set_exception_handler(
            self._handle_async_exception
        )

        self.maintenance_lock = asyncio.Lock()
        self.suggestion_queue = asyncio.Queue(
            maxsize=SUGGESTION_QUEUE_MAX_SIZE
        )
        self._queued_suggestion_ids = set()
        self.suggestion_worker_task = asyncio.create_task(
            self._suggestion_worker(),
            name="suggestion-processing-worker",
        )

        # One shared aiohttp session for the whole bot's
        # lifetime, reused by every store/artwork lookup
        # instead of each call opening its own session
        # (and paying for a fresh TCP/TLS handshake).
        self.http_session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(
                limit=20,
                limit_per_host=8,
                ttl_dns_cache=300,
            )
        )

        LOGGER.info(
            "IGDB player metadata fallback is %s",
            (
                "enabled"
                if igdb_is_configured()
                else "disabled (credentials not configured)"
            ),
        )

        await setup_database()
        removed_temporary_files = (
            await cleanup_temporary_artwork_files()
        )

        if removed_temporary_files:
            LOGGER.info(
                "Removed %s temporary cache/spin files "
                "left by an earlier shutdown",
                removed_temporary_files,
            )

        removed_source_artwork = (
            await remove_redundant_source_artwork()
        )

        if removed_source_artwork:
            LOGGER.info(
                "Removed %s redundant source artwork files; "
                "rendered cards are now the persistent cache",
                removed_source_artwork,
            )

        extensions = (
            "commands.games",
            "commands.admin",
            "commands.history",
            "commands.stats",
            "commands.repair",
            "commands.wishlist",
            "commands.maintenance",
        )

        for extension in extensions:
            try:
                await self.load_extension(
                    extension
                )

            except Exception:
                LOGGER.exception(
                    "Failed to load extension %s",
                    extension,
                )
                raise

        command_names = sorted(
            command.name
            for command in self.tree.get_commands()
        )

        LOGGER.info(
            "Loaded slash commands: %s",
            ", ".join(command_names),
        )

        command_hash = await asyncio.to_thread(
            _command_source_hash
        )
        previous_hash = await asyncio.to_thread(
            _read_command_sync_hash
        )

        if command_hash != previous_hash:
            synced = await self.tree.sync()
            await asyncio.to_thread(
                _write_command_sync_hash,
                command_hash,
            )
            LOGGER.info(
                "Synced %s slash commands after a command change",
                len(synced),
            )
        else:
            LOGGER.info(
                "Slash-command definitions are unchanged; "
                "Discord sync skipped"
            )

    def queue_suggestion(self, message) -> bool:
        message_key = (
            int(message.channel.id),
            int(message.id),
        )

        if message_key in self._queued_suggestion_ids:
            return False

        if self.suggestion_queue.full():
            LOGGER.warning(
                "Suggestion queue is full; message %s "
                "was not queued",
                message.id,
            )
            return False

        self._queued_suggestion_ids.add(message_key)

        try:
            self.suggestion_queue.put_nowait(
                (message_key, message)
            )
        except asyncio.QueueFull:
            self._queued_suggestion_ids.discard(
                message_key
            )
            LOGGER.warning(
                "Suggestion queue filled while adding "
                "message %s",
                message.id,
            )
            return False

        return True

    async def _suggestion_worker(self) -> None:
        while True:
            message_key, message = (
                await self.suggestion_queue.get()
            )

            try:
                await _process_suggestion_message(message)

            except asyncio.CancelledError:
                raise

            except Exception:
                LOGGER.exception(
                    "Unexpected suggestion-processing failure "
                    "for message %s",
                    message.id,
                )

            finally:
                self._queued_suggestion_ids.discard(
                    message_key
                )
                self.suggestion_queue.task_done()

    @staticmethod
    def _handle_async_exception(
        loop: asyncio.AbstractEventLoop,
        context: dict,
    ) -> None:
        exception = context.get(
            "exception"
        )

        if exception is not None:
            LOGGER.error(
                "Unhandled background task exception: %s",
                context.get(
                    "message",
                    "No additional context",
                ),
                exc_info=(
                    type(exception),
                    exception,
                    exception.__traceback__,
                ),
            )
            return

        LOGGER.error(
            "Unhandled asyncio error: %s",
            context.get(
                "message",
                context,
            ),
        )

    async def close(self):
        if self.suggestion_worker_task is not None:
            self.suggestion_worker_task.cancel()

            with contextlib.suppress(
                asyncio.CancelledError
            ):
                await self.suggestion_worker_task

        try:
            await flush_artwork_manifest()

        except Exception:
            LOGGER.exception(
                "Could not flush the artwork manifest "
                "during shutdown"
            )

        try:
            await super().close()

        finally:
            if (
                self.http_session is not None
                and not self.http_session.closed
            ):
                await self.http_session.close()

            await close_database()
            clear_steam_details_cache()
            await clear_store_metadata_cache()


bot = GameNightBot(
    command_prefix="!",
    intents=intents,
    max_messages=100,
)


@bot.event
async def on_ready():
    LOGGER.info(
        "Logged in as %s; Game Night Bot is online",
        bot.user,
    )

@bot.event
async def on_error(
    event_method: str,
    *args,
    **kwargs,
):
    LOGGER.exception(
        "Unhandled exception in Discord event %s",
        event_method,
    )


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    original_error = (
        error.original
        if isinstance(
            error,
            app_commands.CommandInvokeError,
        )
        else error
    )

    if isinstance(
        original_error,
        app_commands.MissingPermissions,
    ):
        message = (
            "❌ You need moderator permissions "
            "to use this command."
        )

    elif isinstance(
        original_error,
        app_commands.CheckFailure,
    ):
        message = (
            "❌ You cannot use this command here."
        )

    else:
        command_name = (
            interaction.command.qualified_name
            if interaction.command
            else "unknown"
        )

        LOGGER.error(
            "Unhandled slash-command error for /%s "
            "(user=%s guild=%s channel=%s)",
            command_name,
            interaction.user.id,
            interaction.guild_id,
            interaction.channel_id,
            exc_info=(
                type(original_error),
                original_error,
                original_error.__traceback__,
            ),
        )

        message = (
            "❌ Something went wrong while running that "
            "command. The error was logged in the hosting "
            "console."
        )

    try:
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

    except discord.DiscordException:
        LOGGER.warning(
            "Could not deliver slash-command error response",
            exc_info=True,
        )


def get_store_display(
    store,
) -> str:
    cleaned_store = str(
        store or "Unknown Store"
    ).strip()

    normalised_store = cleaned_store.casefold()

    if "steam" in normalised_store:
        return (
            f"{STEAM_EMOJI} {cleaned_store}"
        )

    if "epic" in normalised_store:
        return (
            f"{EPIC_EMOJI} {cleaned_store}"
        )

    return f"🎮 {cleaned_store}"


async def _process_suggestion_message(
    message,
):
    if message.author.bot:
        return

    if message.channel.id == SUGGESTION_THREAD_ID:
        store_links = find_supported_store_links(
            message.content
        )

        checked_message = message
        lookup_results = {}

        if store_links:
            lookup_started_at = (
                asyncio.get_running_loop().time()
            )

            lookup_tasks = {
                store_link: asyncio.create_task(
                    get_game_info_from_url(
                        bot.http_session,
                        store_link,
                    )
                )
                for store_link in store_links
            }

            fetched_results = await asyncio.gather(
                *lookup_tasks.values(),
                return_exceptions=True,
            )

            try:
                await enrich_missing_player_metadata(
                    bot.http_session,
                    fetched_results,
                )

            except Exception:
                LOGGER.exception(
                    "IGDB enrichment failed for a new "
                    "suggestion; store metadata was retained"
                )

            lookup_results = dict(
                zip(
                    lookup_tasks.keys(),
                    fetched_results,
                )
            )

            needs_discord_metadata = any(
                isinstance(result, dict)
                and result.get("store")
                == "Epic Games Store"
                and result.get("verification_status")
                in {
                    "blocked",
                    "unverified",
                }
                for result in fetched_results
            )

            if needs_discord_metadata:
                remaining_embed_wait = max(
                    0.0,
                    1.5
                    - (
                        asyncio.get_running_loop().time()
                        - lookup_started_at
                    ),
                )

                if remaining_embed_wait:
                    await asyncio.sleep(
                        remaining_embed_wait
                    )

                try:
                    checked_message = (
                        await message.channel.fetch_message(
                            message.id
                        )
                    )

                except discord.DiscordException:
                    checked_message = message

        added_games = []
        wishlisted_games = []
        existing_games = []
        failed_links = []

        for store_link in store_links:
            game_info = lookup_results.get(
                store_link
            )

            if isinstance(
                game_info,
                BaseException,
            ):
                if isinstance(
                    game_info,
                    asyncio.CancelledError,
                ):
                    raise game_info

                LOGGER.error(
                    "Store lookup failed for new suggestion "
                    "%s: %s: %s",
                    store_link,
                    type(game_info).__name__,
                    game_info,
                )

                failed_links.append(
                    store_link
                )
                continue

            if not game_info:
                failed_links.append(
                    store_link
                )
                continue

            embed_release_info = (
                release_info_from_embeds(
                    checked_message.embeds,
                    store_link,
                )
            )

            if (
                game_info.get(
                    "store"
                )
                == "Epic Games Store"
                and game_info.get(
                    "verification_status"
                )
                in {
                    "blocked",
                    "complete",
                    "unverified",
                }
            ):
                epic_embed_info = (
                    epic_info_from_embeds(
                        checked_message.embeds,
                        store_link,
                    )
                )

                existing_epic_record = (
                    await get_game_cache_record(
                        name=None,
                        store="Epic Games Store",
                        store_link=game_info.get(
                            "store_link"
                        ),
                        external_id=game_info.get(
                            "external_id"
                        ),
                    )
                )

                if epic_embed_info.get(
                    "name"
                ):
                    game_info[
                        "name"
                    ] = epic_embed_info[
                        "name"
                    ]

                elif existing_epic_record:
                    game_info[
                        "name"
                    ] = (
                        existing_epic_record.get(
                            "name"
                        )
                        or game_info.get(
                            "name"
                        )
                    )

                if (
                    not game_info.get(
                        "image_url"
                    )
                    and epic_embed_info.get(
                        "image_url"
                    )
                ):
                    game_info[
                        "image_url"
                    ] = epic_embed_info[
                        "image_url"
                    ]

                elif (
                    not game_info.get(
                        "image_url"
                    )
                    and existing_epic_record
                ):
                    game_info[
                        "image_url"
                    ] = (
                        existing_epic_record.get(
                            "image_url"
                        )
                    )

                if existing_epic_record:
                    game_info[
                        "store_link"
                    ] = (
                        existing_epic_record.get(
                            "store_link"
                        )
                        or game_info.get(
                            "store_link"
                        )
                    )

                    game_info[
                        "link_status"
                    ] = (
                        existing_epic_record.get(
                            "link_status"
                        )
                        or "live"
                    )

                    game_info[
                        "http_status"
                    ] = (
                        existing_epic_record.get(
                            "http_status"
                        )
                    )

                    game_info[
                        "availability_status"
                    ] = (
                        existing_epic_record.get(
                            "availability_status"
                        )
                        or "released"
                    )

                    game_info[
                        "release_date"
                    ] = (
                        existing_epic_record.get(
                            "release_date"
                        )
                    )

                    game_info[
                        "coming_soon"
                    ] = bool(
                        existing_epic_record.get(
                            "coming_soon"
                        )
                    )

                elif (
                    epic_embed_info.get(
                        "name"
                    )
                    or epic_embed_info.get(
                        "image_url"
                    )
                ):
                    game_info[
                        "link_status"
                    ] = "live"

                if game_info.get(
                    "verification_status"
                ) == "complete":
                    game_info[
                        "link_status"
                    ] = "live"

            if (
                embed_release_info[
                    "coming_soon"
                ]
                and not game_info.get(
                    "availability_verified",
                    False,
                )
            ):
                game_info[
                    "coming_soon"
                ] = True
                game_info[
                    "availability_status"
                ] = "coming_soon"
                game_info[
                    "availability_verified"
                ] = True

            if (
                not game_info.get(
                    "release_date"
                )
                and embed_release_info[
                    "release_date"
                ]
            ):
                game_info[
                    "release_date"
                ] = embed_release_info[
                    "release_date"
                ]

            result = await add_game(
                name=game_info["name"],
                store_link=game_info["store_link"],
                store=game_info["store"],
                suggested_by=str(
                    message.author
                ),
                image_url=game_info.get(
                    "image_url"
                ),
                source_link=game_info.get(
                    "source_link"
                ),
                external_id=game_info.get(
                    "external_id"
                ),
                link_status=game_info.get(
                    "link_status",
                    "unknown",
                ),
                http_status=game_info.get(
                    "http_status"
                ),
                availability_status=game_info.get(
                    "availability_status",
                    "released",
                ),
                release_date=game_info.get(
                    "release_date"
                ),
                coming_soon=game_info.get(
                    "coming_soon",
                    False,
                ),
                max_players=game_info.get(
                    "max_players"
                ),
                max_players_source=game_info.get(
                    "max_players_source"
                ),
                return_status=True,
            )

            game_record = await get_game_cache_record(
                name=game_info.get(
                    "name"
                ),
                store=game_info.get(
                    "store"
                ),
                store_link=game_info.get(
                    "store_link"
                ),
                external_id=game_info.get(
                    "external_id"
                ),
            )

            cache_result = None

            if game_record:
                cache_result = await prepare_local_game_artwork(
                    bot=bot,
                    game_record=game_record,
                    refresh=True,
                )

            game_info[
                "cache_result"
            ] = cache_result

            if result == "added":
                added_games.append(
                    game_info
                )

            elif result in {
                "wishlisted",
                "moved_to_wishlist",
                "wishlist_updated",
            }:
                wishlisted_games.append(
                    game_info
                )

            else:
                existing_games.append(
                    game_info
                )

        if (
            added_games
            or wishlisted_games
            or existing_games
            or failed_links
        ):
            reply_lines = []

            for game in added_games:
                cache_line = ""

                if game.get(
                    "cache_result"
                ) == "cached":
                    cache_line = (
                        "\n🖼️ Game card rendered and ready"
                    )

                elif game.get(
                    "cache_result"
                ) == "already_cached":
                    cache_line = (
                        "\n⚡ Existing game card reused"
                    )

                elif game.get(
                    "cache_result"
                ) == "failed":
                    cache_line = (
                        "\n⚠️ Artwork will use the "
                        "store image as a fallback"
                    )

                sale_text = format_sale_text(
                    game.get(
                        "sale_info"
                    )
                )

                sale_line = ""

                if sale_text:
                    sale_line = (
                        "\n🏷️ **Currently on sale**\n"
                        f"{sale_text}"
                    )

                epic_note = ""

                if game.get(
                    "verification_status"
                ) in {
                    "blocked",
                    "unverified",
                }:
                    epic_note = (
                        "\n🟣 Epic blocked the automated "
                        "store check; Discord preview "
                        "metadata was retained."
                    )

                reply_lines.append(
                    "🎮 **Added to the wheel!**\n"
                    f"**{game['name']}**\n"
                    f"🏪 {get_store_display(game['store'])}"
                    f"{sale_line}"
                    f"{cache_line}"
                    f"{epic_note}"
                )

            for game in wishlisted_games:
                release_date = (
                    game.get(
                        "release_date"
                    )
                    or "To be announced"
                )

                reply_lines.append(
                    "🌠 **Added to the wishlist!**\n"
                    f"**{game['name']}**\n"
                    f"🏪 {get_store_display(game['store'])}\n"
                    f"📅 Release: **{release_date}**\n"
                    "It will stay off the wheel until "
                    "the automatic daily release check "
                    "confirms that it is available."
                )

            for game in existing_games:
                sale_text = format_sale_text(
                    game.get(
                        "sale_info"
                    )
                )

                sale_line = ""

                if sale_text:
                    sale_line = (
                        "\n🏷️ **Currently on sale:** "
                        f"{sale_text}"
                    )

                epic_note = ""

                if game.get(
                    "verification_status"
                ) in {
                    "blocked",
                    "unverified",
                }:
                    epic_note = (
                        "\n🟣 Epic blocked the automated "
                        "check; saved metadata was kept."
                    )

                reply_lines.append(
                    f"⚠️ **{game['name']}** is already "
                    "on the wheel."
                    f"{sale_line}"
                    f"{epic_note}"
                )

            for failed_link in failed_links:
                reply_lines.append(
                    "❌ I couldn't read this store link:\n"
                    f"{failed_link}"
                )

            await message.reply(
                "\n\n".join(
                    reply_lines
                )
            )



@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if (
        message.channel.id == SUGGESTION_THREAD_ID
        and find_supported_store_links(message.content)
    ):
        bot.queue_suggestion(message)

    await bot.process_commands(message)


@bot.tree.command(
    name="ping",
    description="Check if the bot is online",
)
async def ping(
    interaction: discord.Interaction,
):
    await interaction.response.send_message(
        "🏓 Pong!"
    )


if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN was not found in the .env file."
    )

if SUGGESTION_THREAD_ID is None:
    raise RuntimeError(
        "SUGGESTION_THREAD_ID was not found. Copy "
        "config.example.json to config.json and add your "
        "Discord suggestion thread ID, or set it in .env."
    )


bot.run(
    TOKEN
)
