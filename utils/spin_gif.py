import gc
import hashlib
import io
import os
import random
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import (
    Image,
    ImageDraw,
    ImageEnhance,
    ImageFont,
    ImageOps,
)

from settings import LOCAL_ARTWORK_CACHE_DIRECTORY


SPIN_FRAME_DURATIONS_MS = (
    180,
    180,
    180,
    180,
    200,
    200,
    220,
    220,
    250,
    250,
    300,
    300,
    380,
    380,
    480,
    480,
    620,
    620,
    820,
    820,
)
SUSPENSE_DURATION_MS = 650
WINNER_FLASH_DURATION_MS = 750
MAX_SPIN_GIF_BYTES = 7_500_000
STATIC_CARD_CACHE_VERSION = "2"
STATIC_CARD_CACHE_DIRECTORY = (
    LOCAL_ARTWORK_CACHE_DIRECTORY / "cards"
)
SPIN_GIF_TEMP_DIRECTORY = (
    LOCAL_ARTWORK_CACHE_DIRECTORY / "spins"
)

CANVAS_WIDTH = 640
CANVAS_HEIGHT = 550

BACKGROUND_TOP = (24, 26, 31)
BACKGROUND_BOTTOM = (46, 49, 57)
PANEL_COLOUR = (32, 35, 41)
TEXT_COLOUR = (245, 246, 247)
MUTED_TEXT_COLOUR = (185, 190, 198)
GOLD_COLOUR = (250, 180, 55)
PURPLE_COLOUR = (165, 106, 255)
EMPTY_PROGRESS_COLOUR = (76, 80, 90)

GIF_STATUS_LINES = (
    (
        "THE WHEEL IS SPINNING!",
        "Shuffling the Game Night backlog...",
    ),
    (
        "PICKING UP SPEED!",
        "Questionable decisions are being considered...",
    ),
    (
        "LETTING FATE DECIDE...",
        "The wheel definitely knows what it is doing.",
    ),
    (
        "GAMES ARE FLYING PAST!",
        "Something good is coming into view...",
    ),
    (
        "THE WHEEL IS THINKING...",
        "It is becoming suspiciously indecisive.",
    ),
    (
        "SLOWING DOWN...",
        "A winner is beginning to emerge...",
    ),
    (
        "THIS COULD BE IT...",
        "Nobody breathe.",
    ),
)
SPIN_RANDOM = random.SystemRandom()
_last_spin_sequence: dict[
    str,
    tuple,
] = {}


@dataclass(frozen=True)
class SpinGif:
    data: bytes | None
    duration_seconds: float


def build_spin_sequence(
    games: list[dict],
    winner: dict,
    *,
    wheel_type: str = "multiplayer",
    total_frames: int = len(
        SPIN_FRAME_DURATIONS_MS
    ),
) -> list[dict]:
    winner_name = str(
        winner.get("name")
        or ""
    ).casefold()
    other_games = [
        game
        for game in games
        if str(
            game.get("name")
            or ""
        ).casefold() != winner_name
    ]

    if not other_games:
        mystery_card = {
            "id": None,
            "name": "The wheel is still spinning...",
            "store": "Game Night",
            "image_url": None,
            "source_image_url": None,
        }
        return [
            mystery_card
            for _ in range(total_frames)
        ]

    previous_signature = _last_spin_sequence.get(
        wheel_type
    )
    sequence = []

    for _ in range(5):
        sequence = []

        while len(sequence) < total_frames:
            shuffled_games = list(
                other_games
            )
            SPIN_RANDOM.shuffle(shuffled_games)
            sequence.extend(shuffled_games)

        sequence = sequence[:total_frames]
        signature = tuple(
            game.get("id")
            or game.get("name")
            for game in sequence
        )

        if signature != previous_signature:
            break

    if (
        signature == previous_signature
        and len(sequence) > 1
    ):
        sequence = sequence[1:] + sequence[:1]
        signature = tuple(
            game.get("id")
            or game.get("name")
            for game in sequence
        )

    _last_spin_sequence[wheel_type] = signature
    return sequence


@lru_cache(maxsize=64)
def _font(
    size: int,
    *,
    bold: bool = False,
):
    font_names = (
        "DejaVuSans-Bold.ttf"
        if bold
        else "DejaVuSans.ttf",
        (
            "/usr/share/fonts/truetype/dejavu/"
            + (
                "DejaVuSans-Bold.ttf"
                if bold
                else "DejaVuSans.ttf"
            )
        ),
        (
            "C:/Windows/Fonts/arialbd.ttf"
            if bold
            else "C:/Windows/Fonts/arial.ttf"
        ),
    )

    for font_name in font_names:
        try:
            return ImageFont.truetype(
                font_name,
                size=size,
            )

        except OSError:
            continue

    try:
        return ImageFont.load_default(
            size=size
        )

    except TypeError:
        return ImageFont.load_default()


@lru_cache(maxsize=1)
def _background_template() -> Image.Image:
    gradient = Image.new(
        "RGB",
        (1, CANVAS_HEIGHT),
    )
    gradient.putdata(
        [
            tuple(
                round(
                    top + (bottom - top) * blend
                )
                for top, bottom in zip(
                    BACKGROUND_TOP,
                    BACKGROUND_BOTTOM,
                )
            )
            for blend in (
                y_position
                / max(CANVAS_HEIGHT - 1, 1)
                for y_position in range(CANVAS_HEIGHT)
            )
        ]
    )
    return gradient.resize(
        (CANVAS_WIDTH, CANVAS_HEIGHT),
        resample=Image.Resampling.BILINEAR,
    )


def _background() -> Image.Image:
    return _background_template().copy()


def _text_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font,
) -> float:
    return draw.textlength(
        text,
        font=font,
    )


def _fit_text(
    draw: ImageDraw.ImageDraw,
    value,
    *,
    maximum_width: int,
    maximum_size: int,
    minimum_size: int,
    bold: bool = False,
) -> tuple[str, ImageFont.ImageFont]:
    text = str(
        value or "Unknown Game"
    ).strip() or "Unknown Game"

    for font_size in range(
        maximum_size,
        minimum_size - 1,
        -1,
    ):
        font = _font(
            font_size,
            bold=bold,
        )

        if _text_width(
            draw,
            text,
            font,
        ) <= maximum_width:
            return text, font

    font = _font(
        minimum_size,
        bold=bold,
    )
    ellipsis = "..."

    while (
        text
        and _text_width(
            draw,
            text + ellipsis,
            font,
        ) > maximum_width
    ):
        text = text[:-1]

    return text.rstrip() + ellipsis, font


def _store_label(store) -> str:
    cleaned_store = str(
        store or "Unknown Store"
    ).strip()
    normalised_store = cleaned_store.casefold()

    if "steam" in normalised_store:
        return "STEAM"

    if "epic" in normalised_store:
        return "EPIC GAMES STORE"

    return cleaned_store.upper()


def _status_for_frame(
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
        return GIF_STATUS_LINES[0]
    if progress < 0.38:
        return GIF_STATUS_LINES[1]
    if progress < 0.54:
        return GIF_STATUS_LINES[2]
    if progress < 0.68:
        return GIF_STATUS_LINES[3]
    if progress < 0.82:
        return GIF_STATUS_LINES[4]
    if progress < 0.94:
        return GIF_STATUS_LINES[5]

    return GIF_STATUS_LINES[6]


def _load_artwork_panel(
    artwork_path: str,
    _modified_time_ns: int,
    _file_size: int,
) -> Image.Image:
    panel_size = (592, 340)

    try:
        with Image.open(
            Path(artwork_path)
        ) as source_image:
            source_artwork = ImageOps.exif_transpose(
                source_image
            ).convert("RGB")

            try:
                fitted_artwork = ImageOps.fit(
                    source_artwork,
                    panel_size,
                    method=Image.Resampling.LANCZOS,
                    centering=(0.5, 0.5),
                )
                try:
                    artwork = ImageEnhance.Brightness(
                        fitted_artwork
                    ).enhance(0.28)
                finally:
                    fitted_artwork.close()

                complete_artwork = ImageOps.contain(
                    source_artwork,
                    panel_size,
                    method=Image.Resampling.LANCZOS,
                )
                try:
                    artwork.paste(
                        complete_artwork,
                        (
                            (
                                panel_size[0]
                                - complete_artwork.width
                            ) // 2,
                            (
                                panel_size[1]
                                - complete_artwork.height
                            ) // 2,
                        ),
                    )
                finally:
                    complete_artwork.close()
            finally:
                source_artwork.close()

    except (
        OSError,
        TypeError,
        ValueError,
    ):
        artwork = Image.new(
            "RGB",
            panel_size,
            PANEL_COLOUR,
        )
        draw = ImageDraw.Draw(artwork)
        unavailable_font = _font(
            21,
            bold=True,
        )
        message = "ARTWORK UNAVAILABLE"
        message_width = _text_width(
            draw,
            message,
            unavailable_font,
        )
        draw.text(
            (
                (panel_size[0] - message_width) / 2,
                panel_size[1] / 2 - 12,
            ),
            message,
            fill=MUTED_TEXT_COLOUR,
            font=unavailable_font,
        )

    mask = Image.new(
        "L",
        panel_size,
        0,
    )
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle(
        (0, 0, panel_size[0], panel_size[1]),
        radius=14,
        fill=255,
    )
    artwork.putalpha(mask)
    mask.close()
    return artwork


def _artwork_cache_key(artwork_path) -> tuple[str, int, int]:
    try:
        resolved_path = Path(
            artwork_path
        ).resolve()
        file_details = resolved_path.stat()
        cache_key = (
            str(resolved_path),
            file_details.st_mtime_ns,
            file_details.st_size,
        )

    except (
        OSError,
        TypeError,
        ValueError,
    ):
        cache_key = (
            "",
            0,
            0,
        )

    return cache_key


def _artwork_panel(
    artwork_path,
) -> Image.Image:
    cache_key = _artwork_cache_key(
        artwork_path
    )

    return _load_artwork_panel(
        *cache_key
    )


def _build_static_game_card(
    name: str,
    store: str,
    artwork_path: str,
    modified_time_ns: int,
    file_size: int,
) -> Image.Image:
    """Render the reusable portion of one wheel card."""

    frame = _background()
    draw = ImageDraw.Draw(frame)
    artwork = _load_artwork_panel(
        artwork_path,
        modified_time_ns,
        file_size,
    )
    try:
        frame.paste(
            artwork,
            (24, 60),
            artwork,
        )
    finally:
        artwork.close()
    game_name, game_font = _fit_text(
        draw,
        name,
        maximum_width=592,
        maximum_size=31,
        minimum_size=19,
        bold=True,
    )
    draw.text(
        (24, 414),
        game_name,
        fill=TEXT_COLOUR,
        font=game_font,
    )
    draw.text(
        (24, 461),
        _store_label(store),
        fill=MUTED_TEXT_COLOUR,
        font=_font(16, bold=True),
    )
    return frame


def _static_card_cache_path(
    card: dict,
    artwork_key: tuple[str, int, int],
) -> tuple[Path, str]:
    name = str(
        card.get("name") or "Unknown Game"
    )
    store = str(
        card.get("store") or "Unknown Store"
    )

    try:
        game_id = int(card.get("id"))
        identity = f"game-{game_id}"
    except (TypeError, ValueError):
        identity = (
            "name-"
            + hashlib.sha256(
                name.casefold().encode("utf-8")
            ).hexdigest()[:12]
        )

    digest = hashlib.sha256()

    for value in (
        STATIC_CARD_CACHE_VERSION,
        name,
        store,
        *artwork_key,
    ):
        digest.update(str(value).encode("utf-8"))
        digest.update(b"\0")

    cache_path = (
        STATIC_CARD_CACHE_DIRECTORY
        / f"{identity}-{digest.hexdigest()[:16]}.jpg"
    )
    return cache_path, identity


def get_static_game_card_path(
    game_id,
) -> Path | None:
    """Return the newest persisted wheel card for a game."""

    try:
        clean_game_id = int(game_id)
    except (TypeError, ValueError):
        return None

    if clean_game_id <= 0:
        return None

    try:
        candidates = list(
            STATIC_CARD_CACHE_DIRECTORY.glob(
                f"game-{clean_game_id}-*.jpg"
            )
        )
    except OSError:
        return None

    valid_candidates = []

    for candidate in candidates:
        try:
            details = candidate.stat()
            if details.st_size > 0:
                valid_candidates.append(
                    (details.st_mtime_ns, candidate)
                )
        except OSError:
            continue

    if not valid_candidates:
        return None

    return max(
        valid_candidates,
        key=lambda item: item[0],
    )[1]


def _write_static_card_cache(
    frame: Image.Image,
    cache_path: Path,
    identity: str,
) -> None:
    cache_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    temporary_path = cache_path.with_name(
        f".{cache_path.name}.{os.getpid()}-"
        f"{threading.get_ident()}.tmp"
    )

    try:
        frame.save(
            temporary_path,
            format="JPEG",
            quality=90,
        )
        os.replace(
            temporary_path,
            cache_path,
        )

    finally:
        temporary_path.unlink(missing_ok=True)

    for old_cache_path in cache_path.parent.glob(
        f"{identity}-*.jpg"
    ):
        if old_cache_path != cache_path:
            try:
                old_cache_path.unlink(missing_ok=True)
            except OSError:
                pass


def _load_disk_static_card(
    cache_path: str,
) -> Image.Image:
    with Image.open(cache_path) as cached_card:
        return cached_card.convert("RGB")


def _static_game_card(card: dict) -> Image.Image:
    artwork_key = _artwork_cache_key(
        card.get("artwork_path")
    )
    cache_path, identity = _static_card_cache_path(
        card,
        artwork_key,
    )

    try:
        if cache_path.stat().st_size <= 0:
            raise OSError(
                "Empty static card cache file"
            )
        return _load_disk_static_card(
            str(cache_path)
        )

    except (OSError, TypeError, ValueError):
        pass

    if not artwork_key[0]:
        existing_card_path = (
            card.get("card_path")
            or get_static_game_card_path(
                card.get("id")
            )
        )

        if existing_card_path:
            try:
                existing_path = Path(
                    existing_card_path
                )
                if existing_path.stat().st_size <= 0:
                    raise OSError(
                        "Empty static card cache file"
                    )
                return _load_disk_static_card(
                    str(existing_path)
                )
            except (OSError, TypeError, ValueError):
                pass

    frame = _build_static_game_card(
        str(card.get("name") or "Unknown Game"),
        str(card.get("store") or "Unknown Store"),
        *artwork_key,
    )

    try:
        _write_static_card_cache(
            frame,
            cache_path,
            identity,
        )
        return frame

    except (OSError, TypeError, ValueError):
        return frame


def prepare_static_game_card(
    card: dict,
) -> Path | None:
    """Render and persist a card from temporary source artwork."""

    artwork_key = _artwork_cache_key(
        card.get("artwork_path")
    )

    if not artwork_key[0]:
        return get_static_game_card_path(
            card.get("id")
        )

    cache_path, _identity = _static_card_cache_path(
        card,
        artwork_key,
    )
    frame = _static_game_card(
        {
            **card,
            "card_path": None,
        }
    )
    frame.close()

    try:
        return (
            cache_path
            if cache_path.is_file()
            and cache_path.stat().st_size > 0
            else None
        )
    except OSError:
        return None


def _draw_progress_bar(
    draw: ImageDraw.ImageDraw,
    *,
    progress: float,
    accent_colour: tuple[int, int, int],
) -> None:
    left = 24
    top = 492
    width = 592
    height = 14

    draw.rounded_rectangle(
        (
            left,
            top,
            left + width,
            top + height,
        ),
        radius=height // 2,
        fill=EMPTY_PROGRESS_COLOUR,
    )

    filled_width = max(
        height,
        round(width * min(max(progress, 0), 1)),
    )
    draw.rounded_rectangle(
        (
            left,
            top,
            left + filled_width,
            top + height,
        ),
        radius=height // 2,
        fill=accent_colour,
    )


def _game_frame(
    card: dict,
    *,
    frame_number: int,
    total_frames: int,
    accent_colour: tuple[int, int, int],
) -> Image.Image:
    frame = _static_game_card(card)
    draw = ImageDraw.Draw(frame)
    title, status_line = _status_for_frame(
        frame_number,
        total_frames,
    )
    title_font = _font(
        25,
        bold=True,
    )

    draw.text(
        (24, 19),
        title,
        fill=accent_colour,
        font=title_font,
    )

    progress = (
        frame_number + 1
    ) / max(total_frames, 1)
    _draw_progress_bar(
        draw,
        progress=progress,
        accent_colour=accent_colour,
    )

    status_font = _font(14)
    status_text, status_font = _fit_text(
        draw,
        status_line,
        maximum_width=592,
        maximum_size=14,
        minimum_size=12,
    )
    draw.text(
        (24, 515),
        status_text,
        fill=MUTED_TEXT_COLOUR,
        font=status_font,
    )
    return frame


def _suspense_frame(
    previous_frame: Image.Image,
    *,
    accent_colour: tuple[int, int, int],
) -> Image.Image:
    darkened = ImageEnhance.Brightness(
        previous_frame
    ).enhance(0.22)
    frame = darkened.convert("RGB")
    draw = ImageDraw.Draw(frame)

    heading = "THE WHEEL HAS STOPPED..."
    heading_font = _font(
        34,
        bold=True,
    )
    heading_width = _text_width(
        draw,
        heading,
        heading_font,
    )
    draw.text(
        (
            (CANVAS_WIDTH - heading_width) / 2,
            215,
        ),
        heading,
        fill=accent_colour,
        font=heading_font,
    )

    subheading = "It landed on something..."
    subheading_font = _font(22)
    subheading_width = _text_width(
        draw,
        subheading,
        subheading_font,
    )
    draw.text(
        (
            (CANVAS_WIDTH - subheading_width) / 2,
            278,
        ),
        subheading,
        fill=TEXT_COLOUR,
        font=subheading_font,
    )
    return frame


def _winner_frame(
    winner: dict,
    *,
    accent_colour: tuple[int, int, int],
) -> Image.Image:
    frame = _static_game_card(winner)
    draw = ImageDraw.Draw(frame)
    title_font = _font(
        27,
        bold=True,
    )
    draw.text(
        (24, 17),
        "AND THE WINNER IS...",
        fill=accent_colour,
        font=title_font,
    )

    winner_line = "THE WHEEL HAS MADE ITS DECISION!"
    winner_font = _font(
        18,
        bold=True,
    )
    winner_width = _text_width(
        draw,
        winner_line,
        winner_font,
    )
    draw.text(
        (
            (CANVAS_WIDTH - winner_width) / 2,
            510,
        ),
        winner_line,
        fill=accent_colour,
        font=winner_font,
    )
    return frame


def build_spin_gif(
    sequence: list[dict],
    winner: dict,
    *,
    wheel_type: str = "multiplayer",
    output_path: str | Path | None = None,
) -> SpinGif:
    if not sequence:
        raise ValueError(
            "A spin GIF requires at least one game frame."
        )

    accent_colour = (
        PURPLE_COLOUR
        if wheel_type == "singleplayer"
        else GOLD_COLOUR
    )
    durations = list(
        SPIN_FRAME_DURATIONS_MS[
            : len(sequence)
        ]
    )

    if len(durations) < len(sequence):
        durations.extend(
            [SPIN_FRAME_DURATIONS_MS[-1]]
            * (len(sequence) - len(durations))
        )

    durations.extend(
        (
            SUSPENSE_DURATION_MS,
            WINNER_FLASH_DURATION_MS,
        )
    )

    palette_frames = []
    previous_frame = None

    def quantize_frame(frame: Image.Image) -> Image.Image:
        return frame.quantize(
            colors=96,
            method=Image.Quantize.FASTOCTREE,
            dither=Image.Dither.FLOYDSTEINBERG,
        )

    try:
        for frame_number, card in enumerate(sequence):
            frame = _game_frame(
                card,
                frame_number=frame_number,
                total_frames=len(sequence),
                accent_colour=accent_colour,
            )

            try:
                palette_frames.append(
                    quantize_frame(frame)
                )
            except BaseException:
                frame.close()
                raise

            if previous_frame is not None:
                previous_frame.close()

            previous_frame = frame

        suspense_frame = _suspense_frame(
            previous_frame,
            accent_colour=accent_colour,
        )
        previous_frame.close()
        previous_frame = None
        try:
            palette_frames.append(
                quantize_frame(suspense_frame)
            )
        finally:
            suspense_frame.close()

        winner_frame = _winner_frame(
            winner,
            accent_colour=accent_colour,
        )
        try:
            palette_frames.append(
                quantize_frame(winner_frame)
            )
        finally:
            winner_frame.close()

        gif_save_options = {
            "format": "GIF",
            "save_all": True,
            "append_images": palette_frames[1:],
            "duration": durations,
            "disposal": 1,
            "optimize": True,
        }
        gif_data = None
        gif_size = 0
        output_file_path = (
            Path(output_path)
            if output_path is not None
            else None
        )

        if output_file_path is None:
            with io.BytesIO() as output:
                palette_frames[0].save(
                    output,
                    **gif_save_options,
                )
                gif_data = output.getvalue()
                gif_size = len(gif_data)
        else:
            output_file_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )
            try:
                palette_frames[0].save(
                    output_file_path,
                    **gif_save_options,
                )
                gif_size = (
                    output_file_path.stat().st_size
                )
            except BaseException:
                output_file_path.unlink(
                    missing_ok=True
                )
                raise

        if gif_size > MAX_SPIN_GIF_BYTES:
            if output_file_path is not None:
                output_file_path.unlink(
                    missing_ok=True
                )
            raise ValueError(
                "Generated spin GIF is larger than the "
                "safe Discord upload limit."
            )

        return SpinGif(
            data=gif_data,
            duration_seconds=(
                sum(durations) / 1000
            ),
        )

    finally:
        if previous_frame is not None:
            previous_frame.close()

        for palette_frame in palette_frames:
            palette_frame.close()

        palette_frames.clear()
        gc.collect()

        if os.name == "posix":
            try:
                import ctypes

                malloc_trim = getattr(
                    ctypes.CDLL(None),
                    "malloc_trim",
                    None,
                )
                if malloc_trim is not None:
                    malloc_trim(0)
            except (AttributeError, OSError):
                pass
