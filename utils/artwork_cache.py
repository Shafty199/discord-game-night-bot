import asyncio
import io
import json
import logging
import os
from pathlib import Path

import aiohttp
from PIL import Image, ImageOps

from settings import LOCAL_ARTWORK_CACHE_DIRECTORY
from utils.http_retry import retrying_request
from utils.spin_gif import (
    SPIN_GIF_TEMP_DIRECTORY,
    get_static_game_card_path,
    prepare_static_game_card,
)
from utils.time_utils import utc_now_iso


LOGGER = logging.getLogger(__name__)


MAX_ARTWORK_BYTES = 8 * 1024 * 1024
LOCAL_ARTWORK_MAX_SIZE = (720, 405)
ARTWORK_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(
    total=20
)
ARTWORK_DOWNLOAD_HEADERS = {
    "User-Agent": (
        "GameNightDiscordBot/1.0 "
        "(artwork cache)"
    )
}
ARTWORK_MANIFEST_PATH = (
    LOCAL_ARTWORK_CACHE_DIRECTORY
    / "manifest.json"
)
ARTWORK_MANIFEST_VERSION = 2
STATIC_CARD_CACHE_DIRECTORY = (
    LOCAL_ARTWORK_CACHE_DIRECTORY / "cards"
)
MAX_STATIC_CARD_CACHE_FILES = 200

_local_artwork_locks: dict[
    int,
    asyncio.Lock,
] = {}
_local_card_locks: dict[
    int,
    asyncio.Lock,
] = {}
_artwork_preparation_semaphore = asyncio.Semaphore(1)
_manifest_lock = asyncio.Lock()
_manifest_cache: dict | None = None
_manifest_dirty = False
_manifest_flush_task: asyncio.Task | None = None
_validated_artwork_files: set[
    tuple[str, int, int]
] = set()


def _read_manifest() -> dict:
    try:
        loaded = json.loads(
            ARTWORK_MANIFEST_PATH.read_text(
                encoding="utf-8"
            )
        )

        if (
            isinstance(loaded, dict)
            and loaded.get("version") in {1, 2}
            and isinstance(loaded.get("games"), dict)
        ):
            # Version 1 entries remain valid. Their exact
            # card filename is filled in lazily the first
            # time each existing card is encountered.
            loaded["version"] = (
                ARTWORK_MANIFEST_VERSION
            )
            return loaded

    except (OSError, ValueError, TypeError):
        pass

    return {
        "version": ARTWORK_MANIFEST_VERSION,
        "games": {},
    }


def _write_manifest(manifest: dict) -> None:
    ARTWORK_MANIFEST_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    temporary_path = ARTWORK_MANIFEST_PATH.with_suffix(
        ".tmp"
    )

    try:
        temporary_path.write_text(
            json.dumps(
                manifest,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.replace(
            temporary_path,
            ARTWORK_MANIFEST_PATH,
        )

    finally:
        temporary_path.unlink(missing_ok=True)


async def _get_manifest() -> dict:
    global _manifest_cache

    async with _manifest_lock:
        if _manifest_cache is None:
            _manifest_cache = await asyncio.to_thread(
                _read_manifest
            )

        return _manifest_cache


async def _record_manifest_entry(
    *,
    game_id: int,
    image_url: str,
    artwork_path: Path,
) -> None:
    global _manifest_cache, _manifest_dirty
    global _manifest_flush_task

    try:
        file_size = artwork_path.stat().st_size
    except OSError:
        return

    async with _manifest_lock:
        if _manifest_cache is None:
            _manifest_cache = await asyncio.to_thread(
                _read_manifest
            )

        _manifest_cache["games"][str(game_id)] = {
            "source_url": str(image_url or ""),
            "card_filename": artwork_path.name,
            "file_size": file_size,
            "updated_at": utc_now_iso(),
        }
        _manifest_dirty = True

        if (
            _manifest_flush_task is None
            or _manifest_flush_task.done()
        ):
            _manifest_flush_task = asyncio.create_task(
                _delayed_manifest_flush()
            )


def _indexed_card_path(
    game_id,
    manifest_entry,
) -> Path | None:
    """Resolve a manifest card without scanning its directory."""

    if not isinstance(manifest_entry, dict):
        return None

    try:
        clean_game_id = int(game_id)
    except (TypeError, ValueError):
        return None

    filename = str(
        manifest_entry.get("card_filename") or ""
    ).strip()

    if (
        not filename
        or Path(filename).name != filename
        or not filename.startswith(
            f"game-{clean_game_id}-"
        )
        or not filename.casefold().endswith(".jpg")
    ):
        return None

    card_path = (
        STATIC_CARD_CACHE_DIRECTORY / filename
    )

    try:
        file_details = card_path.stat()
    except OSError:
        return None

    if file_details.st_size <= 0:
        return None

    expected_size = manifest_entry.get(
        "file_size"
    )

    try:
        expected_size = int(expected_size)
    except (TypeError, ValueError):
        expected_size = None

    if (
        expected_size is not None
        and expected_size > 0
        and file_details.st_size != expected_size
    ):
        return None

    return card_path


async def get_cached_game_card_path(
    game_id,
) -> Path | None:
    """Return a cached card by manifest, with legacy fallback."""

    manifest = await _get_manifest()
    manifest_entry = manifest["games"].get(
        str(game_id)
    )
    card_path = _indexed_card_path(
        game_id,
        manifest_entry,
    )

    if card_path is not None:
        return card_path

    card_path = await asyncio.to_thread(
        get_static_game_card_path,
        game_id,
    )

    if (
        card_path is not None
        and isinstance(manifest_entry, dict)
    ):
        await _record_manifest_entry(
            game_id=int(game_id),
            image_url=manifest_entry.get(
                "source_url",
                "",
            ),
            artwork_path=card_path,
        )

    return card_path


async def _remove_manifest_entries(
    game_ids,
) -> None:
    global _manifest_cache, _manifest_dirty
    global _manifest_flush_task

    cleaned_ids = {
        str(int(game_id))
        for game_id in game_ids
    }

    if not cleaned_ids:
        return

    async with _manifest_lock:
        if _manifest_cache is None:
            _manifest_cache = await asyncio.to_thread(
                _read_manifest
            )

        changed = False

        for game_id in cleaned_ids:
            if _manifest_cache["games"].pop(
                game_id,
                None,
            ) is not None:
                changed = True

        if changed:
            _manifest_dirty = True

            if (
                _manifest_flush_task is None
                or _manifest_flush_task.done()
            ):
                _manifest_flush_task = asyncio.create_task(
                    _delayed_manifest_flush()
                )


async def flush_artwork_manifest() -> None:
    global _manifest_cache, _manifest_dirty

    async with _manifest_lock:
        if not _manifest_dirty:
            return

        if _manifest_cache is None:
            _manifest_cache = await asyncio.to_thread(
                _read_manifest
            )

        await asyncio.to_thread(
            _write_manifest,
            _manifest_cache,
        )
        _manifest_dirty = False


def _cleanup_temporary_artwork_files_sync() -> int:
    temporary_directories = (
        LOCAL_ARTWORK_CACHE_DIRECTORY,
        STATIC_CARD_CACHE_DIRECTORY,
        SPIN_GIF_TEMP_DIRECTORY,
    )
    removed = 0

    for directory in temporary_directories:
        try:
            temporary_files = {
                *directory.glob("*.tmp"),
                *directory.glob(".*.tmp"),
            }

            if directory == SPIN_GIF_TEMP_DIRECTORY:
                temporary_files.update(
                    directory.glob("*.gif")
                )

        except OSError:
            continue

        for temporary_file in temporary_files:
            try:
                if temporary_file.is_file():
                    temporary_file.unlink(
                        missing_ok=True
                    )
                    removed += 1

            except OSError:
                continue

    database_directory = (
        LOCAL_ARTWORK_CACHE_DIRECTORY.parent
    )
    extra_patterns = (
        (database_directory, "*.json.tmp"),
        (
            database_directory / "backups",
            "games-auto-*.db.tmp",
        ),
    )

    for directory, pattern in extra_patterns:
        try:
            temporary_files = list(
                directory.glob(pattern)
            )
        except OSError:
            continue

        for temporary_file in temporary_files:
            try:
                if temporary_file.is_file():
                    temporary_file.unlink(
                        missing_ok=True
                    )
                    removed += 1
            except OSError:
                continue

    return removed


async def cleanup_temporary_artwork_files() -> int:
    """Remove files left behind by interrupted renders/cache writes."""

    return await asyncio.to_thread(
        _cleanup_temporary_artwork_files_sync
    )


async def _delayed_manifest_flush() -> None:
    await asyncio.sleep(0.1)

    try:
        await flush_artwork_manifest()
    except Exception:
        LOGGER.exception(
            "Could not save the local artwork manifest"
        )


def _verify_artwork_file(artwork_path: Path) -> bool:
    try:
        with Image.open(artwork_path) as artwork:
            artwork.verify()

        with Image.open(artwork_path) as artwork:
            return (
                artwork.width > 0
                and artwork.height > 0
            )

    except (OSError, TypeError, ValueError):
        return False


async def _is_valid_artwork_file(
    artwork_path: Path,
) -> bool:
    try:
        file_details = artwork_path.stat()
        signature = (
            str(artwork_path.resolve()),
            file_details.st_mtime_ns,
            file_details.st_size,
        )

    except OSError:
        return False

    if file_details.st_size <= 0:
        return False

    if signature in _validated_artwork_files:
        return True

    valid = await asyncio.to_thread(
        _verify_artwork_file,
        artwork_path,
    )

    if valid:
        _validated_artwork_files.add(signature)

    return valid


def get_local_artwork_path(
    game_id,
) -> Path | None:
    try:
        clean_game_id = int(game_id)

    except (
        TypeError,
        ValueError,
    ):
        return None

    if clean_game_id <= 0:
        return None

    return (
        LOCAL_ARTWORK_CACHE_DIRECTORY
        / f"game-{clean_game_id}.jpg"
    )


def _write_local_artwork(
    image_data: bytes,
    artwork_path: Path,
) -> None:
    artwork_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_path = artwork_path.with_suffix(
        ".tmp"
    )

    try:
        with Image.open(
            io.BytesIO(image_data)
        ) as source_image:
            artwork = ImageOps.exif_transpose(
                source_image
            ).convert("RGB")
            try:
                artwork.thumbnail(
                    LOCAL_ARTWORK_MAX_SIZE,
                    Image.Resampling.LANCZOS,
                )
                artwork.save(
                    temporary_path,
                    format="JPEG",
                    quality=82,
                    optimize=True,
                    progressive=True,
                )
            finally:
                artwork.close()

        os.replace(
            temporary_path,
            artwork_path,
        )

    finally:
        temporary_path.unlink(
            missing_ok=True
        )


async def _download_artwork(
    session: aiohttp.ClientSession,
    image_url: str,
) -> bytes:
    async with retrying_request(
        session,
        "GET",
        image_url,
        headers=ARTWORK_DOWNLOAD_HEADERS,
        timeout=ARTWORK_DOWNLOAD_TIMEOUT,
        allow_redirects=True,
    ) as response:
        response.raise_for_status()

        content_length = response.headers.get(
            "Content-Length"
        )

        if content_length:
            try:
                declared_size = int(
                    content_length
                )

            except ValueError:
                declared_size = 0

            if declared_size > MAX_ARTWORK_BYTES:
                raise ValueError(
                    "Artwork is larger than the "
                    "8 MB cache limit."
                )

        image_buffer = bytearray()

        async for chunk in response.content.iter_chunked(
            64 * 1024
        ):
            image_buffer.extend(
                chunk
            )

            if len(image_buffer) > MAX_ARTWORK_BYTES:
                raise ValueError(
                    "Artwork is larger than the "
                    "8 MB cache limit."
                )

        image_data = bytes(
            image_buffer
        )

        if not image_data:
            raise ValueError(
                "The artwork download was empty."
            )

        content_type = response.headers.get(
            "Content-Type"
        )

        if (
            content_type
            and not content_type.lower().startswith(
                "image/"
            )
        ):
            raise ValueError(
                "The artwork URL did not return "
                "an image."
            )

        return image_data


async def ensure_local_artwork(
    *,
    session: aiohttp.ClientSession,
    game_id,
    image_url: str,
    image_data: bytes | None = None,
    log_errors: bool = True,
) -> Path | None:
    artwork_path = get_local_artwork_path(
        game_id
    )

    if artwork_path is None:
        return None

    try:
        clean_game_id = int(game_id)
        lock = _local_artwork_locks.setdefault(
            clean_game_id,
            asyncio.Lock(),
        )

        async with lock:
            if (
                await _is_valid_artwork_file(
                    artwork_path
                )
                and image_data is None
            ):
                return artwork_path

            local_image_data = image_data

            if local_image_data is None:
                local_image_data = await _download_artwork(
                    session,
                    image_url,
                )

            await asyncio.to_thread(
                _write_local_artwork,
                local_image_data,
                artwork_path,
            )
            await _record_manifest_entry(
                game_id=clean_game_id,
                image_url=image_url,
                artwork_path=artwork_path,
            )
            return artwork_path

    except Exception as error:
        log_method = (
            LOGGER.warning
            if log_errors
            else LOGGER.debug
        )
        log_method(
            "Could not build local artwork cache for "
            "game %s: %s: %s",
            game_id,
            type(error).__name__,
            error,
        )
        return None


async def prepare_local_game_artwork(
    *,
    bot,
    game_record: dict,
    refresh: bool = False,
) -> str:
    """Download source art temporarily and persist its wheel card."""

    session = getattr(
        bot,
        "http_session",
        None,
    )

    if session is None:
        LOGGER.error(
            "Cannot prepare a local game card for %s without "
            "the bot HTTP session.",
            game_record.get(
                "name",
                "Unknown Game",
            ),
        )
        return "failed"

    return await prepare_local_game_card(
        session=session,
        game_record=game_record,
        refresh=refresh,
    )


async def prepare_local_game_card(
    *,
    session: aiohttp.ClientSession,
    game_record: dict,
    refresh: bool = False,
) -> str:
    """Serialise creation of each game's persistent card."""

    try:
        game_id = int(game_record.get("id"))
    except (TypeError, ValueError):
        return "failed"

    lock = _local_card_locks.setdefault(
        game_id,
        asyncio.Lock(),
    )

    async with lock:
        async with _artwork_preparation_semaphore:
            return await _prepare_local_game_card(
                session=session,
                game_record=game_record,
                refresh=refresh,
            )


async def _prepare_local_game_card(
    *,
    session: aiohttp.ClientSession,
    game_record: dict,
    refresh: bool = False,
) -> str:
    """Ensure a reusable card exists without retaining source art."""

    image_url = str(
        game_record.get("image_url")
        or ""
    ).strip()

    if not image_url:
        return "no_artwork"

    artwork_path = get_local_artwork_path(
        game_record.get("id")
    )

    if artwork_path is None:
        return "failed"

    try:
        manifest = await _get_manifest()
        manifest_entry = manifest["games"].get(
            str(int(game_record.get("id")))
        )
        card_path = _indexed_card_path(
            game_record.get("id"),
            manifest_entry,
        )

        if card_path is None:
            card_path = await asyncio.to_thread(
                get_static_game_card_path,
                game_record.get("id"),
            )

        valid_card = bool(
            card_path is not None
            and await _is_valid_artwork_file(
                card_path
            )
        )
        valid_local_artwork = (
            await _is_valid_artwork_file(
                artwork_path
            )
        )

        if (
            not refresh
            and valid_card
            and (
                manifest_entry is None
                or manifest_entry.get("source_url")
                == image_url
            )
        ):
            if (
                manifest_entry is None
                or manifest_entry.get(
                    "card_filename"
                ) != card_path.name
            ):
                await _record_manifest_entry(
                    game_id=int(game_record.get("id")),
                    image_url=image_url,
                    artwork_path=card_path,
                )
            return "already_cached"

        source_matches = bool(
            manifest_entry is None
            or manifest_entry.get("source_url")
            == image_url
        )

        if (
            not refresh
            and valid_local_artwork
            and source_matches
        ):
            prepared_path = artwork_path
        else:
            image_data = await _download_artwork(
                session,
                image_url,
            )

            prepared_path = await ensure_local_artwork(
                session=session,
                game_id=game_record.get("id"),
                image_url=image_url,
                image_data=image_data,
            )

        if prepared_path is None:
            return "failed"

        card_path = await asyncio.to_thread(
            prepare_static_game_card,
            {
                "id": game_record.get("id"),
                "name": game_record.get("name"),
                "store": game_record.get("store"),
                "artwork_path": str(prepared_path),
            },
        )

        if (
            card_path is None
            or not await _is_valid_artwork_file(
                card_path
            )
        ):
            raise RuntimeError(
                "The rendered game card was not created."
            )

        await _record_manifest_entry(
            game_id=int(game_record.get("id")),
            image_url=image_url,
            artwork_path=card_path,
        )

    except Exception:
        existing_card_path = (
            await get_cached_game_card_path(
                game_record.get("id")
            )
        )
        existing_card = bool(
            existing_card_path is not None
            and await _is_valid_artwork_file(
                existing_card_path
            )
        )

        if existing_card:
            LOGGER.warning(
                "Could not refresh the card for %s; using the "
                "existing rendered copy.",
                game_record.get(
                    "name",
                    "Unknown Game",
                ),
            )
            return "already_cached"

        LOGGER.exception(
            "Failed to prepare the local card for %s",
            game_record.get(
                "name",
                "Unknown Game",
            ),
        )
        return "failed"

    finally:
        try:
            await asyncio.to_thread(
                artwork_path.unlink,
                missing_ok=True,
            )
        except OSError:
            LOGGER.debug(
                "Could not remove temporary artwork for game %s",
                game_record.get("id"),
            )

    return "cached"


async def delete_local_game_artwork(
    game_id,
) -> bool:
    """Remove a game's temporary art and persisted card files."""
    artwork_path = get_local_artwork_path(
        game_id
    )
    local_deleted = False

    if artwork_path is not None:
        try:
            local_deleted = artwork_path.is_file()
            await asyncio.to_thread(
                artwork_path.unlink,
                missing_ok=True,
            )

        except OSError as error:
            LOGGER.warning(
                "Could not delete local artwork for game %s: %s",
                game_id,
                error,
            )

    try:
        clean_game_id = int(game_id)
        card_paths = list(
            STATIC_CARD_CACHE_DIRECTORY.glob(
                f"game-{clean_game_id}-*.jpg"
            )
        )
    except (OSError, TypeError, ValueError):
        card_paths = []

    for card_path in card_paths:
        try:
            local_deleted = (
                card_path.is_file()
                or local_deleted
            )
            await asyncio.to_thread(
                card_path.unlink,
                missing_ok=True,
            )
        except OSError as error:
            LOGGER.warning(
                "Could not delete the cached card for game %s: %s",
                game_id,
                error,
            )

    try:
        await _remove_manifest_entries(
            (int(game_id),)
        )

    except (TypeError, ValueError):
        pass

    try:
        _local_artwork_locks.pop(
            int(game_id),
            None,
        )
        _local_card_locks.pop(
            int(game_id),
            None,
        )

    except (TypeError, ValueError):
        pass

    return local_deleted


async def remove_redundant_source_artwork() -> int:
    """Remove legacy source files when a rendered card exists."""

    try:
        artwork_files = list(
            LOCAL_ARTWORK_CACHE_DIRECTORY.glob(
                "game-*.jpg"
            )
        )
    except OSError:
        return 0

    removed = 0

    for artwork_path in artwork_files:
        try:
            game_id = int(
                artwork_path.stem.removeprefix(
                    "game-"
                )
            )
        except ValueError:
            continue

        card_path = await get_cached_game_card_path(
            game_id
        )

        if (
            card_path is None
            or not await _is_valid_artwork_file(
                card_path
            )
        ):
            continue

        try:
            await asyncio.to_thread(
                artwork_path.unlink,
                missing_ok=True,
            )
            removed += 1
        except OSError:
            continue

    return removed


def _prune_static_card_cache(
    valid_game_ids: set[int],
) -> int:
    """Remove cards for deleted games and cap stale disk entries."""

    try:
        card_files = list(
            STATIC_CARD_CACHE_DIRECTORY.glob("*.jpg")
        )
    except OSError:
        return 0

    pruned = 0
    retained_files = []

    for card_path in card_files:
        obsolete = False

        if card_path.stem.startswith("game-"):
            try:
                game_id = int(
                    card_path.stem.split("-", 2)[1]
                )
                obsolete = game_id not in valid_game_ids
            except (IndexError, ValueError):
                pass

        if obsolete:
            try:
                card_path.unlink(missing_ok=True)
                pruned += 1
            except OSError:
                retained_files.append(card_path)
        else:
            retained_files.append(card_path)

    if len(retained_files) <= MAX_STATIC_CARD_CACHE_FILES:
        return pruned

    def modified_time(card_path: Path) -> int:
        try:
            return card_path.stat().st_mtime_ns
        except OSError:
            return 0

    retained_files.sort(key=modified_time)
    excess_count = (
        len(retained_files)
        - MAX_STATIC_CARD_CACHE_FILES
    )

    for card_path in retained_files[:excess_count]:
        try:
            card_path.unlink(missing_ok=True)
            pruned += 1
        except OSError:
            continue

    return pruned


async def maintain_local_artwork(
    *,
    bot,
    game_records: list[dict],
    checked_game_ids: set[int] | None = None,
) -> dict:
    """Repair missing/corrupt art and remove files for deleted games."""

    valid_game_ids = {
        int(record["id"])
        for record in game_records
        if record.get("id") is not None
    }
    already_checked_ids = {
        int(game_id)
        for game_id in (checked_game_ids or set())
    }
    orphaned_ids = []

    try:
        cached_files = list(
            LOCAL_ARTWORK_CACHE_DIRECTORY.glob(
                "game-*.jpg"
            )
        )
    except OSError:
        cached_files = []

    for artwork_path in cached_files:
        try:
            game_id = int(
                artwork_path.stem.removeprefix(
                    "game-"
                )
            )
        except ValueError:
            continue

        if game_id not in valid_game_ids:
            await asyncio.to_thread(
                artwork_path.unlink,
                missing_ok=True,
            )
            orphaned_ids.append(game_id)

    if orphaned_ids:
        await _remove_manifest_entries(orphaned_ids)

    repair_limit = asyncio.Semaphore(5)

    async def check_record(record: dict) -> tuple[str, bool]:
        async with repair_limit:
            card_path = await get_cached_game_card_path(
                record.get("id")
            )
            was_corrupt = bool(
                card_path is not None
                and card_path.exists()
                and not await _is_valid_artwork_file(
                    card_path
                )
            )
            result = await prepare_local_game_artwork(
                bot=bot,
                game_record=record,
                refresh=False,
            )

        return result, was_corrupt

    results = await asyncio.gather(
        *(
            check_record(record)
            for record in game_records
            if record.get("id") is not None
            and int(record["id"]) not in already_checked_ids
        ),
        return_exceptions=True,
    )
    cards_pruned = await asyncio.to_thread(
        _prune_static_card_cache,
        valid_game_ids,
    )
    try:
        await flush_artwork_manifest()
    except Exception:
        LOGGER.exception(
            "Could not save the local artwork manifest "
            "after maintenance"
        )
    repaired = 0
    corrupt = 0
    failed = 0

    for result in results:
        if isinstance(result, BaseException):
            failed += 1
            continue

        status, was_corrupt = result

        if was_corrupt:
            corrupt += 1

        if status == "cached":
            repaired += 1
        elif status == "failed":
            failed += 1

    return {
        "repaired": repaired,
        "corrupt": corrupt,
        "orphaned": len(orphaned_ids),
        "failed": failed,
        "cards_pruned": cards_pruned,
    }
