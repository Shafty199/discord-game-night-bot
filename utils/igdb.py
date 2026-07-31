import asyncio
import json
import logging
import re
import time
from collections import OrderedDict, deque

import aiohttp

from settings import (
    DATABASE_PATH,
    IGDB_CLIENT_ID,
    IGDB_CLIENT_SECRET,
)
from utils.http_retry import retrying_request


LOGGER = logging.getLogger(__name__)

TWITCH_TOKEN_URL = (
    "https://id.twitch.tv/oauth2/token"
)
IGDB_API_BASE = "https://api.igdb.com/v4"
IGDB_TIMEOUT = aiohttp.ClientTimeout(
    total=15,
    connect=6,
)
IGDB_PC_PLATFORM_ID = 6
IGDB_RATE_LIMIT = 4
IGDB_SUCCESS_CACHE_SECONDS = 30 * 24 * 60 * 60
IGDB_NEGATIVE_CACHE_SECONDS = 24 * 60 * 60
IGDB_CACHE_LIMIT = 512
IGDB_CACHE_PATH = (
    DATABASE_PATH.parent / "igdb_players_cache.json"
)
IGDB_CACHE_VERSION = 9

PLAYER_FIELDS = (
    "name,platforms,game_modes.name,"
    "genres.name,themes.name,"
    "multiplayer_modes.platform,"
    "multiplayer_modes.campaigncoop,"
    "multiplayer_modes.dropin,"
    "multiplayer_modes.lancoop,"
    "multiplayer_modes.offlinecoop,"
    "multiplayer_modes.offlinecoopmax,"
    "multiplayer_modes.offlinemax,"
    "multiplayer_modes.onlinecoop,"
    "multiplayer_modes.onlinecoopmax,"
    "multiplayer_modes.onlinemax,"
    "multiplayer_modes.splitscreen,"
    "multiplayer_modes.splitscreenonline"
)

GAME_MODE_NAMES = {
    1: "Single player",
    2: "Multiplayer",
    3: "Co-operative",
    4: "Split screen",
    5: "Massively Multiplayer Online (MMO)",
    6: "Battle Royale",
}

MAX_DISPLAY_GENRES = 3
LOW_PRIORITY_GENRES = {
    "indie",
}
PROMOTED_THEME_GENRES = {
    "action",
    "horror",
    "mystery",
    "open world",
    "party",
    "sandbox",
    "stealth",
    "survival",
}

MULTIPLAYER_BOOLEAN_FIELDS = (
    ("campaigncoop", "campaign_coop"),
    ("dropin", "drop_in"),
    ("lancoop", "lan_coop"),
    ("offlinecoop", "offline_coop"),
    ("onlinecoop", "online_coop"),
    ("splitscreen", "split_screen"),
    ("splitscreenonline", "split_screen_online"),
)

MULTIPLAYER_COUNT_FIELDS = (
    ("offlinecoopmax", "offline_coop_max"),
    ("offlinemax", "offline_max"),
    ("onlinecoopmax", "online_coop_max"),
    ("onlinemax", "online_max"),
)

# IGDB occasionally omits multiplayer details or classifies a
# competitive mode as co-op. Keep title-specific corrections
# deliberately small and verify them against official sources.
VERIFIED_MULTIPLAYER_OVERRIDES = {
    # Bodycam supports ten-player matches, including official
    # 5-vs-5 Team Deathmatch and Body Bomb modes.
    "bodycam": {
        "remove_coop": True,
        "support": {
            "online_multiplayer": True,
            "online_max": 10,
            "team_format": "5v5",
            "team_count": 2,
            "team_size": 5,
            "team_sizes": [5, 5],
            "team_total": 10,
        },
        "game_modes": ("Multiplayer",),
    },
    # Dale & Dawson is a competitive social-deduction game;
    # its largest office supports 21 players.
    "dale and dawson stationery supplies": {
        "remove_coop": True,
        "support": {
            "online_multiplayer": True,
            "online_max": 21,
        },
        "game_modes": ("Multiplayer",),
    },
    # Golf It! is officially listed as Online PvP.
    "golf it": {
        "remove_coop": True,
        "support": {
            "online_max": 30,
        },
        "game_modes": ("Multiplayer",),
    },
    # Master Duel supports official 5-vs-5 Team Battles.
    "yu gi oh master duel": {
        "remove_coop": True,
        "support": {
            "online_max": 10,
            "team_format": "5v5",
            "team_count": 2,
            "team_size": 5,
            "team_sizes": [5, 5],
            "team_total": 10,
        },
        "game_modes": ("Multiplayer",),
    },
    # Mage Arena is officially described as up to 4v4.
    "mage arena": {
        "support": {
            "online_coop": True,
            "online_coop_max": 4,
            "online_multiplayer": True,
            "online_max": 8,
            "team_format": "4v4",
            "team_count": 2,
            "team_size": 4,
            "team_sizes": [4, 4],
            "team_total": 8,
        },
        "game_modes": (
            "Multiplayer",
            "Co-operative",
        ),
    },
    # Meowgic's adventure supports four-player co-op while
    # its competitive arena is two teams of three.
    "meowgic": {
        "support": {
            "online_coop": True,
            "online_coop_max": 4,
            "online_multiplayer": True,
            "online_max": 6,
            "team_format": "3v3",
            "team_count": 2,
            "team_size": 3,
            "team_sizes": [3, 3],
            "team_total": 6,
        },
        "game_modes": (
            "Multiplayer",
            "Co-operative",
        ),
    },
    # Slackers has a four-player competitive online mode.
    "slackers carts of glory": {
        "remove_coop": True,
        "support": {
            "online_multiplayer": True,
            "online_max": 4,
        },
        "game_modes": (
            "Single player",
            "Multiplayer",
        ),
    },
    # Rust is an online MMO whose server limit is variable.
    "rust": {
        "support": {
            "online_multiplayer": True,
            "mmo": True,
        },
        "game_modes": (
            "Multiplayer",
            "Massively Multiplayer Online (MMO)",
        ),
    },
    # s&box contains many user-created games, so there is no
    # single truthful player cap for every server or mode.
    "s and box": {
        "support": {
            "online_multiplayer": True,
            "variable_capacity": True,
        },
        "game_modes": ("Multiplayer",),
    },
}

_player_cache: OrderedDict[
    str,
    tuple[float, dict | None],
] = OrderedDict()
_cache_loaded = False
_cache_lock = asyncio.Lock()

_access_token: str | None = None
_access_token_expires_at = 0.0
_token_lock = asyncio.Lock()

_external_source_ids: dict[str, int] | None = None
_external_source_lock = asyncio.Lock()

_request_times: deque[float] = deque()
_rate_lock = asyncio.Lock()


class IGDBRequestError(RuntimeError):
    pass


def igdb_is_configured() -> bool:
    return bool(
        IGDB_CLIENT_ID
        and IGDB_CLIENT_SECRET
    )


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
        r"[\u00ae\u2122\u00a9]",
        "",
        cleaned,
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


def _clean_external_id(
    value,
) -> str | None:
    cleaned = str(
        value or ""
    ).strip()

    if not cleaned:
        return None

    return cleaned


def _cache_key(
    game_info: dict,
) -> str | None:
    store = str(
        game_info.get("store") or ""
    ).strip().casefold()
    external_id = _clean_external_id(
        game_info.get("external_id")
    )

    if store and external_id:
        return (
            f"external:{store}:"
            f"{external_id.casefold()}"
        )

    title = _normalise_title(
        game_info.get("name")
    )

    if not title:
        return None

    return f"title:{store}:{title}"


def _trim_cache() -> None:
    while len(_player_cache) > IGDB_CACHE_LIMIT:
        _player_cache.popitem(
            last=False
        )


def _read_cache_file() -> OrderedDict:
    try:
        payload = json.loads(
            IGDB_CACHE_PATH.read_text(
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
            "Could not read the IGDB player cache: %s: %s",
            type(error).__name__,
            error,
        )
        return OrderedDict()

    entries = (
        payload.get("entries")
        if isinstance(payload, dict)
        else None
    )

    if (
        not isinstance(payload, dict)
        or payload.get("version")
        != IGDB_CACHE_VERSION
        or not isinstance(entries, dict)
    ):
        return OrderedDict()

    now = time.time()
    loaded = []

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

        if expires_at <= now:
            continue

        value = raw_entry.get("value")

        if (
            value is not None
            and not isinstance(value, dict)
        ):
            continue

        loaded.append(
            (
                expires_at,
                cache_key,
                value,
            )
        )

    loaded.sort()
    return OrderedDict(
        (
            cache_key,
            (expires_at, value),
        )
        for expires_at, cache_key, value in loaded[
            -IGDB_CACHE_LIMIT:
        ]
    )


def _write_cache_file(
    payload: dict,
) -> None:
    IGDB_CACHE_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    temporary_path = IGDB_CACHE_PATH.with_suffix(
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
        IGDB_CACHE_PATH
    )


async def _ensure_cache_loaded() -> None:
    global _cache_loaded

    if _cache_loaded:
        return

    async with _cache_lock:
        if _cache_loaded:
            return

        loaded = await asyncio.to_thread(
            _read_cache_file
        )
        _player_cache.update(loaded)
        _trim_cache()
        _cache_loaded = True


async def _persist_cache() -> None:
    async with _cache_lock:
        payload = {
            "version": IGDB_CACHE_VERSION,
            "entries": {
                cache_key: {
                    "expires_at": expires_at,
                    "value": value,
                }
                for cache_key, (
                    expires_at,
                    value,
                ) in _player_cache.items()
            },
        }

    try:
        await asyncio.to_thread(
            _write_cache_file,
            payload,
        )

    except OSError as error:
        LOGGER.warning(
            "Could not save the IGDB player cache: %s: %s",
            type(error).__name__,
            error,
        )


def _cached_value(
    cache_key: str,
) -> tuple[bool, dict | None]:
    cached = _player_cache.get(
        cache_key
    )

    if cached is None:
        return False, None

    expires_at, value = cached

    if expires_at <= time.time():
        _player_cache.pop(
            cache_key,
            None,
        )
        return False, None

    _player_cache.move_to_end(
        cache_key
    )
    return (
        True,
        dict(value) if value is not None else None,
    )


def _save_cache_value(
    cache_key: str,
    value: dict | None,
) -> None:
    ttl = (
        IGDB_SUCCESS_CACHE_SECONDS
        if value is not None
        else IGDB_NEGATIVE_CACHE_SECONDS
    )
    _player_cache[cache_key] = (
        time.time() + ttl,
        dict(value) if value is not None else None,
    )
    _player_cache.move_to_end(
        cache_key
    )
    _trim_cache()


async def _wait_for_rate_limit() -> None:
    async with _rate_lock:
        while True:
            now = time.monotonic()

            while (
                _request_times
                and now - _request_times[0] >= 1.0
            ):
                _request_times.popleft()

            if len(_request_times) < IGDB_RATE_LIMIT:
                _request_times.append(now)
                return

            await asyncio.sleep(
                max(
                    0.01,
                    1.0 - (
                        now - _request_times[0]
                    ),
                )
            )


async def _get_access_token(
    session: aiohttp.ClientSession,
    *,
    force_refresh: bool = False,
) -> str:
    global _access_token
    global _access_token_expires_at

    if not igdb_is_configured():
        raise IGDBRequestError(
            "IGDB credentials are not configured."
        )

    now = time.monotonic()

    if (
        not force_refresh
        and _access_token
        and now < _access_token_expires_at
    ):
        return _access_token

    async with _token_lock:
        now = time.monotonic()

        if (
            not force_refresh
            and _access_token
            and now < _access_token_expires_at
        ):
            return _access_token

        try:
            async with retrying_request(
                session,
                "POST",
                TWITCH_TOKEN_URL,
                params={
                    "client_id": IGDB_CLIENT_ID,
                    "client_secret": (
                        IGDB_CLIENT_SECRET
                    ),
                    "grant_type": (
                        "client_credentials"
                    ),
                },
                timeout=IGDB_TIMEOUT,
            ) as response:
                if response.status != 200:
                    raise IGDBRequestError(
                        "Twitch token request returned "
                        f"HTTP {response.status}."
                    )

                payload = await response.json(
                    content_type=None
                )

        except IGDBRequestError:
            raise

        except (
            asyncio.TimeoutError,
            aiohttp.ClientError,
            TypeError,
            ValueError,
        ) as error:
            raise IGDBRequestError(
                "Twitch token request failed: "
                f"{type(error).__name__}: {error}"
            ) from error

        token = str(
            payload.get("access_token") or ""
        ).strip()

        try:
            expires_in = int(
                payload.get("expires_in") or 0
            )

        except (TypeError, ValueError):
            expires_in = 0

        if not token or expires_in <= 0:
            raise IGDBRequestError(
                "Twitch returned an invalid IGDB access token."
            )

        _access_token = token
        _access_token_expires_at = (
            time.monotonic()
            + max(60, expires_in - 60)
        )
        return token


async def _request_json(
    session: aiohttp.ClientSession,
    endpoint: str,
    query: str,
) -> list:
    global _access_token
    global _access_token_expires_at

    for attempt in range(2):
        token = await _get_access_token(
            session,
            force_refresh=(attempt == 1),
        )
        await _wait_for_rate_limit()

        try:
            async with retrying_request(
                session,
                "POST",
                f"{IGDB_API_BASE}/{endpoint}",
                data=query,
                headers={
                    "Accept": "application/json",
                    "Client-ID": str(
                        IGDB_CLIENT_ID
                    ),
                    "Authorization": (
                        f"Bearer {token}"
                    ),
                },
                timeout=IGDB_TIMEOUT,
            ) as response:
                if response.status == 401 and attempt == 0:
                    _access_token = None
                    _access_token_expires_at = 0.0
                    continue

                if response.status != 200:
                    raise IGDBRequestError(
                        "IGDB request returned "
                        f"HTTP {response.status} for "
                        f"/{endpoint}."
                    )

                payload = await response.json(
                    content_type=None
                )

        except IGDBRequestError:
            raise

        except (
            asyncio.TimeoutError,
            aiohttp.ClientError,
            TypeError,
            ValueError,
        ) as error:
            raise IGDBRequestError(
                f"IGDB /{endpoint} request failed: "
                f"{type(error).__name__}: {error}"
            ) from error

        if not isinstance(payload, list):
            raise IGDBRequestError(
                f"IGDB /{endpoint} returned invalid data."
            )

        return payload

    raise IGDBRequestError(
        f"IGDB /{endpoint} authentication failed."
    )


async def _get_external_source_ids(
    session: aiohttp.ClientSession,
) -> dict[str, int]:
    global _external_source_ids

    if _external_source_ids is not None:
        return dict(_external_source_ids)

    async with _external_source_lock:
        if _external_source_ids is not None:
            return dict(_external_source_ids)

        payload = await _request_json(
            session,
            "external_game_sources",
            "fields id,name; limit 500;",
        )
        source_ids = {}

        for source in payload:
            if not isinstance(source, dict):
                continue

            try:
                source_id = int(source.get("id"))

            except (TypeError, ValueError):
                continue

            name = _normalise_title(
                source.get("name")
            )

            if name == "steam":
                source_ids["Steam"] = source_id

            elif (
                "epic" in name
                and "game" in name
                and "store" in name
            ):
                source_ids[
                    "Epic Games Store"
                ] = source_id

        _external_source_ids = source_ids
        return dict(source_ids)


def _quote_query_string(
    value,
) -> str:
    cleaned = str(
        value or ""
    ).replace(
        "\\",
        "\\\\",
    ).replace(
        '"',
        '\\"',
    )
    return f'"{cleaned}"'


def _value_filter(
    values: list[str],
) -> str:
    quoted = [
        _quote_query_string(value)
        for value in values
    ]

    if len(quoted) == 1:
        return quoted[0]

    return "(" + ",".join(quoted) + ")"


def _chunks(
    values: list,
    size: int,
):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _platform_id(
    value,
) -> int | None:
    if isinstance(value, dict):
        value = value.get("id")

    try:
        return int(value)

    except (TypeError, ValueError):
        return None


def _is_pc_game(
    game: dict,
) -> bool:
    platforms = game.get("platforms")

    if not isinstance(platforms, list):
        return False

    return IGDB_PC_PLATFORM_ID in {
        _platform_id(platform)
        for platform in platforms
    }


def _valid_player_count(
    value,
) -> int | None:
    try:
        count = int(value)

    except (TypeError, ValueError):
        return None

    if 2 <= count <= 100:
        return count

    return None


def _metadata_complete_with_cached_igdb(
    game_info: dict,
    cached_value: dict | None,
) -> bool:
    """Return whether live IGDB data can be safely skipped."""

    cached_value = (
        cached_value
        if isinstance(cached_value, dict)
        else {}
    )
    steam_support = game_info.get(
        "multiplayer_support"
    )

    if not isinstance(steam_support, dict):
        steam_support = {}

    cached_support = cached_value.get(
        "multiplayer_support"
    )

    if not isinstance(cached_support, dict):
        cached_support = {}

    support = {
        **cached_support,
        **steam_support,
    }
    genres = (
        game_info.get("genres")
        or cached_value.get("genres")
        or []
    )
    game_modes = (
        game_info.get("game_modes")
        or cached_value.get("game_modes")
        or []
    )
    max_players = (
        game_info.get("max_players")
        if game_info.get("max_players") is not None
        else cached_value.get("max_players")
    )

    try:
        player_count = int(max_players)

    except (TypeError, ValueError):
        player_count = None

    normalised_modes = {
        str(mode or "").strip().casefold()
        for mode in game_modes
        if str(mode or "").strip()
    }
    known_single_player = (
        player_count == 1
        or (
            "single player" in normalised_modes
            and not normalised_modes.intersection(
                {
                    "multiplayer",
                    "co-operative",
                    "massively multiplayer online (mmo)",
                }
            )
        )
    )
    has_multiplayer_support = any(
        bool(support.get(field_name))
        for field_name in (
            "online_coop",
            "online_coop_max",
            "online_multiplayer",
            "online_max",
            "offline_coop",
            "offline_coop_max",
            "offline_multiplayer",
            "offline_max",
            "lan_coop",
            "split_screen",
            "split_screen_online",
            "mmo",
            "variable_capacity",
        )
    )
    player_metadata_complete = (
        known_single_player
        or (
            has_multiplayer_support
            and (
                player_count is not None
                or support.get("mmo")
                or support.get("variable_capacity")
                or support.get("capacity_tba")
            )
        )
    )

    return bool(
        genres
        and game_modes
        and player_metadata_complete
    )


def _relation_names(
    values,
    *,
    fallback_names: dict[int, str] | None = None,
) -> list[str]:
    if not isinstance(values, list):
        return []

    names = []

    for value in values:
        name = ""

        if isinstance(value, dict):
            name = str(
                value.get("name") or ""
            ).strip()

        elif fallback_names:
            relation_id = _platform_id(value)

            if relation_id is not None:
                name = fallback_names.get(
                    relation_id,
                    "",
                )

        if name and name.casefold() not in {
            existing.casefold()
            for existing in names
        }:
            names.append(name)

    return names


def _preferred_genres(
    genres,
    themes,
) -> list[str]:
    genre_names = _relation_names(genres)
    theme_names = _relation_names(themes)
    preferred = [
        name
        for name in genre_names
        if name.casefold()
        not in LOW_PRIORITY_GENRES
    ]
    promoted = [
        name
        for name in theme_names
        if name.casefold()
        in PROMOTED_THEME_GENRES
    ]

    for name in promoted:
        if name.casefold() not in {
            existing.casefold()
            for existing in preferred
        }:
            preferred.append(name)

    if not preferred:
        preferred = genre_names

    return preferred[:MAX_DISPLAY_GENRES]


def _multiplayer_support_from_modes(
    modes: list[dict],
) -> dict:
    support = {}

    for source_name, saved_name in (
        MULTIPLAYER_BOOLEAN_FIELDS
    ):
        if any(
            bool(mode.get(source_name))
            for mode in modes
        ):
            support[saved_name] = True

    for source_name, saved_name in (
        MULTIPLAYER_COUNT_FIELDS
    ):
        counts = [
            count
            for count in (
                _valid_player_count(
                    mode.get(source_name)
                )
                for mode in modes
            )
            if count is not None
        ]

        if counts:
            support[saved_name] = max(counts)

    if support:
        support["platform"] = "PC"

    return support


def _apply_verified_multiplayer_classification(
    game_name: str | None,
    support: dict,
    game_modes: list[str],
) -> tuple[dict, list[str]]:
    override = VERIFIED_MULTIPLAYER_OVERRIDES.get(
        _normalise_title(game_name)
    )

    if override is None:
        return support, game_modes

    corrected_support = dict(support)

    if override.get("remove_coop"):
        for coop_key, multiplayer_key in (
            ("online_coop_max", "online_max"),
            ("offline_coop_max", "offline_max"),
        ):
            coop_count = _valid_player_count(
                corrected_support.pop(coop_key, None)
            )
            multiplayer_count = _valid_player_count(
                corrected_support.get(multiplayer_key)
            )

            if coop_count is not None:
                corrected_support[multiplayer_key] = max(
                    coop_count,
                    multiplayer_count or 0,
                )

        for coop_flag in (
            "campaign_coop",
            "lan_coop",
            "offline_coop",
            "online_coop",
        ):
            corrected_support.pop(coop_flag, None)

    for key, value in override.get(
        "support",
        {},
    ).items():
        corrected_support[key] = value

    corrected_support["platform"] = "PC"
    corrected_modes = list(game_modes)

    if override.get("remove_coop"):
        corrected_modes = [
            mode
            for mode in corrected_modes
            if str(mode).strip().casefold()
            != "co-operative"
        ]

    for required_mode in override.get(
        "game_modes",
        (),
    ):
        if not any(
            str(mode).strip().casefold()
            == required_mode.casefold()
            for mode in corrected_modes
        ):
            corrected_modes.append(required_mode)

    return corrected_support, corrected_modes


def _player_info_from_game(
    game: dict,
    *,
    match_method: str,
) -> dict | None:
    game_id = _platform_id(
        game.get("id")
    )

    if game_id is None:
        return None

    modes = game.get("multiplayer_modes")

    if not isinstance(modes, list):
        modes = []

    usable_modes = [
        mode
        for mode in modes
        if isinstance(mode, dict)
    ]
    pc_modes = [
        mode
        for mode in usable_modes
        if _platform_id(
            mode.get("platform")
        ) == IGDB_PC_PLATFORM_ID
    ]

    if pc_modes:
        usable_modes = pc_modes

    game_modes = _relation_names(
        game.get("game_modes"),
        fallback_names=GAME_MODE_NAMES,
    )
    multiplayer_support = (
        _multiplayer_support_from_modes(
            usable_modes
        )
    )
    multiplayer_support, game_modes = (
        _apply_verified_multiplayer_classification(
            game.get("name"),
            multiplayer_support,
            game_modes,
        )
    )
    result = {
        "igdb_game_id": game_id,
        "igdb_name": str(
            game.get("name") or ""
        ).strip(),
        "match_method": match_method,
        "multiplayer_support": multiplayer_support,
        "genres": _preferred_genres(
            game.get("genres"),
            game.get("themes"),
        ),
        "themes": _relation_names(
            game.get("themes")
        ),
        "game_modes": game_modes,
    }

    priorities = (
        ("online_coop_max", "online co-op"),
        ("online_max", "online multiplayer"),
        ("offline_coop_max", "local co-op"),
        ("offline_max", "local multiplayer"),
    )

    for field_name, mode_name in priorities:
        count = _valid_player_count(
            multiplayer_support.get(field_name)
        )

        if count is not None:
            result["max_players"] = count
            result["player_mode"] = mode_name
            return result

    if (
        len(game_modes) == 1
        and game_modes[0].casefold()
        == "single player"
    ):
        result["max_players"] = 1
        result["player_mode"] = "single-player"

    return result


def _select_direct_game(
    request: dict,
    games: list[dict],
) -> dict | None:
    if not games:
        return None

    expected_title = _normalise_title(
        request.get("name")
    )
    exact = [
        game
        for game in games
        if _normalise_title(
            game.get("name")
        ) == expected_title
    ]
    choices = exact or games
    choices.sort(
        key=lambda game: (
            not _is_pc_game(game),
            _platform_id(game.get("id")) or 0,
        )
    )
    return choices[0]


def _select_title_game(
    request: dict,
    games: list[dict],
) -> dict | None:
    expected_title = _normalise_title(
        request.get("name")
    )

    if not expected_title:
        return None

    exact = [
        game
        for game in games
        if (
            isinstance(game, dict)
            and _normalise_title(
                game.get("name")
            ) == expected_title
        )
    ]

    if not exact:
        return None

    exact.sort(
        key=lambda game: (
            not _is_pc_game(game),
            _platform_id(game.get("id")) or 0,
        )
    )
    return exact[0]


async def _fetch_games_by_ids(
    session: aiohttp.ClientSession,
    game_ids: set[int],
) -> dict[int, dict]:
    games_by_id = {}

    for game_id_chunk in _chunks(
        sorted(game_ids),
        500,
    ):
        id_filter = "(" + ",".join(
            str(game_id)
            for game_id in game_id_chunk
        ) + ")"
        payload = await _request_json(
            session,
            "games",
            f"fields {PLAYER_FIELDS}; "
            f"where id = {id_filter}; limit 500;",
        )

        for game in payload:
            if not isinstance(game, dict):
                continue

            try:
                game_id = int(game.get("id"))

            except (TypeError, ValueError):
                continue

            games_by_id[game_id] = game

    return games_by_id


async def _fetch_title_matches(
    session: aiohttp.ClientSession,
    requests: list[dict],
) -> dict[str, list[dict]]:
    matches = {}

    for request_chunk in _chunks(requests, 10):
        query_names = {}
        query_parts = []

        for index, request in enumerate(
            request_chunk
        ):
            query_name = f"players_{index}"
            query_names[query_name] = request[
                "cache_key"
            ]
            query_parts.append(
                f'query games "{query_name}" {{ '
                f"fields {PLAYER_FIELDS}; "
                f"search "
                f"{_quote_query_string(request['name'])}; "
                "where version_parent = null; "
                "limit 10; };"
            )

        payload = await _request_json(
            session,
            "multiquery",
            "\n".join(query_parts),
        )

        for response in payload:
            if not isinstance(response, dict):
                continue

            cache_key = query_names.get(
                str(response.get("name") or "")
            )
            result = response.get("result")

            if (
                cache_key
                and isinstance(result, list)
            ):
                matches[cache_key] = [
                    game
                    for game in result
                    if isinstance(game, dict)
                ]

    return matches


async def get_igdb_player_info_batch(
    session: aiohttp.ClientSession,
    games: list[dict],
    *,
    force_refresh: bool = False,
) -> list[dict | None]:
    results: list[dict | None] = [
        None
        for _ in games
    ]

    if not igdb_is_configured():
        return results

    await _ensure_cache_loaded()

    requests_by_key = {}
    positions_by_key = {}

    for position, game in enumerate(games):
        if not isinstance(game, dict):
            continue

        cache_key = _cache_key(game)
        name = str(
            game.get("name") or ""
        ).strip()

        if not cache_key or not name:
            continue

        positions_by_key.setdefault(
            cache_key,
            [],
        ).append(position)
        requests_by_key.setdefault(
            cache_key,
            {
                "cache_key": cache_key,
                "game_info": game,
                "name": name,
                "store": str(
                    game.get("store") or ""
                ).strip(),
                "external_id": (
                    _clean_external_id(
                        game.get("external_id")
                    )
                ),
            },
        )

    unresolved = []

    for cache_key, request in requests_by_key.items():
        if not force_refresh:
            found, value = _cached_value(
                cache_key
            )

            if (
                found
                and _metadata_complete_with_cached_igdb(
                    request["game_info"],
                    value,
                )
            ):
                for position in positions_by_key[
                    cache_key
                ]:
                    results[position] = (
                        dict(value)
                        if value is not None
                        else None
                    )
                continue

        unresolved.append(request)

    if not unresolved:
        return results

    resolved: dict[str, dict] = {}

    try:
        source_ids = await _get_external_source_ids(
            session
        )
        external_groups = {}

        for request in unresolved:
            source_id = source_ids.get(
                request["store"]
            )

            if (
                source_id is not None
                and request["external_id"]
            ):
                external_groups.setdefault(
                    source_id,
                    [],
                ).append(request)

        direct_game_ids = {}

        for source_id, source_requests in (
            external_groups.items()
        ):
            for request_chunk in _chunks(
                source_requests,
                400,
            ):
                external_ids = list(dict.fromkeys(
                    request["external_id"]
                    for request in request_chunk
                ))
                payload = await _request_json(
                    session,
                    "external_games",
                    "fields game,uid,name,platform; "
                    "where external_game_source = "
                    f"{source_id} & uid = "
                    f"{_value_filter(external_ids)}; "
                    "limit 500;",
                )
                keys_by_external_id = {}

                for request in request_chunk:
                    keys_by_external_id.setdefault(
                        request["external_id"].casefold(),
                        [],
                    ).append(request["cache_key"])

                for external_game in payload:
                    if not isinstance(
                        external_game,
                        dict,
                    ):
                        continue

                    external_id = str(
                        external_game.get("uid") or ""
                    ).strip().casefold()

                    try:
                        game_id = int(
                            external_game.get("game")
                        )

                    except (TypeError, ValueError):
                        continue

                    for cache_key in keys_by_external_id.get(
                        external_id,
                        [],
                    ):
                        direct_game_ids.setdefault(
                            cache_key,
                            set(),
                        ).add(game_id)

        all_direct_ids = {
            game_id
            for game_ids in direct_game_ids.values()
            for game_id in game_ids
        }
        direct_games = await _fetch_games_by_ids(
            session,
            all_direct_ids,
        ) if all_direct_ids else {}

        unresolved_by_key = {
            request["cache_key"]: request
            for request in unresolved
        }

        for cache_key, game_ids in (
            direct_game_ids.items()
        ):
            request = unresolved_by_key[cache_key]
            candidates = [
                direct_games[game_id]
                for game_id in game_ids
                if game_id in direct_games
            ]
            selected = _select_direct_game(
                request,
                candidates,
            )

            if selected:
                player_info = _player_info_from_game(
                    selected,
                    match_method=(
                        f"{request['store']} ID"
                    ),
                )

                if player_info:
                    resolved[cache_key] = player_info

        title_requests = [
            request
            for request in unresolved
            if request["cache_key"] not in resolved
        ]
        title_matches = await _fetch_title_matches(
            session,
            title_requests,
        )

        for request in title_requests:
            cache_key = request["cache_key"]
            selected = _select_title_game(
                request,
                title_matches.get(cache_key, []),
            )

            if not selected:
                continue

            player_info = _player_info_from_game(
                selected,
                match_method="exact title",
            )

            if player_info:
                resolved[cache_key] = player_info

    except IGDBRequestError as error:
        LOGGER.warning(
            "IGDB player metadata lookup failed: %s",
            error,
        )
        return results

    for request in unresolved:
        cache_key = request["cache_key"]
        value = resolved.get(cache_key)
        _save_cache_value(
            cache_key,
            value,
        )

        for position in positions_by_key[cache_key]:
            results[position] = (
                dict(value)
                if value is not None
                else None
            )

    await _persist_cache()
    return results


async def enrich_missing_player_metadata(
    session: aiohttp.ClientSession,
    game_infos,
    *,
    force_refresh: bool = False,
) -> int:
    candidates = [
        game_info
        for game_info in game_infos
        if (
            isinstance(game_info, dict)
            and str(
                game_info.get("name") or ""
            ).strip()
        )
    ]

    if not candidates:
        return 0

    player_results = await get_igdb_player_info_batch(
        session,
        candidates,
        force_refresh=force_refresh,
    )
    enriched = 0

    for game_info, player_info in zip(
        candidates,
        player_results,
    ):
        if not player_info:
            continue

        game_info["igdb_id"] = (
            player_info.get("igdb_game_id")
        )
        igdb_support = dict(
            player_info.get("multiplayer_support")
            or {}
        )
        existing_support = game_info.get(
            "multiplayer_support"
        )

        if not isinstance(existing_support, dict):
            existing_support = {}

        # Current official Steam wording can include team
        # structures that IGDB does not model, so retain those
        # fields while filling the remaining support from IGDB.
        game_info["multiplayer_support"] = {
            **igdb_support,
            **existing_support,
        }
        for field_name in (
            "genres",
            "themes",
            "game_modes",
        ):
            igdb_values = list(
                player_info.get(field_name) or []
            )

            # Steam can still provide useful genres and mode
            # categories when a sparse IGDB record omits them.
            if igdb_values or not game_info.get(field_name):
                game_info[field_name] = igdb_values

        if (
            game_info.get("max_players") is None
            and player_info.get("max_players")
            is not None
        ):
            game_info["max_players"] = (
                player_info["max_players"]
            )
            game_info["max_players_source"] = "IGDB"
            game_info["player_mode"] = (
                player_info.get("player_mode")
            )
            enriched += 1

    return enriched


def clear_igdb_runtime_cache() -> None:
    global _cache_loaded
    global _access_token
    global _access_token_expires_at
    global _external_source_ids

    _player_cache.clear()
    _cache_loaded = False
    _access_token = None
    _access_token_expires_at = 0.0
    _external_source_ids = None
    _request_times.clear()
