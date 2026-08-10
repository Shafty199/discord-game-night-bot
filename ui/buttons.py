import logging
import time

import discord

from database.database import (
    get_smart_random_game,
    get_smart_random_game_for_ids,
    get_smart_random_singleplayer_game,
    mark_game_played,
)
from settings import EPIC_EMOJI, STEAM_EMOJI
from ui.animation import (
    animate_spin,
    create_hosted_spin_embed,
    create_starting_spin_embed,
    edit_spin_result,
    wait_for_hosted_spin,
)
from ui.embeds import (
    create_spin_embed,
    format_game_multiplayer_support,
    format_sale_text,
)
from utils.spin_runtime import animate_with_sale_lookup


LOGGER = logging.getLogger(__name__)


async def _get_different_game(
    current_game_id: int,
    *,
    wheel_type: str = "multiplayer",
    eligible_game_ids=None,
):
    if eligible_game_ids is not None:
        new_game = await get_smart_random_game_for_ids(
            eligible_game_ids
        )
    elif wheel_type == "singleplayer":
        new_game = (
            await get_smart_random_singleplayer_game()
        )
    else:
        new_game = await get_smart_random_game()

    if not new_game:
        return None

    attempts = 0

    while (
        new_game[0] == current_game_id
        and attempts < 10
    ):
        if eligible_game_ids is not None:
            new_game = await get_smart_random_game_for_ids(
                eligible_game_ids
            )
        elif wheel_type == "singleplayer":
            new_game = (
                await get_smart_random_singleplayer_game()
            )
        else:
            new_game = await get_smart_random_game()

        attempts += 1

        if not new_game:
            return None

    return new_game


class SpinView(discord.ui.View):
    def __init__(
        self,
        game,
        sale_info: dict | None = None,
        wheel_type: str = "multiplayer",
        session_context: dict | None = None,
    ):
        super().__init__(
            timeout=300
        )

        self.game = game
        self.sale_info = sale_info
        self.wheel_type = wheel_type
        self.session_context = session_context
        self.locked = False
        self.spinning = False

    @discord.ui.button(
        label="Spin Again",
        emoji="🔄",
        style=discord.ButtonStyle.primary,
    )
    async def reroll(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if self.session_context is not None:
            sessions_cog = interaction.client.get_cog(
                "Sessions"
            )

            if (
                sessions_cog is None
                or not await sessions_cog.can_control_session(
                    interaction,
                    self.session_context["session_id"],
                    generation=self.session_context["generation"],
                )
            ):
                return

        if self.locked:
            await interaction.response.send_message(
                "🔒 This game has already been locked in.",
                ephemeral=True,
            )
            return

        if self.spinning:
            await interaction.response.send_message(
                "🎡 The wheel is already spinning!",
                ephemeral=True,
            )
            return

        self.spinning = True

        prepared_spin_manager = getattr(
            interaction.client,
            "prepared_spin_manager",
            None,
        )
        if self.session_context is not None:
            prepared_spin = (
                await prepared_spin_manager.acquire_session(
                    self.session_context["session_id"],
                    generation=self.session_context["generation"],
                    exclude_game_id=self.game[0],
                )
                if prepared_spin_manager is not None
                else None
            )
        else:
            prepared_spin = (
                await prepared_spin_manager.acquire(
                    self.wheel_type,
                    exclude_game_id=self.game[0],
                )
                if prepared_spin_manager is not None
                else None
            )
        new_game = (
            prepared_spin.winning_game
            if prepared_spin is not None
            else await _get_different_game(
                self.game[0],
                wheel_type=self.wheel_type,
                eligible_game_ids=(
                    self.session_context["eligible_game_ids"]
                    if self.session_context is not None
                    else None
                ),
            )
        )

        if not new_game:
            self.spinning = False

            await interaction.response.send_message(
                "🎮 No games are available.",
                ephemeral=True,
            )
            return

        for child in self.children:
            child.disabled = True

        if prepared_spin is not None:
            delivery_started_at = time.perf_counter()
            await interaction.response.edit_message(
                embed=create_hosted_spin_embed(
                    self.wheel_type,
                    prepared_spin.image_url,
                ),
                view=None,
                attachments=[],
            )
            LOGGER.info(
                "Hosted reroll displayed: wheel=%s winner_id=%s "
                "discord_edit=%.3fs",
                self.wheel_type,
                prepared_spin.winner_id,
                time.perf_counter() - delivery_started_at,
            )
            animation = wait_for_hosted_spin(
                prepared_spin.duration_seconds
            )

        else:
            await interaction.response.edit_message(
                embed=create_starting_spin_embed(
                    self.wheel_type
                ),
                view=None,
                attachments=[],
            )
            animation = animate_spin(
                target=interaction,
                winning_game=new_game,
                wheel_type=self.wheel_type,
                session=(
                    interaction.client.http_session
                ),
                eligible_game_ids=(
                    self.session_context["eligible_game_ids"]
                    if self.session_context is not None
                    else None
                ),
            )

        try:
            sale_info = await animate_with_sale_lookup(
                animation,
                session=interaction.client.http_session,
                game=new_game,
            )

            new_view = SpinView(
                new_game,
                sale_info=sale_info,
                wheel_type=self.wheel_type,
                session_context=self.session_context,
            )

            await edit_spin_result(
                interaction,
                embed=create_spin_embed(
                    new_game,
                    sale_info=sale_info,
                    wheel_type=self.wheel_type,
                ),
                view=new_view,
                game_id=new_game[0],
            )

        except discord.DiscordException as error:
            self.spinning = False

            for child in self.children:
                child.disabled = False

            await edit_spin_result(
                interaction,
                embed=create_spin_embed(
                    self.game,
                    sale_info=self.sale_info,
                    wheel_type=self.wheel_type,
                ),
                view=self,
                game_id=self.game[0],
            )

            await interaction.followup.send(
                "❌ Something interrupted the spin.\n"
                f"`{error}`",
                ephemeral=True,
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
    ):
        if self.session_context is not None:
            sessions_cog = interaction.client.get_cog(
                "Sessions"
            )

            if (
                sessions_cog is None
                or not await sessions_cog.can_control_session(
                    interaction,
                    self.session_context["session_id"],
                    generation=self.session_context["generation"],
                )
            ):
                return

        if self.locked:
            await interaction.response.send_message(
                "🔒 This game has already been locked in.",
                ephemeral=True,
            )
            return

        if self.spinning:
            await interaction.response.send_message(
                "🎡 Wait for the wheel to stop first!",
                ephemeral=True,
            )
            return

        if self.session_context is not None:
            await interaction.response.defer()

        self.locked = True

        if self.session_context is None:
            await mark_game_played(
                game_id=self.game[0],
                locked_by=interaction.user.display_name,
            )

        prepared_spin_manager = getattr(
            interaction.client,
            "prepared_spin_manager",
            None,
        )

        if prepared_spin_manager is not None:
            await prepared_spin_manager.invalidate_game(
                self.game[0]
            )

        if self.session_context is not None:
            await sessions_cog.lock_game_from_spin(
                interaction,
                self.session_context["session_id"],
                self.game,
            )

        for child in self.children:
            child.disabled = True

        if self.session_context is not None:
            await interaction.edit_original_response(
                view=self
            )
        else:
            await interaction.response.edit_message(
                view=self
            )

        store_link = self.game[2]
        store = self.game[3]

        normalised_store = str(
            store or ""
        ).casefold()

        if "steam" in normalised_store:
            store_display = (
                f"{STEAM_EMOJI} {store}"
            )

        elif "epic" in normalised_store:
            store_display = (
                f"{EPIC_EMOJI} {store}"
            )

        else:
            store_display = (
                f"🎮 {store}"
            )

        link_text = ""

        if store_link:
            link_text = (
                f"\n🔗 [Open on {store}]"
                f"({store_link})\n"
            )

        sale_text = format_sale_text(
            self.sale_info
        )

        sale_line = ""

        if sale_text:
            sale_line = (
                "\n🏷️ **Currently on sale**\n"
                f"{sale_text}\n"
            )

        multiplayer_text = (
            format_game_multiplayer_support(
                self.game
            )
        )
        multiplayer_line = ""

        if multiplayer_text:
            multiplayer_line = (
                "\n🤝 **Multiplayer Support**\n"
                f"{multiplayer_text}\n"
            )

        locked_content = (
            "## 🎉 TONIGHT'S GAME IS LOCKED IN!\n\n"
            f"# 🎮 {self.game[1]}\n"
            f"**{store_display}**\n"
            f"{multiplayer_line}"
            f"{sale_line}"
            f"{link_text}\n"
            f"🔒 Locked in by "
            f"{interaction.user.mention}\n\n"
            "**Get the squad together — game on!**"
        )

        if self.session_context is not None:
            locked_message = await interaction.followup.send(
                locked_content,
                view=sessions_cog.locked_game_view(
                    self.session_context["session_id"]
                ),
                wait=True,
            )
            await sessions_cog.save_locked_game_message(
                self.session_context["session_id"],
                channel_id=locked_message.channel.id,
                message_id=locked_message.id,
            )

            try:
                await interaction.delete_original_response()
            except discord.NotFound:
                pass
        else:
            await interaction.followup.send(locked_content)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
