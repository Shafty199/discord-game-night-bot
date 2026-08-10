import json
import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
DATABASE_PATH = PROJECT_ROOT / "database" / "games.db"
LOCAL_ARTWORK_CACHE_DIRECTORY = (
    DATABASE_PATH.parent / "artwork"
)

load_dotenv(
    PROJECT_ROOT / ".env"
)


def _load_config() -> dict:
    try:
        with CONFIG_PATH.open(
            "r",
            encoding="utf-8",
        ) as file:
            config = json.load(
                file
            )

    except FileNotFoundError:
        return {}

    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"{CONFIG_PATH.name} contains invalid JSON: "
            f"{error}"
        ) from error

    if not isinstance(
        config,
        dict,
    ):
        raise RuntimeError(
            f"{CONFIG_PATH.name} must contain a JSON object."
        )

    return config


def _setting(
    environment_name: str,
    config_name: str,
    default: str = "",
) -> str:
    environment_value = os.getenv(environment_name)

    if environment_value is not None:
        return str(environment_value).strip()

    config_value = CONFIG.get(config_name)

    if config_value is not None:
        return str(config_value).strip()

    return default


def _optional_discord_id(
    value,
    *,
    setting_name: str,
) -> int | None:
    cleaned_value = str(
        value or ""
    ).strip()

    if not cleaned_value:
        return None

    try:
        discord_id = int(
            cleaned_value
        )

    except ValueError as error:
        raise RuntimeError(
            f"{setting_name} must be a Discord ID number."
        ) from error

    if discord_id <= 0:
        raise RuntimeError(
            f"{setting_name} must be a positive Discord ID."
        )

    return discord_id


CONFIG = _load_config()

DISCORD_TOKEN = (
    os.getenv(
        "DISCORD_TOKEN",
        "",
    ).strip()
    or None
)

STEAMGRIDDB_API_KEY = (
    os.getenv(
        "STEAMGRIDDB_API_KEY",
        "",
    ).strip()
    or None
)

IGDB_CLIENT_ID = (
    os.getenv(
        "IGDB_CLIENT_ID",
        "",
    ).strip()
    or None
)

IGDB_CLIENT_SECRET = (
    os.getenv(
        "IGDB_CLIENT_SECRET",
        "",
    ).strip()
    or None
)

OCI_SPIN_NAMESPACE = (
    os.getenv(
        "OCI_SPIN_NAMESPACE",
        "",
    ).strip()
    or None
)

OCI_SPIN_BUCKET = (
    os.getenv(
        "OCI_SPIN_BUCKET",
        "",
    ).strip()
    or None
)

OCI_SPIN_REGION = (
    os.getenv(
        "OCI_SPIN_REGION",
        "",
    ).strip()
    or None
)

SPIN_CACHE_CHANNEL_ID = _optional_discord_id(
    os.getenv(
        "SPIN_CACHE_CHANNEL_ID"
    )
    or CONFIG.get(
        "spin_cache_channel_id"
    ),
    setting_name="SPIN_CACHE_CHANNEL_ID",
)

SESSION_CHANNEL_ID = _optional_discord_id(
    os.getenv(
        "SESSION_CHANNEL_ID"
    )
    or CONFIG.get(
        "session_channel_id"
    ),
    setting_name="SESSION_CHANNEL_ID",
)

SESSION_NOTIFY_ROLE_ID = _optional_discord_id(
    os.getenv(
        "SESSION_NOTIFY_ROLE_ID"
    )
    or CONFIG.get(
        "session_notify_role_id"
    ),
    setting_name="SESSION_NOTIFY_ROLE_ID",
)

LOGGING_CHANNEL_ID = _optional_discord_id(
    os.getenv(
        "LOGGING_CHANNEL_ID"
    )
    or CONFIG.get(
        "logging_channel_id"
    ),
    setting_name="LOGGING_CHANNEL_ID",
)

GAME_NIGHT_CHANNEL_ID = _optional_discord_id(
    os.getenv(
        "GAME_NIGHT_CHANNEL_ID"
    )
    or CONFIG.get(
        "game_night_channel_id"
    )
    or SESSION_CHANNEL_ID,
    setting_name="GAME_NIGHT_CHANNEL_ID",
)

GAME_NIGHT_ROLE_ID = _optional_discord_id(
    os.getenv(
        "GAME_NIGHT_ROLE_ID"
    )
    or CONFIG.get(
        "game_night_role_id"
    )
    or SESSION_NOTIFY_ROLE_ID,
    setting_name="GAME_NIGHT_ROLE_ID",
)

GAME_NIGHT_VOICE_CHANNEL_ID = _optional_discord_id(
    os.getenv(
        "GAME_NIGHT_VOICE_CHANNEL_ID"
    )
    or CONFIG.get(
        "game_night_voice_channel_id"
    ),
    setting_name="GAME_NIGHT_VOICE_CHANNEL_ID",
)

GAME_NIGHT_TIMEZONE = (
    _setting(
        "GAME_NIGHT_TIMEZONE",
        "game_night_timezone",
        "UTC",
    )
    or "UTC"
)

SUGGESTION_THREAD_ID = _optional_discord_id(
    os.getenv(
        "SUGGESTION_THREAD_ID"
    )
    or CONFIG.get(
        "suggestion_thread_id"
    ),
    setting_name="SUGGESTION_THREAD_ID",
)

if SUGGESTION_THREAD_ID is None:
    raise RuntimeError(
        "SUGGESTION_THREAD_ID was not found in .env or "
        "config.json."
    )

STEAM_EMOJI = _setting(
    "STEAM_EMOJI",
    "steam_emoji",
    "🎮",
)

EPIC_EMOJI = _setting(
    "EPIC_EMOJI",
    "epic_emoji",
    "🛍️",
)

# A fixed UTC offset avoids a platform-specific timezone dependency.
# Accepted examples include +10:00, -05:00, UTC and GMT.
DISPLAY_TIMEZONE_OFFSET = _setting(
    "DISPLAY_TIMEZONE_OFFSET",
    "display_timezone_offset",
    "+00:00",
)

STEAM_COUNTRY_CODE = _setting(
    "STEAM_COUNTRY_CODE",
    "steam_country_code",
    "US",
).upper()

if (
    len(STEAM_COUNTRY_CODE) != 2
    or not STEAM_COUNTRY_CODE.isalpha()
):
    raise RuntimeError(
        "STEAM_COUNTRY_CODE must be a two-letter country code."
    )

STEAM_LANGUAGE = _setting(
    "STEAM_LANGUAGE",
    "steam_language",
    "english",
) or "english"

STORE_ACCEPT_LANGUAGE = _setting(
    "STORE_ACCEPT_LANGUAGE",
    "store_accept_language",
    "en-US,en;q=0.9",
) or "en-US,en;q=0.9"

EPIC_STORE_LOCALE = _setting(
    "EPIC_STORE_LOCALE",
    "epic_store_locale",
    "en-US",
) or "en-US"
