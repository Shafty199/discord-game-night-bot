import asyncio
import logging
import math

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database.database import (
    batched_database_writes,
    get_game_cache_record,
    get_wishlist_games,
    sync_game,
)
from settings import (
    EPIC_EMOJI,
    STEAM_EMOJI,
    SUGGESTION_THREAD_ID,
)
from utils.artwork_cache import prepare_local_game_artwork
from utils.igdb import enrich_missing_player_metadata
from utils.store import (
    detect_store,
    get_game_info_from_url,
)


LOGGER = logging.getLogger(__name__)

WISHLIST_PER_PAGE = 8
AUTO_CHECK_INTERVAL_HOURS = 6
AUTO_CHECK_STARTUP_DELAY_SECONDS = 15


async def _prefetch_wishlist_game(
    *,
    session,
    store_link: str,
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
            game_info = await get_game_info_from_url(
                session,
                store_link,
                force_refresh=True,
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


def _store_display(
    store: str,
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


def _release_display(
    release_date,
) -> str:
    cleaned_date = str(
        release_date or ""
    ).strip()

    return (
        cleaned_date
        or "To be announced"
    )


def _limited_name_list(
    names: list[str],
    limit: int = 15,
) -> str:
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

    return "\n".join(
        lines
    )


class WishlistView(discord.ui.View):
    def __init__(
        self,
        games,
        author_id: int,
    ):
        super().__init__(
            timeout=180
        )

        self.games = games
        self.author_id = author_id
        self.current_page = 0

        self.total_pages = max(
            1,
            math.ceil(
                len(games)
                / WISHLIST_PER_PAGE
            ),
        )

        self._update_buttons()

    def _update_buttons(self):
        self.previous_button.disabled = (
            self.current_page <= 0
        )

        self.next_button.disabled = (
            self.current_page
            >= self.total_pages - 1
        )

    def create_embed(self) -> discord.Embed:
        start = (
            self.current_page
            * WISHLIST_PER_PAGE
        )

        end = (
            start
            + WISHLIST_PER_PAGE
        )

        page_games = self.games[
            start:end
        ]

        embed = discord.Embed(
            title="🌠 Game Night Wishlist",
            description=(
                f"There are **{len(self.games)} upcoming "
                "games** waiting for release.\n\n"
                "Wishlist games stay off the wheel until "
                "the automatic six-hour release check confirms "
                "that they are available."
            ),
            colour=discord.Colour.purple(),
        )

        if not page_games:
            embed.add_field(
                name="Nothing here yet",
                value=(
                    "Post a Steam link for a coming-soon "
                    "game in the suggestions thread."
                ),
                inline=False,
            )

        for position, game in enumerate(
            page_games,
            start=start + 1,
        ):
            (
                _game_id,
                name,
                store_link,
                store,
                suggested_by,
                release_date,
                _image_url,
                _added_date,
            ) = game

            details = [
                (
                    "📅 **Release:** "
                    f"{_release_display(release_date)}"
                ),
                _store_display(store),
                (
                    "💡 Suggested by "
                    f"**{suggested_by}**"
                ),
            ]

            if store_link:
                details.append(
                    f"🔗 [Open store page]"
                    f"({store_link})"
                )

            embed.add_field(
                name=f"{position}. {name}",
                value="\n".join(
                    details
                ),
                inline=False,
            )

        embed.set_footer(
            text=(
                f"Page {self.current_page + 1} "
                f"of {self.total_pages}  •  "
                "Release status is checked every 6 hours"
            )
        )

        return embed

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who opened the wishlist "
                "can change its page.",
                ephemeral=True,
            )
            return False

        return True

    @discord.ui.button(
        label="Previous",
        emoji="⬅️",
        style=discord.ButtonStyle.secondary,
    )
    async def previous_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        self.current_page -= 1
        self._update_buttons()

        await interaction.response.edit_message(
            embed=self.create_embed(),
            view=self,
        )

    @discord.ui.button(
        label="Next",
        emoji="➡️",
        style=discord.ButtonStyle.secondary,
    )
    async def next_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        self.current_page += 1
        self._update_buttons()

        await interaction.response.edit_message(
            embed=self.create_embed(),
            view=self,
        )

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


class Wishlist(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
    ):
        self.bot = bot
        self.release_check_lock = asyncio.Lock()
        self.release_check.start()

    def cog_unload(self):
        self.release_check.cancel()

    async def _announce_releases(
        self,
        promoted_games: list[dict],
    ) -> None:
        if not promoted_games:
            return

        try:
            channel = await self.bot.fetch_channel(
                SUGGESTION_THREAD_ID
            )

        except discord.DiscordException as error:
            LOGGER.warning(
                "Could not access the suggestions thread "
                "for release announcements: %s: %s",
                type(error).__name__,
                error,
            )
            return

        embed = discord.Embed(
            title="🎉 Wishlist Games Released!",
            description=(
                "These games have automatically moved "
                "from the wishlist onto the Game Night wheel."
            ),
            colour=discord.Colour.green(),
        )

        for game in promoted_games[:15]:
            store_link = game.get(
                "store_link"
            )

            store = game.get(
                "store",
                "Unknown Store",
            )

            value_lines = [
                _store_display(
                    store
                ),
                (
                    "💡 Suggested by "
                    f"**{game.get('suggested_by', 'Unknown')}**"
                ),
            ]

            if store_link:
                value_lines.append(
                    f"🔗 [Open store page]"
                    f"({store_link})"
                )

            embed.add_field(
                name=f"🎮 {game['name']}",
                value="\n".join(
                    value_lines
                ),
                inline=False,
            )

        remaining = len(
            promoted_games
        ) - 15

        if remaining > 0:
            embed.set_footer(
                text=(
                    f"And {remaining} more released game"
                    f"{'s' if remaining != 1 else ''}."
                )
            )

        await channel.send(
            embed=embed
        )

    async def _check_wishlist_releases(
        self,
    ) -> dict:
        async with self.release_check_lock:
            maintenance_lock = getattr(
                self.bot,
                "maintenance_lock",
                None,
            )

            if maintenance_lock is None:
                async with batched_database_writes(
                    batch_size=25
                ):
                    return await self._run_wishlist_release_check()

            async with maintenance_lock:
                async with batched_database_writes(
                    batch_size=25
                ):
                    return await self._run_wishlist_release_check()

    async def _run_wishlist_release_check(
        self,
    ) -> dict:
        wishlist_games = (
            await get_wishlist_games()
        )

        promoted_games = []
        updated_games = []
        unchanged_games = []
        failed_games = []

        steam_semaphore = asyncio.Semaphore(
            3
        )
        epic_semaphore = asyncio.Semaphore(
            1
        )

        prefetch_tasks = {
            game[2]: asyncio.create_task(
                _prefetch_wishlist_game(
                    session=self.bot.http_session,
                    store_link=game[2],
                    steam_semaphore=steam_semaphore,
                    epic_semaphore=epic_semaphore,
                )
            )
            for game in wishlist_games
            if game[2]
        }

        prefetched_results = {}

        if prefetch_tasks:
            fetched_values = await asyncio.gather(
                *prefetch_tasks.values()
            )

            prefetched_results = dict(
                zip(
                    prefetch_tasks.keys(),
                    fetched_values,
                )
            )

            newly_released_game_infos = [
                result.get("game_info")
                for result in fetched_values
                if (
                    isinstance(result, dict)
                    and isinstance(
                        result.get("game_info"),
                        dict,
                    )
                    and result["game_info"].get(
                        "availability_verified",
                        False,
                    )
                    and result["game_info"].get(
                        "availability_status"
                    ) == "released"
                    and not result["game_info"].get(
                        "coming_soon",
                        False,
                    )
                )
            ]

            if newly_released_game_infos:
                try:
                    await enrich_missing_player_metadata(
                        self.bot.http_session,
                        newly_released_game_infos,
                    )

                except Exception:
                    LOGGER.exception(
                        "IGDB enrichment failed for newly "
                        "released wishlist games; store "
                        "metadata was retained"
                    )

        for game in wishlist_games:
            (
                _game_id,
                current_name,
                store_link,
                current_store,
                suggested_by,
                _release_date,
                _image_url,
                _added_date,
            ) = game

            if not store_link:
                failed_games.append(
                    current_name
                )
                continue

            prefetched_result = prefetched_results.get(
                store_link
            )

            lookup_error = (
                prefetched_result.get(
                    "error"
                )
                if prefetched_result
                else None
            )

            if lookup_error:
                failed_games.append(
                    current_name
                )

                LOGGER.warning(
                    "Wishlist store lookup failed for "
                    "%s: %s: %s",
                    current_name,
                    type(lookup_error).__name__,
                    lookup_error,
                )
                continue

            game_info = (
                prefetched_result.get(
                    "game_info"
                )
                if prefetched_result
                else None
            )

            if not game_info:
                failed_games.append(
                    current_name
                )
                continue

            availability_verified = bool(
                game_info.get(
                    "availability_verified",
                    False,
                )
            )

            next_availability_status = game_info.get(
                "availability_status",
                "coming_soon",
            )

            next_coming_soon = game_info.get(
                "coming_soon",
                True,
            )

            if not availability_verified:
                # Automatic checks must never promote a
                # wishlist game unless the store provides
                # positive release evidence.
                next_availability_status = "coming_soon"
                next_coming_soon = True

            try:
                result = await sync_game(
                    name=game_info.get(
                        "name"
                    ) or current_name,
                    store_link=game_info.get(
                        "store_link"
                    ) or store_link,
                    source_link=game_info.get(
                        "source_link"
                    ) or store_link,
                    store=game_info.get(
                        "store"
                    ) or current_store,
                    suggested_by=suggested_by,
                    image_url=game_info.get(
                        "image_url"
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
                    availability_status=(
                        next_availability_status
                    ),
                    release_date=game_info.get(
                        "release_date"
                    ),
                    coming_soon=next_coming_soon,
                    max_players=game_info.get(
                        "max_players"
                    ),
                    max_players_source=game_info.get(
                        "max_players_source"
                    ),
                    igdb_id=game_info.get(
                        "igdb_id"
                    ),
                    multiplayer_support=game_info.get(
                        "multiplayer_support"
                    ),
                    genres=game_info.get(
                        "genres"
                    ),
                    themes=game_info.get(
                        "themes"
                    ),
                    game_modes=game_info.get(
                        "game_modes"
                    ),
                )

            except Exception as error:
                failed_games.append(
                    current_name
                )

                LOGGER.exception(
                    "Wishlist database update failed for %s",
                    current_name,
                )
                continue

            if result == "promoted":
                promoted_game = {
                    "name": game_info.get(
                        "name"
                    ) or current_name,
                    "store_link": game_info.get(
                        "store_link"
                    ) or store_link,
                    "store": game_info.get(
                        "store"
                    ) or current_store,
                    "suggested_by": suggested_by,
                }

                promoted_games.append(
                    promoted_game
                )

                game_record = (
                    await get_game_cache_record(
                        name=promoted_game[
                            "name"
                        ],
                        store=promoted_game[
                            "store"
                        ],
                        store_link=promoted_game[
                            "store_link"
                        ],
                        external_id=game_info.get(
                            "external_id"
                        ),
                    )
                )

                if game_record:
                    try:
                        await prepare_local_game_artwork(
                            bot=self.bot,
                            game_record=game_record,
                            refresh=True,
                        )

                    except Exception as error:
                        LOGGER.exception(
                            "Wishlist local artwork preparation failed "
                            "for %s",
                            current_name,
                        )

            elif result in {
                "wishlist_updated",
                "updated",
            }:
                updated_games.append(
                    game_info.get(
                        "name"
                    ) or current_name
                )

            elif result in {
                "wishlist_unchanged",
                "unchanged",
            }:
                unchanged_games.append(
                    current_name
                )

            elif result == "unavailable":
                failed_games.append(
                    current_name
                )

        await self._announce_releases(
            promoted_games
        )

        if promoted_games:
            LOGGER.info(
                "Wishlist games released: %s",
                ", ".join(
                    game["name"]
                    for game in promoted_games
                ),
            )

        return {
            "checked": len(
                wishlist_games
            ),
            "promoted": promoted_games,
            "updated": updated_games,
            "unchanged": unchanged_games,
            "failed": failed_games,
        }

    @tasks.loop(
        hours=AUTO_CHECK_INTERVAL_HOURS
    )
    async def release_check(self):
        try:
            result = (
                await self._check_wishlist_releases()
            )

            LOGGER.info(
                "Wishlist check complete: checked=%s "
                "released=%s updated=%s failed=%s",
                result["checked"],
                len(result["promoted"]),
                len(result["updated"]),
                len(result["failed"]),
            )

        except Exception:
            LOGGER.exception(
                "Unexpected wishlist release-check failure"
            )

    @release_check.before_loop
    async def before_release_check(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(
            AUTO_CHECK_STARTUP_DELAY_SECONDS
        )

    @app_commands.command(
        name="wishlist",
        description=(
            "Show upcoming games waiting for release"
        ),
    )
    async def wishlist(
        self,
        interaction: discord.Interaction,
    ):
        games = await get_wishlist_games()

        view = WishlistView(
            games=games,
            author_id=interaction.user.id,
        )

        await interaction.response.send_message(
            embed=view.create_embed(),
            view=view,
        )

    @app_commands.command(
        name="checkreleases",
        description=(
            "Check wishlist games for newly released titles"
        ),
    )
    @app_commands.checks.has_permissions(
        manage_guild=True
    )
    async def checkreleases(
        self,
        interaction: discord.Interaction,
    ):
        await interaction.response.defer(
            ephemeral=True
        )

        result = (
            await self._check_wishlist_releases()
        )

        promoted_names = [
            game["name"]
            for game in result[
                "promoted"
            ]
        ]

        message = (
            "## 🌠 Wishlist Release Check\n\n"
            f"🎮 Wishlist games checked: "
            f"**{result['checked']}**\n"
            f"🎉 Released and moved to wheel: "
            f"**{len(promoted_names)}**\n"
            f"📝 Metadata updated: "
            f"**{len(result['updated'])}**\n"
            f"⚠️ Could not verify: "
            f"**{len(result['failed'])}**"
        )

        if promoted_names:
            message += (
                "\n\n## 🎉 Newly Released\n"
                + _limited_name_list(
                    promoted_names
                )
            )

        if result["updated"]:
            message += (
                "\n\n## 📝 Wishlist Updated\n"
                + _limited_name_list(
                    result["updated"]
                )
            )

        if result["failed"]:
            message += (
                "\n\n## ⚠️ Could Not Verify\n"
                + _limited_name_list(
                    result["failed"]
                )
            )

        await interaction.followup.send(
            message[:2000],
            ephemeral=True,
        )


async def setup(
    bot: commands.Bot,
):
    await bot.add_cog(
        Wishlist(bot)
    )
