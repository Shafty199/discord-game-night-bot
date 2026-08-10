import math
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from database.database import (
    get_all_games,
    get_all_singleplayer_games,
    get_smart_random_game,
    get_smart_random_game_for_ids,
    get_smart_random_singleplayer_game,
)
from settings import EPIC_EMOJI, STEAM_EMOJI
from ui.animation import (
    animate_spin,
    create_hosted_spin_embed,
    create_starting_spin_embed,
    edit_spin_result,
    wait_for_hosted_spin,
)
from ui.buttons import SpinView
from ui.embeds import create_spin_embed
from utils.spin_runtime import animate_with_sale_lookup
from utils.time_utils import (
    display_now,
    to_display_datetime,
)


GAMES_PER_PAGE = 10
LOGGER = logging.getLogger(__name__)


class GameListView(discord.ui.View):
    def __init__(
        self,
        games,
        author_id: int,
        *,
        wheel_type: str = "multiplayer",
    ):
        super().__init__(
            timeout=180
        )

        self.games = games
        self.author_id = author_id
        self.wheel_type = wheel_type
        self.current_page = 0

        self.total_pages = max(
            1,
            math.ceil(
                len(games) / GAMES_PER_PAGE
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
            * GAMES_PER_PAGE
        )
        end = start + GAMES_PER_PAGE
        page_games = self.games[start:end]

        singleplayer = (
            self.wheel_type == "singleplayer"
        )

        embed = discord.Embed(
            title=(
                "🧍 Single Player Wheel"
                if singleplayer
                else "🎮 Multiplayer Game Night Wheel"
            ),
            description=(
                f"There are **{len(self.games)} games** "
                + (
                    "not suitable for online group play."
                    if singleplayer
                    else "currently waiting on the multiplayer wheel."
                )
            ),
            colour=(
                discord.Colour.purple()
                if singleplayer
                else discord.Colour.gold()
            ),
        )

        if not page_games:
            embed.add_field(
                name="No games found",
                value=(
                    "No single-player or local-only games "
                    "are currently available."
                    if singleplayer
                    else (
                        "Add a supported Steam or Epic "
                        "Games Store link to the suggestions thread."
                    )
                ),
                inline=False,
            )

        for position, game in enumerate(
            page_games,
            start=start + 1,
        ):
            (
                name,
                store,
                suggested_by,
                times_played,
                last_played,
            ) = game[:5]
            wheel_reason = (
                game[5]
                if singleplayer and len(game) > 5
                else "single_player"
            )

            if store == "Steam":
                store_icon = STEAM_EMOJI
            elif store == "Epic Games Store":
                store_icon = EPIC_EMOJI
            else:
                store_icon = "🎮"

            details = []

            if times_played == 0:
                details.append("🎯 Played: **Never**")
            else:
                details.append(
                    f"🎯 Played: **{times_played}**"
                )

            if last_played:
                last = to_display_datetime(
                    last_played
                ) or display_now()
                days = (
                    display_now().date()
                    - last.date()
                ).days

                if days == 0:
                    friendly = "Today"
                elif days == 1:
                    friendly = "Yesterday"
                elif days < 7:
                    friendly = f"{days} days ago"
                else:
                    friendly = last.strftime(
                        "%d %b %Y"
                    )

                details.append(
                    f"🕒 Last Played: **{friendly}**"
                )

            if singleplayer:
                details.append(
                    (
                        "🏠 Local multiplayer/co-op only"
                        if wheel_reason == "local_only"
                        else "🧍 Confirmed single-player-only"
                    )
                )

            details.append(
                f"{store_icon} {store}"
            )
            details.append(
                f"💡 Suggested by **{suggested_by}**"
            )

            embed.add_field(
                name=f"{position}. {name}",
                value="\n".join(details),
                inline=False,
            )

        embed.set_footer(
            text=(
                f"Page {self.current_page + 1} "
                f"of {self.total_pages}"
            )
        )

        return embed

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who opened the game "
                "list can change its page.",
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


class Games(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
    ):
        self.bot = bot

    async def _run_spin(
        self,
        interaction: discord.Interaction,
        *,
        wheel_type: str,
        session_context: dict | None = None,
    ):
        prepared_spin_manager = getattr(
            self.bot,
            "prepared_spin_manager",
            None,
        )
        if session_context is not None:
            prepared_spin = (
                await prepared_spin_manager.acquire_session(
                    session_context["session_id"],
                    generation=session_context["generation"],
                )
                if prepared_spin_manager is not None
                else None
            )
        else:
            prepared_spin = (
                await prepared_spin_manager.acquire(
                    wheel_type
                )
                if prepared_spin_manager is not None
                else None
            )

        if prepared_spin is not None:
            winning_game = prepared_spin.winning_game

        elif session_context is not None:
            winning_game = await get_smart_random_game_for_ids(
                session_context["eligible_game_ids"]
            )

        elif wheel_type == "singleplayer":
            winning_game = await (
                get_smart_random_singleplayer_game()
            )

        else:
            winning_game = await get_smart_random_game()

        if not winning_game:
            await interaction.response.send_message(
                (
                    "🧍 There are no confirmed single-player "
                    "games on that wheel yet."
                    if wheel_type == "singleplayer"
                    else "🎮 There are no games on the wheel yet."
                ),
                ephemeral=True,
            )
            return

        if prepared_spin is not None:
            delivery_started_at = time.perf_counter()
            await interaction.response.send_message(
                embed=create_hosted_spin_embed(
                    wheel_type,
                    prepared_spin.image_url,
                )
            )
            LOGGER.info(
                "Hosted spin displayed: wheel=%s winner_id=%s "
                "discord_response=%.3fs",
                wheel_type,
                prepared_spin.winner_id,
                time.perf_counter() - delivery_started_at,
            )

        else:
            await interaction.response.send_message(
                embed=create_starting_spin_embed(
                    wheel_type
                )
            )

        message = await interaction.original_response()

        if prepared_spin is not None:
            animation = wait_for_hosted_spin(
                prepared_spin.duration_seconds
            )
        else:
            animation = animate_spin(
                message=message,
                winning_game=winning_game,
                wheel_type=wheel_type,
                session=self.bot.http_session,
                eligible_game_ids=(
                    session_context["eligible_game_ids"]
                    if session_context is not None
                    else None
                ),
            )

        sale_info = await animate_with_sale_lookup(
            animation,
            session=self.bot.http_session,
            game=winning_game,
        )

        await edit_spin_result(
            message,
            embed=create_spin_embed(
                winning_game,
                sale_info=sale_info,
                wheel_type=wheel_type,
            ),
            view=SpinView(
                winning_game,
                sale_info=sale_info,
                wheel_type=wheel_type,
                session_context=session_context,
            ),
            game_id=winning_game[0],
        )

    async def run_session_spin(
        self,
        interaction: discord.Interaction,
        session_context: dict,
    ):
        await self._run_spin(
            interaction,
            wheel_type="multiplayer",
            session_context=session_context,
        )

    @app_commands.command(
        name="games",
        description=(
            "Show every game on the multiplayer wheel"
        ),
    )
    async def games(
        self,
        interaction: discord.Interaction,
    ):
        wheel_games = await get_all_games()

        view = GameListView(
            games=wheel_games,
            author_id=interaction.user.id,
            wheel_type="multiplayer",
        )

        await interaction.response.send_message(
            embed=view.create_embed(),
            view=view,
        )

    @app_commands.command(
        name="singleplayergames",
        description=(
            "Show games on the Single Player/local-only wheel"
        ),
    )
    async def singleplayergames(
        self,
        interaction: discord.Interaction,
    ):
        wheel_games = (
            await get_all_singleplayer_games()
        )

        view = GameListView(
            games=wheel_games,
            author_id=interaction.user.id,
            wheel_type="singleplayer",
        )

        await interaction.response.send_message(
            embed=view.create_embed(),
            view=view,
        )

    @app_commands.command(
        name="spin",
        description=(
            "Spin the multiplayer Game Night wheel"
        ),
    )
    async def spin(
        self,
        interaction: discord.Interaction,
    ):
        await self._run_spin(
            interaction,
            wheel_type="multiplayer",
        )

    @app_commands.command(
        name="singleplayerspin",
        description=(
            "Spin the Single Player/local-only wheel"
        ),
    )
    async def singleplayerspin(
        self,
        interaction: discord.Interaction,
    ):
        await self._run_spin(
            interaction,
            wheel_type="singleplayer",
        )


async def setup(
    bot: commands.Bot,
):
    await bot.add_cog(
        Games(bot)
    )
