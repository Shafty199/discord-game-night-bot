import asyncio
import json
import logging
import math
import re
from collections import OrderedDict
from difflib import SequenceMatcher
from time import time
from urllib.parse import quote, urlparse

import aiohttp

from settings import (
    DATABASE_PATH,
    STEAMGRIDDB_API_KEY,
)
from utils.http_retry import retrying_request


LOGGER = logging.getLogger(
    __name__
)

STEAMGRIDDB_API_BASE = (
    "https://www.steamgriddb.com/api/v2"
)

STEAMGRIDDB_TIMEOUT = aiohttp.ClientTimeout(
    total=12,
    connect=5,
)

TARGET_ARTWORK_ASPECT_RATIO = 592 / 340
MINIMUM_TITLE_MATCH = 0.92
STEAMGRIDDB_SUCCESS_CACHE_SECONDS = 30 * 24 * 60 * 60
STEAMGRIDDB_NEGATIVE_CACHE_SECONDS = 6 * 60 * 60
STEAMGRIDDB_CACHE_LIMIT = 512
STEAMGRIDDB_CACHE_PATH = (
    DATABASE_PATH.parent / "steamgriddb_cache.json"
)

_artwork_cache: OrderedDict[
    str,
    tuple[float, str | None],
] = OrderedDict()
_inflight_requests: dict[
    str,
    asyncio.Task,
] = {}
_cache_loaded = False
_cache_io_lock = asyncio.Lock()


def _normalise_title(
    value: str | None,
) -> str:
    cleaned = str(
        value or ""
    ).casefold()

    cleaned = cleaned.replace(
        "&",
        " and ",
    )

    cleaned = re.sub(
        r"[^a-z0-9]+",
        " ",
        cleaned,
    )

    return re.sub(
        r"\s+",
        " ",
        cleaned,
    ).strip()


def _trim_cache() -> None:
    while len(_artwork_cache) > STEAMGRIDDB_CACHE_LIMIT:
        _artwork_cache.popitem(
            last=False
        )


def _read_cache_file() -> OrderedDict:
    try:
        payload = json.loads(
            STEAMGRIDDB_CACHE_PATH.read_text(
                encoding="utf-8"
            )
        )

    except FileNotFoundError:
        return OrderedDict()

    except (
        OSError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as error:
        LOGGER.warning(
            "Could not read the SteamGridDB cache: %s: %s",
            type(error).__name__,
            error,
        )
        return OrderedDict()

    if not isinstance(payload, dict):
        return OrderedDict()

    entries = payload.get("entries")

    if not isinstance(entries, dict):
        return OrderedDict()

    current_time = time()
    loaded_entries = []

    for cache_key, raw_entry in entries.items():
        if (
            not isinstance(cache_key, str)
            or not isinstance(raw_entry, dict)
        ):
            continue

        try:
            expires_at = float(
                raw_entry.get("expires_at")
            )

        except (TypeError, ValueError):
            continue

        artwork_url = raw_entry.get("url")

        if expires_at <= current_time:
            continue

        if (
            artwork_url is not None
            and not _valid_artwork_url(
                artwork_url
            )
        ):
            continue

        loaded_entries.append(
            (
                expires_at,
                cache_key,
                artwork_url,
            )
        )

    loaded_entries.sort()
    return OrderedDict(
        (
            cache_key,
            (expires_at, artwork_url),
        )
        for (
            expires_at,
            cache_key,
            artwork_url,
        ) in loaded_entries[
            -STEAMGRIDDB_CACHE_LIMIT:
        ]
    )


def _write_cache_file(
    payload: dict,
) -> None:
    STEAMGRIDDB_CACHE_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_path = STEAMGRIDDB_CACHE_PATH.with_suffix(
        ".json.tmp"
    )
    temporary_path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    temporary_path.replace(
        STEAMGRIDDB_CACHE_PATH
    )


async def _ensure_cache_loaded() -> None:
    global _cache_loaded

    if _cache_loaded:
        return

    async with _cache_io_lock:
        if _cache_loaded:
            return

        loaded_entries = await asyncio.to_thread(
            _read_cache_file
        )
        _artwork_cache.update(
            loaded_entries
        )
        _trim_cache()
        _cache_loaded = True


async def _persist_cache() -> None:
    async with _cache_io_lock:
        payload = {
            "version": 1,
            "entries": {
                cache_key: {
                    "url": artwork_url,
                    "expires_at": expires_at,
                }
                for cache_key, (
                    expires_at,
                    artwork_url,
                ) in _artwork_cache.items()
            },
        }

        try:
            await asyncio.to_thread(
                _write_cache_file,
                payload,
            )

        except (
            OSError,
            TypeError,
            ValueError,
        ) as error:
            LOGGER.warning(
                "Could not save the SteamGridDB cache: %s: %s",
                type(error).__name__,
                error,
            )


def _cached_artwork(
    cache_key: str,
) -> tuple[bool, str | None]:
    cached = _artwork_cache.get(
        cache_key
    )

    if cached is None:
        return False, None

    expires_at, artwork_url = cached

    if expires_at <= time():
        _artwork_cache.pop(
            cache_key,
            None,
        )
        return False, None

    _artwork_cache.move_to_end(
        cache_key
    )
    return True, artwork_url


async def _remember_artwork(
    cache_key: str,
    artwork_url: str | None,
) -> None:
    cache_seconds = (
        STEAMGRIDDB_SUCCESS_CACHE_SECONDS
        if artwork_url
        else STEAMGRIDDB_NEGATIVE_CACHE_SECONDS
    )

    _artwork_cache.pop(
        cache_key,
        None,
    )
    _artwork_cache[cache_key] = (
        time() + cache_seconds,
        artwork_url,
    )
    _trim_cache()
    await _persist_cache()


def _number(
    value,
) -> float:
    try:
        return float(value)

    except (TypeError, ValueError):
        return 0.0


def _valid_artwork_url(
    value,
) -> bool:
    try:
        parsed = urlparse(
            str(value or "").strip()
        )

    except ValueError:
        return False

    if (
        parsed.scheme.casefold() != "https"
        or parsed.username
        or parsed.password
    ):
        return False

    hostname = (
        parsed.hostname
        or ""
    ).casefold().rstrip(".")

    return bool(
        hostname == "steamgriddb.com"
        or hostname.endswith(
            ".steamgriddb.com"
        )
        or hostname == "s3.amazonaws.com"
        or hostname.endswith(
            ".amazonaws.com"
        )
    )


def _select_game_match(
    game_name: str,
    search_results,
) -> dict | None:
    wanted = _normalise_title(
        game_name
    )

    if (
        not wanted
        or not isinstance(
            search_results,
            list,
        )
    ):
        return None

    candidates = []

    for result in search_results:
        if not isinstance(
            result,
            dict,
        ):
            continue

        try:
            game_id = int(
                result.get("id")
            )

        except (TypeError, ValueError):
            continue

        if game_id <= 0:
            continue

        candidate_name = _normalise_title(
            result.get("name")
        )

        if not candidate_name:
            continue

        similarity = SequenceMatcher(
            None,
            wanted,
            candidate_name,
        ).ratio()

        exact = candidate_name == wanted
        verified = bool(
            result.get("verified")
        )

        candidates.append(
            (
                exact,
                similarity,
                verified,
                result,
            )
        )

    if not candidates:
        return None

    exact_matches = [
        candidate
        for candidate in candidates
        if candidate[0]
    ]

    if exact_matches:
        return max(
            exact_matches,
            key=lambda candidate: (
                candidate[2],
                candidate[1],
            ),
        )[3]

    best_match = max(
        candidates,
        key=lambda candidate: (
            candidate[1],
            candidate[2],
        ),
    )

    if best_match[1] < MINIMUM_TITLE_MATCH:
        return None

    return best_match[3]


def _unsafe_artwork(
    artwork: dict,
) -> bool:
    def is_true(value) -> bool:
        if isinstance(value, bool):
            return value

        return str(
            value or ""
        ).strip().casefold() in {
            "1",
            "true",
            "yes",
        }

    if any(
        is_true(
            artwork.get(flag)
        )
        for flag in (
            "nsfw",
            "humor",
            "epilepsy",
        )
    ):
        return True

    tags = artwork.get("tags")

    if isinstance(tags, str):
        tags = [tags]

    if isinstance(tags, list):
        unsafe_tags = {
            "nsfw",
            "humor",
            "epilepsy",
        }

        if any(
            _normalise_title(tag)
            in unsafe_tags
            for tag in tags
        ):
            return True

    return False


def _select_artwork(
    artwork_groups,
) -> str | None:
    candidates = []

    for source_kind, artworks in artwork_groups:
        if not isinstance(
            artworks,
            list,
        ):
            continue

        for artwork in artworks:
            if (
                not isinstance(
                    artwork,
                    dict,
                )
                or _unsafe_artwork(
                    artwork
                )
            ):
                continue

            artwork_url = str(
                artwork.get("url")
                or ""
            ).strip()

            if not _valid_artwork_url(
                artwork_url
            ):
                continue

            width = _number(
                artwork.get("width")
            )
            height = _number(
                artwork.get("height")
            )

            if width <= 0 or height <= 0:
                continue

            aspect_ratio = width / height
            aspect_distance = abs(
                math.log(
                    aspect_ratio
                    / TARGET_ARTWORK_ASPECT_RATIO
                )
            )

            style = artwork.get("style")

            if isinstance(style, list):
                style = " ".join(
                    str(value)
                    for value in style
                )

            preferred_style = (
                "official"
                in str(
                    style or ""
                ).casefold()
            )

            votes = (
                _number(
                    artwork.get("upvotes")
                )
                - _number(
                    artwork.get("downvotes")
                )
            )

            candidates.append(
                (
                    -aspect_distance,
                    source_kind == "grid",
                    preferred_style,
                    votes,
                    _number(
                        artwork.get("score")
                    ),
                    width * height,
                    artwork_url,
                )
            )

    if not candidates:
        return None

    return max(
        candidates
    )[-1]


async def _request_data(
    session: aiohttp.ClientSession,
    path: str,
    *,
    params: dict | None = None,
):
    if not STEAMGRIDDB_API_KEY:
        return None

    try:
        async with retrying_request(
            session,
            "GET",
            f"{STEAMGRIDDB_API_BASE}{path}",
            headers={
                "Authorization": (
                    "Bearer "
                    f"{STEAMGRIDDB_API_KEY}"
                ),
                "Accept": "application/json",
                "User-Agent": (
                    "GameNightDiscordBot/1.0"
                ),
            },
            params=params,
            timeout=STEAMGRIDDB_TIMEOUT,
        ) as response:
            if response.status >= 400:
                log_method = (
                    LOGGER.warning
                    if response.status in {
                        401,
                        403,
                        429,
                    }
                    else LOGGER.debug
                )

                log_method(
                    "SteamGridDB request failed with HTTP %s.",
                    response.status,
                )
                return None

            payload = await response.json(
                content_type=None
            )

    except asyncio.TimeoutError:
        LOGGER.debug(
            "SteamGridDB request timed out."
        )
        return None

    except Exception as error:
        LOGGER.debug(
            "SteamGridDB request failed: %s: %s",
            type(error).__name__,
            error,
        )
        return None

    if (
        not isinstance(payload, dict)
        or payload.get("success") is not True
    ):
        return None

    return payload.get("data")


async def _lookup_steamgriddb_artwork(
    session: aiohttp.ClientSession,
    game_name: str,
    cache_key: str,
) -> str | None:
    search_results = await _request_data(
        session,
        "/search/autocomplete/"
        f"{quote(game_name, safe='')}",
    )

    if search_results is None:
        return None

    game_match = _select_game_match(
        game_name,
        search_results,
    )

    if not game_match:
        LOGGER.debug(
            "No trustworthy SteamGridDB title match for %s.",
            game_name,
        )
        await _remember_artwork(
            cache_key,
            None,
        )
        return None

    game_id = int(
        game_match["id"]
    )

    safe_filters = {
        "types": "static",
        "nsfw": "false",
        "humor": "false",
        "epilepsy": "false",
    }

    grids, heroes = await asyncio.gather(
        _request_data(
            session,
            f"/grids/game/{game_id}",
            params=safe_filters,
        ),
        _request_data(
            session,
            f"/heroes/game/{game_id}",
            params=safe_filters,
        ),
    )

    artwork_url = _select_artwork(
        (
            ("grid", grids),
            ("hero", heroes),
        )
    )

    if not artwork_url:
        LOGGER.debug(
            "SteamGridDB returned no suitable artwork for %s.",
            game_name,
        )

        if grids is not None and heroes is not None:
            await _remember_artwork(
                cache_key,
                None,
            )

        return None

    await _remember_artwork(
        cache_key,
        artwork_url,
    )

    LOGGER.info(
        "SteamGridDB artwork fallback selected for %s.",
        game_name,
    )

    return artwork_url


def _remove_completed_request(
    cache_key: str,
    task: asyncio.Task,
) -> None:
    if _inflight_requests.get(
        cache_key
    ) is task:
        _inflight_requests.pop(
            cache_key,
            None,
        )


async def get_steamgriddb_artwork(
    session: aiohttp.ClientSession,
    game_name: str | None,
) -> str | None:
    cache_key = _normalise_title(
        game_name
    )

    if not cache_key:
        return None

    await _ensure_cache_loaded()

    cache_hit, cached_url = _cached_artwork(
        cache_key
    )

    if cache_hit:
        return cached_url

    if not STEAMGRIDDB_API_KEY:
        return None

    task = _inflight_requests.get(
        cache_key
    )

    if task is None:
        task = asyncio.create_task(
            _lookup_steamgriddb_artwork(
                session,
                str(game_name),
                cache_key,
            )
        )
        _inflight_requests[
            cache_key
        ] = task
        task.add_done_callback(
            lambda completed_task, key=cache_key: (
                _remove_completed_request(
                    key,
                    completed_task,
                )
            )
        )

    return await asyncio.shield(
        task
    )
