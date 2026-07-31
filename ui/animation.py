import asyncio
import io
import logging
import secrets

import discord

from database.database import (
    MULTIPLAYER_WHEEL_FILTER,
    SINGLEPLAYER_WHEEL_FILTER,
    database_connection,
)
from settings import EPIC_EMOJI, STEAM_EMOJI
from utils.artwork_cache import (
    get_cached_game_card_path,
    prepare_local_game_card,
)
from utils.spin_gif import (
    SPIN_FRAME_DURATIONS_MS,
    SPIN_GIF_TEMP_DIRECTORY,
    build_spin_sequence,
    build_spin_gif,
)


LOGGER = logging.getLogger(__name__)


SPIN_DELAYS = [
    duration / 1000
    for duration in SPIN_FRAME_DURATIONS_MS
]
SPIN_GIF_FILENAME_PREFIX = "game-night-wheel"
SPIN_GIF_DELIVERY_GRACE_SECONDS = 0.85
GIF_RENDER_SEMAPHORE = asyncio.Semaphore(1)


SPIN_STATUS_LINES = [
    (
        "🎡 The Wheel Is Spinning!",
        "Shuffling the Game Night backlog...",
    ),
    (
        "💨 Picking Up Speed!",
        "Questionable decisions are being considered...",
    ),
    (
        "🎲 Letting Fate Decide...",
        "The wheel definitely knows what it is doing.",
    ),
    (
        "👀 Games Are Flying Past!",
        "Something good is coming into view...",
    ),
    (
        "🤔 The Wheel Is Thinking...",
        "It is becoming suspiciously indecisive.",
    ),
    (
        "🐌 Slowing Down...",
        "A winner is beginning to emerge...",
    ),
    (
        "😬 This Could Be It...",
        "Nobody breathe.",
    ),
]


def create_starting_spin_embed(
    wheel_type: str = "multiplayer",
) -> discord.Embed:
    singleplayer = (
        wheel_type == "singleplayer"
    )

    return discord.Embed(
        title=(
            "🧍 Starting the Single Player Wheel..."
            if singleplayer
            else "🎡 Starting the Wheel..."
        ),
        description=(
            (
                "The solo backlog is waking up...\n\n"
                "🎞️ **Preparing the wheel animation...**"
            )
            if singleplayer
            else (
                "The wheel is waking up...\n\n"
                "🎞️ **Preparing the wheel animation...**"
            )
        ),
        colour=(
            discord.Colour.purple()
            if singleplayer
            else discord.Colour.gold()
        ),
    )


def _clean_text(
    value,
    fallback: str = "Unknown Game",
) -> str:
    if value is None:
        return fallback

    cleaned_value = str(
        value
    ).strip()

    return cleaned_value or fallback


def _clean_display_name(
    name: str,
    max_length: int = 70,
) -> str:
    cleaned_name = _clean_text(
        name
    )

    if len(cleaned_name) <= max_length:
        return cleaned_name

    return (
        cleaned_name[: max_length - 3]
        + "..."
    )


def _clean_image_url(
    value,
) -> str | None:
    if value is None:
        return None

    cleaned_value = str(
        value
    ).strip()

    if not cleaned_value.lower().startswith(
        (
            "https://",
            "http://",
        )
    ):
        return None

    if any(
        character.isspace()
        for character in cleaned_value
    ):
        return None

    return cleaned_value


def _get_store_display(
    store: str,
) -> str:
    normalised_store = _clean_text(
        store,
        "Unknown Store",
    ).casefold()

    if "steam" in normalised_store:
        return f"{STEAM_EMOJI} Steam"

    if "epic" in normalised_store:
        return f"{EPIC_EMOJI} Epic Games Store"

    return (
        f"🎮 {_clean_text(store, 'Unknown Store')}"
    )


def _get_progress_bar(
    frame_number: int,
    total_frames: int,
) -> str:
    bar_length = 12

    progress = (
        frame_number + 1
    ) / max(
        total_frames,
        1,
    )

    filled_blocks = round(
        progress * bar_length
    )

    filled_blocks = min(
        max(
            filled_blocks,
            0,
        ),
        bar_length,
    )

    empty_blocks = (
        bar_length - filled_blocks
    )

    return (
        "▰" * filled_blocks
        + "▱" * empty_blocks
    )


def _get_status(
    frame_number: int,
    total_frames: int,
) -> tuple[str, str]:
    progress = (
        frame_number + 1
    ) / max(
        total_frames,
        1,
    )

    if progress < 0.20:
        return SPIN_STATUS_LINES[0]

    if progress < 0.38:
        return SPIN_STATUS_LINES[1]

    if progress < 0.54:
        return SPIN_STATUS_LINES[2]

    if progress < 0.68:
        return SPIN_STATUS_LINES[3]

    if progress < 0.82:
        return SPIN_STATUS_LINES[4]

    if progress < 0.94:
        return SPIN_STATUS_LINES[5]

    return SPIN_STATUS_LINES[6]


def _create_game_card_embed(
    *,
    name: str,
    store: str,
    image_url: str | None,
    frame_number: int,
    total_frames: int,
) -> discord.Embed:
    title, status_line = _get_status(
        frame_number,
        total_frames,
    )

    progress_bar = _get_progress_bar(
        frame_number,
        total_frames,
    )

    embed = discord.Embed(
        title=title,
        description=(
            f"# 🎮 {_clean_display_name(name)}\n\n"
            f"{_get_store_display(store)}\n\n"
            f"`{progress_bar}`\n"
            f"*{status_line}*"
        ),
        colour=discord.Colour.gold(),
    )

    cleaned_image_url = _clean_image_url(
        image_url
    )

    if cleaned_image_url:
        embed.set_image(
            url=cleaned_image_url
        )

    else:
        embed.add_field(
            name="🖼️ Artwork unavailable",
            value=(
                "The wheel is spinning this one "
                "without a cover image."
            ),
            inline=False,
        )

    return embed


def _create_suspense_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🛑 THE WHEEL HAS STOPPED...",
        description=(
            "\n"
            "# 👀 It landed on something...\n"
            "\n"
            "Revealing tonight's game..."
        ),
        colour=discord.Colour.gold(),
    )

    embed.set_footer(
        text="Nobody complain until the reveal."
    )

    return embed


def _create_winner_flash_embed(
    *,
    name: str,
    store: str,
    image_url: str | None,
) -> discord.Embed:
    embed = discord.Embed(
        title="🎉 AND THE WINNER IS...",
        description=(
            f"# 🎮 {_clean_display_name(name)}\n\n"
            f"{_get_store_display(store)}\n\n"
            "✨ **The wheel has made its decision!** ✨"
        ),
        colour=discord.Colour.gold(),
    )

    cleaned_image_url = _clean_image_url(
        image_url
    )

    if cleaned_image_url:
        embed.set_image(
            url=cleaned_image_url
        )

    embed.set_footer(
        text="Preparing the final Game Night card..."
    )

    return embed


async def _get_animation_games(
    wheel_type: str = "multiplayer",
) -> list[dict]:
    wheel_filter = (
        SINGLEPLAYER_WHEEL_FILTER
        if wheel_type == "singleplayer"
        else MULTIPLAYER_WHEEL_FILTER
    )

    async with database_connection() as db:
        cursor = await db.execute(
            f"""
            SELECT
                id,
                name,
                store,
                NULLIF(
                    image_url,
                    ''
                ) AS display_image_url,
                image_url AS source_image_url
            FROM games
            WHERE
                COALESCE(
                    availability_status,
                    'released'
                ) = 'released'
                AND (
                    link_status IS NULL
                    OR link_status != 'dead'
                )
                AND {wheel_filter}
            ORDER BY name COLLATE NOCASE
            """
        )

        rows = await cursor.fetchall()

    games = []
    seen_names = set()

    for (
        game_id,
        name,
        store,
        image_url,
        source_image_url,
    ) in rows:
        cleaned_name = _clean_text(
            name
        )

        comparison_name = (
            cleaned_name.casefold()
        )

        if comparison_name in seen_names:
            continue

        seen_names.add(
            comparison_name
        )

        games.append(
            {
                "id": game_id,
                "name": cleaned_name,
                "store": _clean_text(
                    store,
                    "Unknown Store",
                ),
                "image_url": _clean_image_url(
                    image_url
                ),
                "source_image_url": _clean_image_url(
                    source_image_url
                ),
            }
        )

    return games


def _winner_to_card(
    winning_game,
) -> dict:
    return {
        "id": winning_game[0],
        "name": _clean_text(
            winning_game[1]
        ),
        "store": _clean_text(
            winning_game[3],
            "Unknown Store",
        ),
        "image_url": _clean_image_url(
            winning_game[7]
        ),
        "source_image_url": _clean_image_url(
            winning_game[9]
            if len(winning_game) > 9
            else None
        ),
    }


async def _prepare_local_artwork(
    cards: list[dict],
    *,
    session,
) -> None:
    if session is None:
        raise RuntimeError(
            "The spin animation requires the shared "
            "HTTP session."
        )

    unique_cards = {}

    for card in cards:
        game_id = card.get("id")

        if game_id is not None:
            unique_cards.setdefault(
                int(game_id),
                card,
            )

    download_limit = asyncio.Semaphore(5)

    async def prepare_card(
        card: dict,
    ) -> None:
        async with download_limit:
            card_path = await get_cached_game_card_path(
                card.get("id")
            )

            if card_path is None:

                for image_url in dict.fromkeys(
                    (
                        card.get("source_image_url"),
                        card.get("image_url"),
                    )
                ):
                    if not image_url:
                        continue

                    cache_result = await prepare_local_game_card(
                        session=session,
                        game_record={
                            "id": card.get("id"),
                            "name": card.get("name"),
                            "store": card.get("store"),
                            "image_url": image_url,
                        },
                        refresh=False,
                    )

                    if cache_result in {
                        "cached",
                        "already_cached",
                    }:
                        card_path = (
                            await get_cached_game_card_path(
                                card.get("id")
                            )
                        )

                    if card_path is not None:
                        break

        card["card_path"] = (
            str(card_path)
            if card_path is not None
            else None
        )
        card["artwork_path"] = None

    if unique_cards:
        await asyncio.gather(
            *(
                prepare_card(card)
                for card in unique_cards.values()
            )
        )


def _create_gif_embed(
    wheel_type: str,
    gif_filename: str,
) -> discord.Embed:
    singleplayer = (
        wheel_type == "singleplayer"
    )
    embed = discord.Embed(
        title=(
            "🧍 Single Player Wheel"
            if singleplayer
            else "🎡 Game Night Wheel"
        ),
        description=(
            "The game names, stores and status text "
            "are animated directly with the artwork."
        ),
        colour=(
            discord.Colour.purple()
            if singleplayer
            else discord.Colour.gold()
        ),
    )
    embed.set_image(
        url=f"attachment://{gif_filename}"
    )
    embed.set_footer(
        text="The winner card will appear when the wheel stops."
    )
    return embed


def _load_gif_into_memory(
    gif_path,
) -> io.BytesIO:
    """Load one completed GIF without creating a second bytes copy."""

    gif_buffer = io.BytesIO()

    try:
        with gif_path.open("rb") as gif_file:
            while chunk := gif_file.read(
                256 * 1024
            ):
                gif_buffer.write(chunk)

        gif_buffer.seek(0)
        return gif_buffer

    except BaseException:
        gif_buffer.close()
        raise


async def _edit_target(
    target,
    *,
    embed: discord.Embed,
    view=None,
    attachments=None,
) -> None:
    edit_arguments = {
        "embed": embed,
        "view": view,
    }

    if attachments is not None:
        edit_arguments[
            "attachments"
        ] = attachments

    if isinstance(
        target,
        discord.Interaction,
    ):
        await target.edit_original_response(
            **edit_arguments
        )

    else:
        await target.edit(
            **edit_arguments
        )


async def edit_spin_result(
    target,
    *,
    embed: discord.Embed,
    view,
    game_id: int,
) -> None:
    """Show the result with its original store artwork."""
    _ = game_id
    await _edit_target(
        target,
        embed=embed,
        view=view,
        attachments=[],
    )


async def _animate_spin_with_embeds(
    *,
    edit_target,
    sequence: list[dict],
    winner: dict,
) -> None:
    total_frames = len(
        sequence
    )

    for frame_number, game in enumerate(
        sequence
    ):
        embed = _create_game_card_embed(
            name=game["name"],
            store=game["store"],
            image_url=game["image_url"],
            frame_number=frame_number,
            total_frames=total_frames,
        )

        await _edit_target(
            edit_target,
            embed=embed,
            view=None,
        )

        await asyncio.sleep(
            SPIN_DELAYS[frame_number]
        )

    await _edit_target(
        edit_target,
        embed=_create_suspense_embed(),
        view=None,
    )

    await asyncio.sleep(
        0.65
    )

    await _edit_target(
        edit_target,
        embed=_create_winner_flash_embed(
            name=winner["name"],
            store=winner["store"],
            image_url=winner["image_url"],
        ),
        view=None,
    )

    await asyncio.sleep(
        0.75
    )


async def animate_spin(
    winning_game,
    target=None,
    message=None,
    wheel_type: str = "multiplayer",
    session=None,
) -> None:
    edit_target = (
        target
        or message
    )

    if edit_target is None:
        raise ValueError(
            "animate_spin requires either "
            "'target' or 'message'."
        )

    winner = _winner_to_card(
        winning_game
    )
    games = await _get_animation_games(
        wheel_type=wheel_type
    )
    sequence = build_spin_sequence(
        games=games,
        winner=winner,
        wheel_type=wheel_type,
    )
    gif_filename = (
        f"{SPIN_GIF_FILENAME_PREFIX}-"
        f"{secrets.token_hex(6)}.gif"
    )
    gif_path = (
        SPIN_GIF_TEMP_DIRECTORY
        / gif_filename
    )

    try:
        await _prepare_local_artwork(
            [*sequence, winner],
            session=session,
        )
        async with GIF_RENDER_SEMAPHORE:
            spin_gif = await asyncio.to_thread(
                build_spin_gif,
                sequence,
                winner,
                wheel_type=wheel_type,
                output_path=gif_path,
            )
        spin_duration_seconds = (
            spin_gif.duration_seconds
        )
        gif_buffer = await asyncio.to_thread(
            _load_gif_into_memory,
            gif_path,
        )
        await asyncio.to_thread(
            gif_path.unlink,
            missing_ok=True,
        )
        gif_file = discord.File(
            gif_buffer,
            filename=gif_filename,
        )

        try:
            await _edit_target(
                edit_target,
                embed=_create_gif_embed(
                    wheel_type,
                    gif_filename,
                ),
                view=None,
                attachments=[gif_file],
            )

        finally:
            gif_file.close()
            gif_buffer.close()

        spin_gif = None
        gif_file = None
        gif_buffer = None
        await asyncio.sleep(
            spin_duration_seconds
            + SPIN_GIF_DELIVERY_GRACE_SECONDS
        )
        return

    except Exception:
        LOGGER.exception(
            "Single-file wheel animation failed; "
            "using the compatibility animation."
        )

    finally:
        try:
            await asyncio.to_thread(
                gif_path.unlink,
                missing_ok=True,
            )
        except OSError:
            LOGGER.warning(
                "Could not remove temporary spin GIF %s",
                gif_path,
            )

    await _animate_spin_with_embeds(
        edit_target=edit_target,
        sequence=sequence,
        winner=winner,
    )
