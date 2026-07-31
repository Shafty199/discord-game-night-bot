import asyncio
import logging
from time import monotonic

import aiohttp

from settings import (
    STEAM_COUNTRY_CODE,
    STEAM_LANGUAGE,
    STORE_ACCEPT_LANGUAGE,
)
from utils.http_retry import retrying_request


LOGGER = logging.getLogger(
    __name__
)

STEAM_DETAILS_URL = (
    "https://store.steampowered.com/api/appdetails"
)

STEAM_DETAILS_TIMEOUT = aiohttp.ClientTimeout(
    total=15,
    connect=7,
)

STEAM_DETAILS_HEADERS = {
    "User-Agent": (
        "GameNightDiscordBot/1.0 "
        "(Steam metadata lookup)"
    ),
    "Accept": "application/json",
    "Accept-Language": STORE_ACCEPT_LANGUAGE,
}

STEAM_DETAILS_CACHE_TTL_SECONDS = 10 * 60
STEAM_DETAILS_CACHE_LIMIT = 256

_details_cache: dict[
    str,
    tuple[float, dict],
] = {}

_inflight_requests: dict[
    str,
    asyncio.Task,
] = {}

_cache_cleanup_handle: asyncio.TimerHandle | None = None


def clear_steam_details_cache() -> int:
    global _cache_cleanup_handle

    removed = len(_details_cache)
    _details_cache.clear()

    if _cache_cleanup_handle is not None:
        _cache_cleanup_handle.cancel()
        _cache_cleanup_handle = None

    return removed


def _schedule_cache_cleanup() -> None:
    global _cache_cleanup_handle

    if _cache_cleanup_handle is not None:
        _cache_cleanup_handle.cancel()
        _cache_cleanup_handle = None

    if not _details_cache:
        return

    try:
        loop = asyncio.get_running_loop()

    except RuntimeError:
        return

    oldest_cached_at = min(
        cached_at
        for cached_at, _details in _details_cache.values()
    )
    delay = max(
        0.1,
        (
            oldest_cached_at
            + STEAM_DETAILS_CACHE_TTL_SECONDS
            - monotonic()
        ),
    )
    _cache_cleanup_handle = loop.call_later(
        delay,
        _run_scheduled_cache_cleanup,
    )


def _run_scheduled_cache_cleanup() -> None:
    global _cache_cleanup_handle

    _cache_cleanup_handle = None
    prune_steam_details_cache()


def prune_steam_details_cache() -> int:
    now = monotonic()
    expired_app_ids = [
        app_id
        for app_id, (
            cached_at,
            _details,
        ) in _details_cache.items()
        if (
            now - cached_at
            >= STEAM_DETAILS_CACHE_TTL_SECONDS
        )
    ]

    for app_id in expired_app_ids:
        _details_cache.pop(app_id, None)

    _schedule_cache_cleanup()
    return len(expired_app_ids)


def _cached_details(
    app_id: str,
) -> dict | None:
    cached = _details_cache.get(
        app_id
    )

    if not cached:
        return None

    cached_at, details = cached

    if (
        monotonic() - cached_at
        >= STEAM_DETAILS_CACHE_TTL_SECONDS
    ):
        _details_cache.pop(
            app_id,
            None,
        )
        return None

    return details


def _save_cached_details(
    app_id: str,
    details: dict,
) -> None:
    prune_steam_details_cache()

    if (
        len(_details_cache)
        >= STEAM_DETAILS_CACHE_LIMIT
    ):
        oldest_app_id = min(
            _details_cache,
            key=lambda key: _details_cache[key][0],
        )
        _details_cache.pop(
            oldest_app_id,
            None,
        )

    _details_cache[app_id] = (
        monotonic(),
        details,
    )
    _schedule_cache_cleanup()


async def _request_steam_app_data(
    session: aiohttp.ClientSession,
    app_id: str,
) -> dict | None:
    try:
        async with retrying_request(
            session,
            "GET",
            STEAM_DETAILS_URL,
            params={
                "appids": app_id,
                "cc": STEAM_COUNTRY_CODE.casefold(),
                "l": STEAM_LANGUAGE,
            },
            headers=STEAM_DETAILS_HEADERS,
            timeout=STEAM_DETAILS_TIMEOUT,
            allow_redirects=True,
        ) as response:
            if response.status != 200:
                LOGGER.debug(
                    "Steam returned HTTP %s for app %s.",
                    response.status,
                    app_id,
                )
                return None

            payload = await response.json(
                content_type=None
            )

    except (
        asyncio.TimeoutError,
        aiohttp.ClientError,
        ValueError,
        TypeError,
    ) as error:
        LOGGER.debug(
            "Steam app-details request failed for %s: %s: %s",
            app_id,
            type(error).__name__,
            error,
        )
        return None

    if not isinstance(
        payload,
        dict,
    ):
        return None

    app_result = payload.get(
        app_id
    )

    if (
        not isinstance(
            app_result,
            dict,
        )
        or not app_result.get("success")
    ):
        return None

    app_data = app_result.get("data")

    if not isinstance(
        app_data,
        dict,
    ):
        return None

    _save_cached_details(
        app_id,
        app_data,
    )
    return app_data


def _remove_completed_request(
    app_id: str,
    task: asyncio.Task,
) -> None:
    if _inflight_requests.get(
        app_id
    ) is task:
        _inflight_requests.pop(
            app_id,
            None,
        )


async def fetch_steam_app_data(
    session: aiohttp.ClientSession,
    app_id,
    *,
    force_refresh: bool = False,
) -> dict | None:
    clean_app_id = str(
        app_id or ""
    ).strip()

    if not clean_app_id.isdigit():
        return None

    cached = (
        None
        if force_refresh
        else _cached_details(
            clean_app_id
        )
    )

    if cached is not None:
        return cached

    task = _inflight_requests.get(
        clean_app_id
    )

    if task is None:
        task = asyncio.create_task(
            _request_steam_app_data(
                session,
                clean_app_id,
            )
        )
        _inflight_requests[
            clean_app_id
        ] = task
        task.add_done_callback(
            lambda completed_task, key=clean_app_id: (
                _remove_completed_request(
                    key,
                    completed_task,
                )
            )
        )

    return await asyncio.shield(
        task
    )
