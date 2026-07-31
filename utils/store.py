import asyncio
import copy
import html
import json
import re
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import (
    unquote,
    urlparse,
    urlunparse,
)

import aiohttp

from settings import (
    STEAM_COUNTRY_CODE,
    STEAM_LANGUAGE,
    STORE_ACCEPT_LANGUAGE,
)
from utils.epic import get_epic_game_info
from utils.http_retry import retrying_request
from utils.steam_api import fetch_steam_app_data
from utils.time_utils import DISPLAY_TIMEZONE


SUPPORTED_LINK_PATTERN = re.compile(
    r"https?://[^\s<>()]+",
    re.IGNORECASE,
)

STEAM_APP_PATTERN = re.compile(
    r"store\.steampowered\.com/(?:agecheck/)?app/(\d+)",
    re.IGNORECASE,
)

CONFIRMED_DEAD_STATUSES = {
    404,
    410,
}
METADATA_CACHE_TTL_SECONDS = 300
METADATA_FAILURE_CACHE_TTL_SECONDS = 30
METADATA_CACHE_MAX_ENTRIES = 256
STEAM_RELEASE_TIMEZONE = DISPLAY_TIMEZONE
STEAM_STORE_ITEMS_URL = (
    "https://api.steampowered.com/"
    "IStoreBrowseService/GetItems/v1/"
)
STEAM_RELATIVE_UNLOCK_PATTERN = re.compile(
    r"\bthis game plans to unlock in "
    r"approximately\s+(\d+(?:\.\d+)?)\s+"
    r"(minutes?|hours?|days?|weeks?)\b.*$",
    re.IGNORECASE,
)

_metadata_cache: OrderedDict[
    str,
    tuple[float, dict | None],
] = OrderedDict()
_metadata_inflight: dict[
    str,
    asyncio.Task,
] = {}
_metadata_cache_lock = asyncio.Lock()
_metadata_cleanup_handle: asyncio.TimerHandle | None = None


class OpenGraphParser(HTMLParser):
    def __init__(self):
        super().__init__()

        self.title = None
        self.image = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "meta":
            return

        attributes = {
            str(key).lower(): value
            for key, value in attrs
            if key and value
        }

        property_name = (
            attributes.get("property")
            or attributes.get("name")
            or ""
        ).lower()

        content = attributes.get("content")

        if not content:
            return

        cleaned_content = html.unescape(
            str(content)
        ).strip()

        if property_name == "og:title":
            self.title = cleaned_content

        elif property_name == "og:image":
            self.image = cleaned_content


def clean_url(
    value,
) -> str | None:
    if value is None:
        return None

    cleaned_value = str(
        value
    ).strip()

    if not cleaned_value:
        return None

    cleaned_value = cleaned_value.strip(
        "<>[]()\"'"
    )

    if re.search(
        r"[\s\x00-\x1f\x7f]",
        cleaned_value,
    ):
        return None

    try:
        parsed = urlparse(
            cleaned_value
        )

    except ValueError:
        return None

    if parsed.scheme.lower() not in {
        "http",
        "https",
    }:
        return None

    if not parsed.hostname:
        return None

    try:
        parsed.port

    except ValueError:
        return None

    return cleaned_value


def find_supported_store_links(
    message_content: str,
) -> list[str]:
    links = []
    seen = set()

    for raw_url in SUPPORTED_LINK_PATTERN.findall(
        message_content or ""
    ):
        url = raw_url.rstrip(
            ".,!?;:'\")]}>" 
        )

        cleaned_url = clean_url(
            url
        )

        if not cleaned_url:
            continue

        if not detect_store(
            cleaned_url
        ):
            continue

        comparison_value = cleaned_url.casefold()

        if comparison_value in seen:
            continue

        seen.add(
            comparison_value
        )

        links.append(
            cleaned_url
        )

    return links


def detect_store(
    url: str,
) -> str | None:
    cleaned_url = clean_url(
        url
    )

    if not cleaned_url:
        return None

    try:
        parsed = urlparse(
            cleaned_url
        )

    except ValueError:
        return None

    hostname = (
        parsed.hostname
        or ""
    ).lower()

    path = parsed.path.lower()

    if (
        hostname.endswith(
            "steampowered.com"
        )
        and "/app/" in path
    ):
        return "Steam"

    if hostname.endswith(
        "epicgames.com"
    ):
        return "Epic Games Store"

    return None


def get_steam_app_id(
    url: str,
) -> str | None:
    match = STEAM_APP_PATTERN.search(
        url or ""
    )

    if not match:
        return None

    return match.group(1)


def build_steam_store_url(
    app_id: str,
) -> str:
    return (
        "https://store.steampowered.com/"
        f"app/{app_id}/"
    )


def get_steam_image_url(
    url: str,
) -> str | None:
    app_id = get_steam_app_id(
        url
    )

    if not app_id:
        return None

    return (
        "https://cdn.cloudflare.steamstatic.com/"
        f"steam/apps/{app_id}/header.jpg"
    )


def get_epic_product_key(
    url: str,
) -> str | None:
    cleaned_url = clean_url(
        url
    )

    if not cleaned_url:
        return None

    parsed = urlparse(
        cleaned_url
    )

    path_parts = [
        unquote(part).strip()
        for part in parsed.path.split("/")
        if part.strip()
    ]

    if not path_parts:
        return None

    ignored_parts = {
        "p",
        "product",
        "store",
        "en-us",
        "en-gb",
        "en-au",
    }

    for part in reversed(
        path_parts
    ):
        if part.casefold() not in ignored_parts:
            return part.casefold()

    return None


def get_external_id(
    url: str,
    store: str,
) -> str | None:
    if store == "Steam":
        return get_steam_app_id(
            url
        )

    if store == "Epic Games Store":
        return get_epic_product_key(
            url
        )

    return None


def canonicalise_store_url(
    url: str,
) -> str | None:
    cleaned_url = clean_url(
        url
    )

    if not cleaned_url:
        return None

    store = detect_store(
        cleaned_url
    )

    if store == "Steam":
        app_id = get_steam_app_id(
            cleaned_url
        )

        if not app_id:
            return None

        return build_steam_store_url(
            app_id
        )

    if store == "Epic Games Store":
        parsed = urlparse(
            cleaned_url
        )

        return urlunparse(
            (
                "https",
                parsed.netloc.lower(),
                parsed.path,
                "",
                parsed.query,
                "",
            )
        )

    return None


def get_fallback_game_name(
    url: str,
) -> str:
    cleaned_url = clean_url(
        url
    )

    if not cleaned_url:
        return "Unknown Game"

    parsed = urlparse(
        cleaned_url
    )

    path_parts = [
        unquote(part)
        for part in parsed.path.split("/")
        if part
    ]

    if not path_parts:
        return "Unknown Game"

    store = detect_store(
        cleaned_url
    )

    if store == "Steam":
        lowered_parts = [
            part.casefold()
            for part in path_parts
        ]

        try:
            app_index = lowered_parts.index(
                "app"
            )

            slug = path_parts[
                app_index + 2
            ]

        except (
            ValueError,
            IndexError,
        ):
            slug = path_parts[-1]

    else:
        slug = path_parts[-1]

    clean_name = (
        slug
        .replace("_", " ")
        .replace("-", " ")
        .strip()
    )

    if not clean_name:
        return "Unknown Game"

    if (
        store == "Steam"
        and clean_name.isdigit()
    ):
        return "Unknown Game"

    return clean_name.title()


def remove_sale_prefix(
    title: str,
) -> str:
    cleaned_title = str(
        title
    ).strip()

    sale_patterns = (
        r"^save\s+up\s+to\s+\d+(?:\.\d+)?%\s+on\s+",
        r"^save\s+\d+(?:\.\d+)?%\s+on\s+",
    )

    for pattern in sale_patterns:
        cleaned_title = re.sub(
            pattern,
            "",
            cleaned_title,
            flags=re.IGNORECASE,
        ).strip()

    return cleaned_title


def clean_store_title(
    page_title: str | None,
) -> str | None:
    if not page_title:
        return None

    cleaned_title = html.unescape(
        page_title
    ).strip()

    cleaned_title = re.sub(
        r"\s+on Steam$",
        "",
        cleaned_title,
        flags=re.IGNORECASE,
    )

    cleaned_title = re.sub(
        r"\s*\|\s*Download and Buy Today.*$",
        "",
        cleaned_title,
        flags=re.IGNORECASE,
    )

    cleaned_title = re.sub(
        r"\s*\|\s*Epic Games Store.*$",
        "",
        cleaned_title,
        flags=re.IGNORECASE,
    )

    cleaned_title = re.sub(
        r"\s*[-–]\s*Epic Games Store.*$",
        "",
        cleaned_title,
        flags=re.IGNORECASE,
    )

    cleaned_title = remove_sale_prefix(
        cleaned_title
    )

    cleaned_title = cleaned_title.strip()

    if not cleaned_title:
        return None

    invalid_titles = {
        "sign in",
        "signin",
        "login",
        "log in",
        "steam",
        "steam store",
        "steam community",
        "welcome to steam",
        "store",
        "home",
        "age check",
        "age verification",
        "unknown game",
    }

    if cleaned_title.casefold() in invalid_titles:
        return None

    if cleaned_title.isdigit():
        return None

    if len(cleaned_title) < 3:
        return None

    return cleaned_title


def parse_page_metadata(
    page_html: str | None,
) -> tuple[str | None, str | None]:
    if not page_html:
        return None, None

    parser = OpenGraphParser()

    try:
        parser.feed(
            page_html
        )

    except Exception:
        return None, None

    return (
        parser.title,
        clean_url(
            parser.image
        ),
    )


def determine_link_status(
    http_status: int | None,
    request_error: str | None,
) -> str:
    if http_status in CONFIRMED_DEAD_STATUSES:
        return "dead"

    if request_error:
        return "unknown"

    if http_status is None:
        return "unknown"

    if 200 <= http_status < 400:
        return "live"

    return "unknown"


def _html_to_plain_text(
    page_html: str | None,
) -> str:
    if not page_html:
        return ""

    without_scripts = re.sub(
        r"<script\b[^>]*>.*?</script>",
        " ",
        page_html,
        flags=(
            re.IGNORECASE
            | re.DOTALL
        ),
    )

    without_styles = re.sub(
        r"<style\b[^>]*>.*?</style>",
        " ",
        without_scripts,
        flags=(
            re.IGNORECASE
            | re.DOTALL
        ),
    )

    plain_text = re.sub(
        r"<[^>]+>",
        " ",
        without_styles,
    )

    plain_text = html.unescape(
        plain_text
    )

    return re.sub(
        r"\s+",
        " ",
        plain_text,
    ).strip()


def _parse_steam_calendar_date(
    value: str,
) -> datetime | None:
    for date_format in (
        "%d %b, %Y",
        "%d %B, %Y",
        "%d %b %Y",
        "%d %B %Y",
        "%b %d, %Y",
        "%B %d, %Y",
    ):
        try:
            return datetime.strptime(
                value,
                date_format,
            )

        except ValueError:
            continue

    return None


def _normalise_steam_release_date(
    value,
    *,
    now: datetime | None = None,
) -> str | None:
    """Convert Steam's relative unlock text to a local display date."""

    cleaned = re.sub(
        r"\s+",
        " ",
        str(value or ""),
    ).strip()

    if not cleaned:
        return None

    unlock_match = (
        STEAM_RELATIVE_UNLOCK_PATTERN.search(
            cleaned
        )
    )

    if not unlock_match:
        return cleaned

    calendar_text = cleaned[
        :unlock_match.start()
    ].strip(" -:\u2013\u2014")

    if not calendar_text:
        return cleaned

    amount = float(
        unlock_match.group(1)
    )
    unit = unlock_match.group(2).casefold()

    # Hour/minute countdowns are precise enough to resolve
    # Steam's calendar date into the configured display timezone.
    # Longer "approximately N weeks" text is only useful as
    # context, so strip it without replacing Steam's date.
    if unit.startswith("minute"):
        remaining = timedelta(
            minutes=amount
        )

    elif unit.startswith("hour"):
        remaining = timedelta(
            hours=amount
        )

    else:
        return calendar_text

    parsed_calendar = _parse_steam_calendar_date(
        calendar_text
    )

    if parsed_calendar is None:
        return calendar_text

    current_time = (
        now
        if now is not None
        else datetime.now(
            STEAM_RELEASE_TIMEZONE
        )
    )

    if current_time.tzinfo is None:
        current_time = current_time.replace(
            tzinfo=STEAM_RELEASE_TIMEZONE
        )
    else:
        current_time = current_time.astimezone(
            STEAM_RELEASE_TIMEZONE
        )

    local_unlock_date = (
        current_time + remaining
    ).date()
    supplied_date = parsed_calendar.date()

    # Only correct a timezone boundary. If the approximate
    # countdown and supplied date differ wildly, retain the
    # explicit Steam date rather than guessing.
    if abs(
        (local_unlock_date - supplied_date).days
    ) > 1:
        return calendar_text

    return (
        f"{local_unlock_date.day} "
        f"{local_unlock_date.strftime('%b, %Y')}"
    )


def _release_date_from_steam_timestamp(
    value,
) -> str | None:
    """Format a Steam Unix release time in the display timezone."""

    try:
        timestamp = int(value)

    except (TypeError, ValueError):
        return None

    if timestamp <= 0:
        return None

    try:
        release_time = datetime.fromtimestamp(
            timestamp,
            tz=STEAM_RELEASE_TIMEZONE,
        )

    except (OverflowError, OSError, ValueError):
        return None

    return (
        f"{release_time.day} "
        f"{release_time.strftime('%b, %Y')}"
    )


def _release_date_is_in_future(
    release_date: str | None,
) -> bool:
    if not release_date:
        return False

    cleaned_date = re.sub(
        r"\s+",
        " ",
        str(release_date),
    ).strip()

    current_date = datetime.now(
        STEAM_RELEASE_TIMEZONE
    ).replace(tzinfo=None)

    supported_formats = (
        "%B %Y",
        "%b %Y",
        "%d %B %Y",
        "%d %b %Y",
        "%d %B, %Y",
        "%d %b, %Y",
        "%B %d, %Y",
        "%b %d, %Y",
    )

    for date_format in supported_formats:
        try:
            parsed_date = datetime.strptime(
                cleaned_date,
                date_format,
            )

        except ValueError:
            continue

        if date_format in {
            "%B %Y",
            "%b %Y",
        }:
            return (
                parsed_date.year,
                parsed_date.month,
            ) > (
                current_date.year,
                current_date.month,
            )

        return parsed_date.date() > current_date.date()

    year_match = re.fullmatch(
        r"(20\d{2})",
        cleaned_date,
    )

    if year_match:
        return int(
            year_match.group(1)
        ) > current_date.year

    return False


def _release_date_is_released(
    release_date: str | None,
) -> bool:
    if not release_date:
        return False

    cleaned_date = re.sub(
        r"\s+",
        " ",
        str(release_date),
    ).strip()

    current_date = datetime.now(
        STEAM_RELEASE_TIMEZONE
    ).replace(tzinfo=None)

    supported_formats = (
        "%B %Y",
        "%b %Y",
        "%d %B %Y",
        "%d %b %Y",
        "%B %d, %Y",
        "%b %d, %Y",
        "%d %B, %Y",
        "%d %b, %Y",
    )

    for date_format in supported_formats:
        try:
            parsed_date = datetime.strptime(
                cleaned_date,
                date_format,
            )

        except ValueError:
            continue

        if date_format in {
            "%B %Y",
            "%b %Y",
        }:
            return (
                parsed_date.year,
                parsed_date.month,
            ) <= (
                current_date.year,
                current_date.month,
            )

        return (
            parsed_date.date()
            <= current_date.date()
        )

    year_match = re.fullmatch(
        r"(20\d{2})",
        cleaned_date,
    )

    if year_match:
        return (
            int(year_match.group(1))
            < current_date.year
        )

    return False


def _extract_steam_page_release_info(
    page_html: str | None,
) -> dict:
    plain_text = _html_to_plain_text(
        page_html
    )

    normalised_text = plain_text.casefold()

    release_date = None

    release_patterns = (
        r"Planned Release Date:\s*([^|]{2,80}?)(?=\s{2,}|Interested\?|Add to your wishlist|About This Game|$)",
        r"Planned Release Date\s*([^|]{2,80}?)(?=\s{2,}|Interested\?|Add to your wishlist|About This Game|$)",
        r"Release Date:\s*([^|]{2,80}?)(?=\s{2,}|Developer:|Publisher:|About This Game|$)",
    )

    for pattern in release_patterns:
        match = re.search(
            pattern,
            plain_text,
            flags=re.IGNORECASE,
        )

        if not match:
            continue

        candidate = re.sub(
            r"\s+",
            " ",
            match.group(1),
        ).strip(" :-–—")

        if candidate:
            release_date = (
                _normalise_steam_release_date(
                    candidate
                )
            )
            break

    explicit_unreleased = any(
        phrase in normalised_text
        for phrase in (
            "planned release date:",
            "this game is not yet available on steam",
            "add to your wishlist and get notified when it becomes available",
        )
    )

    future_release = (
        _release_date_is_in_future(
            release_date
        )
    )

    released_date = (
        _release_date_is_released(
            release_date
        )
    )

    raw_html = str(
        page_html or ""
    ).casefold()

    released_purchase_markers = (
        "game_area_purchase_game",
        "game_purchase_action",
        "btn_addtocart",
        "add to cart",
        "play game",
        "install game",
        "free to play",
    )

    has_released_purchase_evidence = any(
        marker in raw_html
        for marker in released_purchase_markers
    )

    if (
        future_release
        or explicit_unreleased
    ):
        # A future date or Steam's own explicit
        # unreleased wording is stronger evidence than
        # generic purchase-container HTML, which can
        # also appear on coming-soon pages.
        return {
            "coming_soon": True,
            "release_date": release_date,
            "availability_status": "coming_soon",
            "availability_verified": True,
        }

    if (
        has_released_purchase_evidence
        or released_date
    ):
        return {
            "coming_soon": False,
            "release_date": release_date,
            "availability_status": "released",
            "availability_verified": True,
        }

    return {
        "coming_soon": False,
        "release_date": release_date,
        "availability_status": "unknown",
        "availability_verified": False,
    }

def _format_steam_price(
    cents,
    currency: str | None,
) -> str | None:
    try:
        amount = int(
            cents
        )

    except (
        TypeError,
        ValueError,
    ):
        return None

    currency_code = (
        str(currency or "AUD")
        .strip()
        .upper()
    )

    symbols = {
        "AUD": "A$",
        "USD": "US$",
        "CAD": "CA$",
        "NZD": "NZ$",
        "GBP": "£",
        "EUR": "€",
        "JPY": "¥",
    }

    symbol = symbols.get(
        currency_code,
        f"{currency_code} ",
    )

    if currency_code == "JPY":
        return f"{symbol}{amount:,}"

    return f"{symbol}{amount / 100:,.2f}"


def _extract_steam_price_info(
    app_data: dict,
) -> dict | None:
    if not isinstance(
        app_data,
        dict,
    ):
        return None

    if app_data.get(
        "is_free"
    ) is True:
        return {
            "is_free": True,
            "is_on_sale": False,
            "discount_percent": 0,
            "original_price": None,
            "final_price": "Free",
            "currency": None,
        }

    price_overview = app_data.get(
        "price_overview"
    )

    if not isinstance(
        price_overview,
        dict,
    ):
        return None

    try:
        discount_percent = int(
            price_overview.get(
                "discount_percent",
                0,
            )
            or 0
        )

    except (
        TypeError,
        ValueError,
    ):
        discount_percent = 0

    currency = str(
        price_overview.get(
            "currency",
            "AUD",
        )
    ).strip().upper() or "AUD"

    original_price = _format_steam_price(
        price_overview.get(
            "initial"
        ),
        currency,
    )

    final_price = _format_steam_price(
        price_overview.get(
            "final"
        ),
        currency,
    )

    return {
        "is_free": False,
        "is_on_sale": (
            discount_percent > 0
            and bool(final_price)
        ),
        "discount_percent": max(
            discount_percent,
            0,
        ),
        "original_price": original_price,
        "final_price": final_price,
        "currency": currency,
    }


MULTIPLAYER_CATEGORY_DESCRIPTIONS = {
    "multi-player",
    "co-op",
    "online co-op",
    "local co-op",
    "shared/split screen co-op",
    "shared/split screen",
    "online multi-player",
    "online pvp",
    "local pvp",
    "lan pvp",
    "lan co-op",
    "pvp",
    "mmo",
    "cross-platform multiplayer",
    "shared/split screen pvp",
}

# Some Steam listings confirm player support but omit it from
# fields returned by the Steam app-details API, or phrase it in
# a way that may change later. Keep this deliberately small and
# only add limits confirmed by an official listing or publisher.
VERIFIED_STEAM_PLAYER_LIMITS = {
    # A Game About Chopping Trees is advertised for the
    # player alone or with one friend.
    "4512570": 2,
    # Catto Pew Pew! supports 16-player competitive lobbies.
    "3665520": 16,
    # Dale & Dawson offices support up to 21 players on the
    # largest map (18 on the standard maps).
    "2920570": 21,
    # Dinkum — KRAFTON confirms up to six-player PC co-op.
    "1062520": 6,
    # PAYDAY 3 — the official listing confirms 1-to-4 players.
    "1272080": 4,
    # Superliminal — Group Therapy supports up to 12 players.
    "1049410": 12,
    # Slackers: Carts of Glory supports the player plus
    # three friends in its online racing mode.
    "2354000": 4,
    # JUST A GUY's official demo listing states that its
    # online multiplayer mode is for two players.
    "3916530": 2,
    # Meowgic has four-player co-op and a 3v3 arena.
    "4252290": 6,
}


# Store-category data is generally the best reusable source,
# but a few games need a small correction where different game
# modes have different limits or the game is itself a platform.
VERIFIED_STEAM_MULTIPLAYER_SUPPORT = {
    "2920570": {
        "remove_coop": True,
        "support": {
            "online_multiplayer": True,
            "online_max": 21,
        },
    },
    "4252290": {
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
    },
    "590830": {
        "support": {
            "online_multiplayer": True,
            "variable_capacity": True,
        },
    },
}

LOW_PRIORITY_STEAM_GENRES = {
    "indie",
}

TEAM_VERSUS_PATTERN = re.compile(
    r"\b(?:up\s+to\s+)?"
    r"(\d{1,2})\s*"
    r"(?:v(?:s\.?)?|versus|"
    r"[-\u2013\u2014]?\s*on\s*"
    r"[-\u2013\u2014]?)\s*"
    r"(\d{1,2})\b",
    re.IGNORECASE,
)

TEAM_COUNT_PATTERN = re.compile(
    r"\b(\d{1,2})\s+teams?\s+of\s+"
    r"(?:up\s+to\s+)?"
    r"(\d{1,2})(?:\s+players?)?\b",
    re.IGNORECASE,
)

PER_TEAM_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bup\s+to\s+(\d{1,2})\s+players?\s+per\s+team\b",
        r"\bteams?\s+of\s+up\s+to\s+(\d{1,2})\s+players?\b",
        r"\b(\d{1,2})\s+players?\s+(?:on\s+each|per)\s+team\b",
        r"\b(\d{1,2})[-\s]+player\s+teams?\b",
    )
)

PLAYER_LIMIT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bup\s+to\s+(\d{1,3})\+?\s+(?:online\s+|local\s+)?players?\b",
        r"\bsupports?\s+up\s+to\s+(\d{1,3})\+?\s+players?\b",
        r"\bfor\s+up\s+to\s+(\d{1,3})\+?\s+players?\b",
        r"\b(\d{1,3})\s*[-\u2013\u2014]\s*(\d{1,3})\+?\s+players?\b",
        r"\b(\d{1,3})\s*[-\u2013\u2014]?\s*to\s*[-\u2013\u2014]?\s*(\d{1,3})\+?\s+players?\b",
        r"\b(\d{1,3})\s+(?:to|through)\s+(\d{1,3})\+?\s+players?\b",
        r"\bteams?\s+of\s+(\d{1,3})\s+(?:to|through|[-\u2013\u2014])\s+(\d{1,3})\+?\b",
        r"\b(\d{1,3})\s*[-\u2013\u2014]\s*player\b",
        r"\b(\d{1,3})\+?\s+player\s+(?:online\s+)?(?:co-?op|multiplayer)\b",
        r"\b(?:co-?op|multiplayer)\s+for\s+(\d{1,3})\+?\s+players?\b",
    )
)

PLAYER_FRIEND_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bplay\s+with\s+(?:up\s+to\s+)?(\d{1,2})\s+friends?\b",
        r"\brace\s+with\s+(?:up\s+to\s+)?(\d{1,2})\s+friends?\b",
        r"\bcompete\s+with\s+(?:up\s+to\s+)?(\d{1,2})\s+friends?\b",
    )
)


def _is_single_player_only(
    app_data: dict,
    *,
    descriptions: set[str] | None = None,
) -> bool:
    """
    True only when Steam explicitly tags the game as
    Single-player and lists none of its multiplayer/co-op
    category tags. Absence of category data at all is NOT
    treated as single-player — that's just unknown.
    """

    if not isinstance(
        app_data,
        dict,
    ):
        return False

    if descriptions is None:
        descriptions = (
            _steam_category_descriptions(
                app_data
            )
        )

    if not descriptions:
        return False

    if "single-player" not in descriptions:
        return False

    return not (
        descriptions
        & MULTIPLAYER_CATEGORY_DESCRIPTIONS
    )


def _steam_category_descriptions(
    app_data: dict,
) -> set[str]:
    if not isinstance(app_data, dict):
        return set()

    categories = app_data.get("categories")

    if not isinstance(categories, list):
        return set()

    return {
        str(
            category.get(
                "description",
                "",
            )
        ).strip().casefold()
        for category in categories
        if isinstance(category, dict)
        and str(
            category.get("description") or ""
        ).strip()
    }


def _steam_description_text(
    app_data: dict,
) -> str:
    if not isinstance(app_data, dict):
        return ""

    text_parts = []

    for field_name in (
        "short_description",
        "detailed_description",
        "about_the_game",
    ):
        value = app_data.get(field_name)

        if value:
            text_parts.append(
                _html_to_plain_text(str(value))
            )

    return " ".join(text_parts)


def _extract_team_support(
    app_data: dict,
    *,
    combined_text: str | None = None,
    descriptions: set[str] | None = None,
) -> dict:
    """Extract structured team sizes from official Steam text."""

    if combined_text is None:
        combined_text = _steam_description_text(
            app_data
        )

    if not combined_text:
        return {}

    candidates = []

    for match in TEAM_VERSUS_PATTERN.finditer(
        combined_text
    ):
        first_size = int(match.group(1))
        second_size = int(match.group(2))
        total = first_size + second_size

        if (
            1 <= first_size <= 50
            and 1 <= second_size <= 50
            and 2 <= total <= 100
        ):
            candidates.append(
                {
                    "team_format": (
                        f"{first_size}v{second_size}"
                    ),
                    "team_count": 2,
                    "team_sizes": [
                        first_size,
                        second_size,
                    ],
                    "team_total": total,
                    "team_size": (
                        first_size
                        if first_size == second_size
                        else max(
                            first_size,
                            second_size,
                        )
                    ),
                }
            )

    for match in TEAM_COUNT_PATTERN.finditer(
        combined_text
    ):
        team_count = int(match.group(1))
        team_size = int(match.group(2))
        total = team_count * team_size

        if (
            2 <= team_count <= 20
            and 1 <= team_size <= 50
            and 2 <= total <= 100
        ):
            candidates.append(
                {
                    "team_format": (
                        f"{team_count} teams of "
                        f"{team_size}"
                    ),
                    "team_count": team_count,
                    "team_size": team_size,
                    "team_total": total,
                }
            )

    if candidates:
        support = max(
            candidates,
            key=lambda value: value["team_total"],
        )

    else:
        team_sizes = [
            int(match.group(1))
            for pattern in PER_TEAM_PATTERNS
            for match in pattern.finditer(
                combined_text,
            )
            if 1 <= int(match.group(1)) <= 50
        ]

        if not team_sizes:
            return {}

        team_size = max(team_sizes)
        support = {
            "team_format": (
                f"up to {team_size} per team"
            ),
            "team_size": team_size,
        }

    if descriptions is None:
        descriptions = (
            _steam_category_descriptions(
                app_data
            )
        )
    total = support.get("team_total")
    team_size = support.get("team_size")
    has_online = bool(
        descriptions
        & {
            "online pvp",
            "online co-op",
            "online multi-player",
            "cross-platform multiplayer",
            "mmo",
        }
    )
    has_local = bool(
        descriptions
        & {
            "local pvp",
            "local co-op",
            "shared/split screen pvp",
            "shared/split screen co-op",
            "shared/split screen",
        }
    )

    if has_online:
        support["online_multiplayer"] = True

        if total:
            support["online_max"] = total

        if "online co-op" in descriptions:
            support["online_coop"] = True

            if team_size:
                support["online_coop_max"] = team_size

    if has_local:
        support["offline_multiplayer"] = True

        if total:
            support["offline_max"] = total

        if (
            "local co-op" in descriptions
            or "shared/split screen co-op"
            in descriptions
        ):
            support["offline_coop"] = True

            if team_size:
                support["offline_coop_max"] = team_size

    support["platform"] = "PC"
    return support


def _extract_max_players(
    app_data: dict,
    *,
    combined_text: str | None = None,
    team_support: dict | None = None,
) -> int | None:
    """
    Extract only an explicitly stated multiplayer limit.

    Steam does not expose a dependable dedicated maximum-
    players field, so this deliberately avoids guessing.
    """

    if not isinstance(
        app_data,
        dict,
    ):
        return None

    if combined_text is None:
        combined_text = _steam_description_text(
            app_data
        )

    steam_app_id = str(
        app_data.get("steam_appid") or ""
    ).strip()

    if not combined_text:
        return VERIFIED_STEAM_PLAYER_LIMITS.get(
            steam_app_id
        )

    candidates = []

    for pattern in PLAYER_LIMIT_PATTERNS:
        for match in pattern.finditer(
            combined_text
        ):
            values = [
                int(value)
                for value in match.groups()
                if value is not None
            ]

            if values:
                candidates.append(
                    max(values)
                )

    for pattern in PLAYER_FRIEND_PATTERNS:
        for match in pattern.finditer(
            combined_text
        ):
            friend_count = int(match.group(1))

            if 1 <= friend_count <= 99:
                # "With three friends" means the player plus
                # those three friends: four players total.
                candidates.append(friend_count + 1)

    plausible = [
        value
        for value in candidates
        if 2 <= value <= 100
    ]
    if team_support is None:
        team_support = _extract_team_support(
            app_data,
            combined_text=combined_text,
        )

    team_total = team_support.get("team_total")

    if isinstance(team_total, int):
        plausible.append(team_total)

    verified_limit = (
        VERIFIED_STEAM_PLAYER_LIMITS.get(
            steam_app_id
        )
    )

    if isinstance(verified_limit, int):
        plausible.append(verified_limit)

    if not plausible:
        return None

    return max(
        plausible
    )


def _extract_multiplayer_support(
    app_data: dict,
    max_players: int | None,
    *,
    descriptions: set[str] | None = None,
    team_support: dict | None = None,
) -> dict:
    """Build support from official Steam categories and text."""

    if descriptions is None:
        descriptions = (
            _steam_category_descriptions(
                app_data
            )
        )

    if team_support is None:
        team_support = _extract_team_support(
            app_data,
            descriptions=descriptions,
        )

    support = dict(team_support)
    has_explicit_online = bool(
        descriptions
        & {
            "online pvp",
            "online co-op",
            "online multi-player",
            "cross-platform multiplayer",
            "mmo",
        }
    )
    has_explicit_local = bool(
        descriptions
        & {
            "local pvp",
            "local co-op",
            "lan pvp",
            "lan co-op",
            "shared/split screen pvp",
            "shared/split screen co-op",
            "shared/split screen",
        }
    )
    online_coop = "online co-op" in descriptions
    online_multiplayer = bool(
        descriptions
        & {
            "online pvp",
            "online multi-player",
            "cross-platform multiplayer",
            "mmo",
        }
    )

    # Older Steam listings sometimes use only the legacy
    # generic Co-op/Multi-player categories. Treat those as
    # online only when the listing has no explicit local mode.
    if not has_explicit_online and not has_explicit_local:
        online_coop = "co-op" in descriptions
        online_multiplayer = bool(
            descriptions
            & {
                "multi-player",
                "pvp",
            }
        )

    local_coop = bool(
        descriptions
        & {
            "local co-op",
            "lan co-op",
            "shared/split screen co-op",
        }
    )
    local_multiplayer = bool(
        descriptions
        & {
            "local pvp",
            "lan pvp",
            "shared/split screen pvp",
            "shared/split screen",
        }
    )

    if online_coop:
        support["online_coop"] = True

        if max_players is not None:
            support.setdefault(
                "online_coop_max",
                max_players,
            )

    if online_multiplayer:
        support["online_multiplayer"] = True

        if max_players is not None:
            support.setdefault(
                "online_max",
                max_players,
            )

    if local_coop:
        support["offline_coop"] = True

        if max_players is not None:
            support.setdefault(
                "offline_coop_max",
                max_players,
            )

    if local_multiplayer:
        support["offline_multiplayer"] = True

        if max_players is not None:
            support.setdefault(
                "offline_max",
                max_players,
            )

    if "lan co-op" in descriptions:
        support["lan_coop"] = True

    if descriptions & {
        "shared/split screen",
        "shared/split screen co-op",
        "shared/split screen pvp",
    }:
        support["split_screen"] = True

    if "mmo" in descriptions:
        support["mmo"] = True

    steam_app_id = str(
        app_data.get("steam_appid") or ""
    ).strip()
    override = VERIFIED_STEAM_MULTIPLAYER_SUPPORT.get(
        steam_app_id,
    )

    if override:
        if override.get("remove_coop"):
            for field_name in (
                "campaign_coop",
                "lan_coop",
                "offline_coop",
                "offline_coop_max",
                "online_coop",
                "online_coop_max",
            ):
                support.pop(field_name, None)

        support.update(
            override.get("support", {})
        )

    if support:
        support["platform"] = "PC"

    return support


def _extract_steam_genres(
    app_data: dict,
) -> list[str]:
    raw_genres = app_data.get("genres")

    if not isinstance(raw_genres, list):
        return []

    names = []

    for genre in raw_genres:
        if not isinstance(genre, dict):
            continue

        name = str(
            genre.get("description") or ""
        ).strip()

        if (
            name
            and name.casefold() not in {
                existing.casefold()
                for existing in names
            }
        ):
            names.append(name)

    preferred = [
        name
        for name in names
        if name.casefold()
        not in LOW_PRIORITY_STEAM_GENRES
    ]

    if not preferred:
        preferred = names

    return preferred[:3]


def _extract_steam_game_modes(
    app_data: dict,
    multiplayer_support: dict,
    *,
    descriptions: set[str] | None = None,
) -> list[str]:
    if descriptions is None:
        descriptions = (
            _steam_category_descriptions(
                app_data
            )
        )
    modes = []

    if "single-player" in descriptions:
        modes.append("Single player")

    if (
        any(
            key.startswith("online_")
            or key.startswith("offline_")
            for key in multiplayer_support
        )
        or multiplayer_support.get("mmo")
    ):
        modes.append("Multiplayer")

    if any(
        multiplayer_support.get(key)
        for key in (
            "campaign_coop",
            "lan_coop",
            "offline_coop",
            "online_coop",
        )
    ):
        modes.append("Co-operative")

    if multiplayer_support.get("split_screen"):
        modes.append("Split screen")

    if multiplayer_support.get("mmo"):
        modes.append(
            "Massively Multiplayer Online (MMO)"
        )

    return modes


STORE_PAGE_TIMEOUT = aiohttp.ClientTimeout(
    total=15,
    connect=7,
)

STORE_PAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": STORE_ACCEPT_LANGUAGE,
}


async def fetch_steam_release_info(
    session: aiohttp.ClientSession,
    app_id: str,
) -> dict | None:
    """Fetch Steam's exact release timestamp for timezone-safe dates."""

    cleaned_app_id = str(app_id or "").strip()

    if not cleaned_app_id.isdigit():
        return None

    request_data = json.dumps(
        {
            "ids": [
                {
                    "appid": int(cleaned_app_id),
                }
            ],
            "context": {
                "language": STEAM_LANGUAGE,
                "country_code": STEAM_COUNTRY_CODE,
                "steam_realm": 1,
            },
            "data_request": {
                "include_release": True,
            },
        },
        separators=(",", ":"),
    )

    try:
        async with retrying_request(
            session,
            "GET",
            STEAM_STORE_ITEMS_URL,
            params={
                "input_json": request_data,
            },
            headers={
                "User-Agent": STORE_PAGE_HEADERS[
                    "User-Agent"
                ],
                "Accept": "application/json",
                "Accept-Language": STORE_ACCEPT_LANGUAGE,
            },
            timeout=STORE_PAGE_TIMEOUT,
        ) as response:
            if response.status >= 400:
                return None

            payload = await response.json(
                content_type=None
            )

    except (
        asyncio.TimeoutError,
        aiohttp.ClientError,
        TypeError,
        ValueError,
    ):
        return None

    response_data = (
        payload.get("response")
        if isinstance(payload, dict)
        else None
    )

    store_items = (
        response_data.get("store_items")
        if isinstance(response_data, dict)
        else None
    )

    if not isinstance(store_items, list):
        return None

    for item in store_items:
        if not isinstance(item, dict):
            continue

        try:
            item_app_id = int(
                item.get("appid", item.get("id"))
            )

        except (TypeError, ValueError):
            continue

        if item_app_id != int(cleaned_app_id):
            continue

        release = item.get("release")

        if not isinstance(release, dict):
            return None

        release_date = (
            _release_date_from_steam_timestamp(
                release.get("steam_release_date")
            )
        )

        if not release_date:
            return None

        return {
            "release_date": release_date,
            "coming_soon": bool(
                release.get(
                    "is_coming_soon",
                    item.get("is_coming_soon", False),
                )
            ),
        }

    return None


async def fetch_store_page(
    session: aiohttp.ClientSession,
    url: str,
) -> dict:
    """
    Fetch a store page using the shared bot-wide ``session``
    instead of opening a new ``aiohttp.ClientSession`` per
    call, so TCP/TLS connections get pooled and reused.
    """

    try:
        async with retrying_request(
            session,
            "GET",
            url,
            headers=STORE_PAGE_HEADERS,
            timeout=STORE_PAGE_TIMEOUT,
            allow_redirects=True,
            max_redirects=10,
        ) as response:
            final_url = clean_url(
                str(response.url)
            )

            http_status = response.status
            page_html = None

            if http_status < 400:
                page_html = await response.text(
                    errors="ignore"
                )

            return {
                "final_url": final_url,
                "http_status": http_status,
                "page_html": page_html,
                "error": None,
            }

    except asyncio.TimeoutError:
        return {
            "final_url": None,
            "http_status": None,
            "page_html": None,
            "error": "Request timed out",
        }

    except aiohttp.TooManyRedirects:
        return {
            "final_url": None,
            "http_status": None,
            "page_html": None,
            "error": "Too many redirects",
        }

    except aiohttp.ClientError as error:
        return {
            "final_url": None,
            "http_status": None,
            "page_html": None,
            "error": str(error),
        }


async def fetch_steam_app_details(
    session: aiohttp.ClientSession,
    app_id: str,
    *,
    force_refresh: bool = False,
) -> dict | None:
    app_data = await fetch_steam_app_data(
        session,
        app_id,
        force_refresh=force_refresh,
    )

    if not isinstance(
        app_data,
        dict,
    ):
        return None

    name = clean_store_title(
        str(
            app_data.get(
                "name",
                "",
            )
        ).strip()
    )

    if not name:
        return None

    image_url = clean_url(
        app_data.get(
            "header_image"
        )
    )

    release_data = app_data.get(
        "release_date"
    )

    coming_soon = False
    release_date = None

    if isinstance(
        release_data,
        dict,
    ):
        coming_soon = bool(
            release_data.get(
                "coming_soon",
                False,
            )
        )

        release_date = (
            _normalise_steam_release_date(
                release_data.get(
                    "date",
                    "",
                )
            )
        )

    app_type = str(
        app_data.get(
            "type",
            "",
        )
    ).strip().casefold() or None

    release_date_in_future = (
        _release_date_is_in_future(
            release_date
        )
    )

    released_store_evidence = bool(
        app_data.get(
            "is_free"
        ) is True
        or isinstance(
            app_data.get(
                "price_overview"
            ),
            dict,
        )
    )

    if release_date_in_future:
        coming_soon = True

    elif released_store_evidence:
        # A playable free-game flag or a live price is
        # stronger evidence of release than a stale
        # coming_soon flag.
        coming_soon = False

    price_info = _extract_steam_price_info(
        app_data
    )
    descriptions = _steam_category_descriptions(
        app_data
    )
    combined_text = _steam_description_text(
        app_data
    )
    team_support = _extract_team_support(
        app_data,
        combined_text=combined_text,
        descriptions=descriptions,
    )
    max_players = _extract_max_players(
        app_data,
        combined_text=combined_text,
        team_support=team_support,
    )
    multiplayer_support = (
        _extract_multiplayer_support(
            app_data,
            max_players,
            descriptions=descriptions,
            team_support=team_support,
        )
    )
    genres = _extract_steam_genres(
        app_data
    )
    game_modes = _extract_steam_game_modes(
        app_data,
        multiplayer_support,
        descriptions=descriptions,
    )

    if (
        max_players is None
        and _is_single_player_only(
            app_data,
            descriptions=descriptions,
        )
    ):
        max_players = 1

    released_date_verified = (
        _release_date_is_released(
            release_date
        )
    )

    # A false coming_soon flag by itself is not enough
    # evidence that a game has released. Steam metadata can
    # temporarily return false or incomplete data for titles
    # that are still unreleased. Promotion requires positive
    # released evidence: a live price/free flag or a release
    # date that has actually arrived.
    availability_verified = bool(
        release_date_in_future
        or coming_soon
        or released_store_evidence
        or released_date_verified
    )

    availability_status = (
        "coming_soon"
        if (
            coming_soon
            or release_date_in_future
        )
        else (
            "released"
            if (
                released_store_evidence
                or released_date_verified
            )
            else "unknown"
        )
    )

    if (
        max_players is None
        and availability_status == "coming_soon"
        and multiplayer_support
        and not multiplayer_support.get(
            "variable_capacity"
        )
    ):
        multiplayer_support[
            "capacity_tba"
        ] = True

    return {
        "name": name,
        "image_url": image_url,
        "price_info": price_info,
        "app_type": app_type,
        "max_players": max_players,
        "max_players_source": (
            "Steam"
            if max_players is not None
            else None
        ),
        "multiplayer_support": multiplayer_support,
        "genres": genres,
        "game_modes": game_modes,
        "coming_soon": coming_soon,
        "release_date": release_date,
        "availability_status": (
            availability_status
        ),
        "availability_verified": (
            availability_verified
        ),
    }


async def get_steam_sale_info(
    session: aiohttp.ClientSession,
    store_link: str | None,
    store: str | None = None,
) -> dict | None:
    if store and "steam" not in str(
        store
    ).casefold():
        return None

    app_id = get_steam_app_id(
        store_link or ""
    )

    if not app_id:
        return None

    details = await fetch_steam_app_details(
        session,
        app_id,
    )

    if not details:
        return None

    price_info = details.get(
        "price_info"
    )

    if not isinstance(
        price_info,
        dict,
    ):
        return None

    if not price_info.get(
        "is_on_sale"
    ):
        return None

    return price_info


async def _fetch_game_info_from_url(
    session: aiohttp.ClientSession,
    url: str,
    *,
    force_refresh: bool = False,
) -> dict | None:
    source_link = clean_url(
        url
    )

    if not source_link:
        return None

    store = detect_store(
        source_link
    )

    if not store:
        return None

    if store == "Epic Games Store":
        return await get_epic_game_info(
            session,
            source_link,
        )

    request_url = canonicalise_store_url(
        source_link
    )

    if not request_url:
        return None

    steam_details = None

    steam_app_id = get_steam_app_id(
        request_url
    )

    if steam_app_id:
        # The storefront page and Steam app-details API
        # are independent. Fetching them together keeps
        # the same verification behavior while roughly
        # halving lookup latency on slower connections.
        fetch_result, steam_details = await asyncio.gather(
            fetch_store_page(
                session,
                request_url,
            ),
            fetch_steam_app_details(
                session,
                steam_app_id,
                force_refresh=force_refresh,
            ),
        )

    else:
        fetch_result = await fetch_store_page(
            session,
            request_url,
        )

    http_status = fetch_result[
        "http_status"
    ]

    link_status = determine_link_status(
        http_status=http_status,
        request_error=fetch_result["error"],
    )

    final_url = fetch_result[
        "final_url"
    ]

    store_link = request_url

    if (
        final_url
        and detect_store(final_url) == store
    ):
        store_link = (
            canonicalise_store_url(
                final_url
            )
            or final_url
        )

    page_title, page_image = parse_page_metadata(
        fetch_result["page_html"]
    )

    cleaned_page_title = clean_store_title(
        page_title
    )

    game_name = cleaned_page_title
    image_url = page_image
    sale_info = None
    max_players = None
    multiplayer_support = {}
    genres = None
    game_modes = None
    availability_verified = False

    page_release_info = (
        _extract_steam_page_release_info(
            fetch_result["page_html"]
        )
        if store == "Steam"
        else {
            "coming_soon": False,
            "release_date": None,
            "availability_status": "released",
            "availability_verified": True,
        }
    )

    timestamp_release_info = None

    if (
        store == "Steam"
        and steam_app_id
        and (
            page_release_info.get(
                "coming_soon",
                False,
            )
            or (
                steam_details
                and steam_details.get(
                    "coming_soon",
                    False,
                )
            )
        )
    ):
        timestamp_release_info = (
            await fetch_steam_release_info(
                session,
                steam_app_id,
            )
        )

    coming_soon = bool(
        page_release_info.get(
            "coming_soon",
            False,
        )
    )

    page_release_date = page_release_info.get(
        "release_date"
    )
    timestamp_release_date = (
        timestamp_release_info.get(
            "release_date"
        )
        if timestamp_release_info
        else None
    )
    release_date = (
        timestamp_release_date
        or page_release_date
    )

    availability_status = (
        page_release_info.get(
            "availability_status"
        )
        or (
            "released"
            if store != "Steam"
            else "unknown"
        )
    )

    availability_verified = bool(
        page_release_info.get(
            "availability_verified",
            store != "Steam",
        )
    )

    if store == "Steam":
        if steam_details:
            if not game_name:
                game_name = steam_details[
                    "name"
                ]

            image_url = (
                image_url
                or steam_details[
                    "image_url"
                ]
            )

            price_info = steam_details.get(
                "price_info"
            )

            max_players = steam_details.get(
                "max_players"
            )
            multiplayer_support = dict(
                steam_details.get(
                    "multiplayer_support"
                )
                or {}
            )
            genres = (
                steam_details.get("genres")
                or None
            )
            game_modes = (
                steam_details.get("game_modes")
                or None
            )

            details_coming_soon = bool(
                steam_details.get(
                    "coming_soon",
                    False,
                )
            )

            coming_soon = (
                coming_soon
                or details_coming_soon
            )

            details_status = (
                steam_details.get(
                    "availability_status"
                )
                or "unknown"
            )

            details_verified = bool(
                steam_details.get(
                    "availability_verified",
                    False,
                )
            )

            page_status_verified = bool(
                page_release_info.get(
                    "availability_verified",
                    False,
                )
            )

            page_status = (
                page_release_info.get(
                    "availability_status"
                )
                or "unknown"
            )

            if (
                page_status_verified
                and page_status == "released"
            ):
                # A live Steam page with a purchase/play
                # section is stronger evidence than stale
                # coming-soon metadata.
                availability_status = "released"
                availability_verified = True
                coming_soon = False

            elif (
                page_status_verified
                and page_status == "coming_soon"
            ):
                availability_status = (
                    "coming_soon"
                )
                availability_verified = True
                coming_soon = True

            elif details_verified:
                availability_status = (
                    details_status
                )
                availability_verified = True
                coming_soon = (
                    details_status
                    == "coming_soon"
                )

            elif coming_soon:
                availability_status = (
                    "coming_soon"
                )
                availability_verified = True

            details_release_date = (
                steam_details.get(
                    "release_date"
                )
            )

            # Steam's Store Browse timestamp is the exact
            # unlock moment, so it wins after conversion to
            # the configured timezone. Page and app-details strings remain
            # fallbacks when Steam does not expose a timestamp.
            release_date = (
                timestamp_release_date
                or (
                    page_release_date
                    if (
                        page_status_verified
                        and page_release_date
                    )
                    else (
                        details_release_date
                        or page_release_date
                    )
                )
            )

            if (
                isinstance(price_info, dict)
                and price_info.get(
                    "is_on_sale"
                )
            ):
                sale_info = price_info

        # Keep only artwork confirmed by the Steam
        # page or app-details response. Guessing the CDN
        # header path causes repeated 404 errors for some
        # new or unavailable app IDs.

    if not game_name:
        game_name = get_fallback_game_name(
            store_link
        )

    return {
        "name": game_name,
        "store": store,
        "store_link": store_link,
        "source_link": source_link,
        "image_url": image_url,
        "external_id": get_external_id(
            store_link,
            store,
        ),
        "link_status": link_status,
        "http_status": http_status,
        "error": fetch_result["error"],
        "sale_info": sale_info,
        "max_players": max_players,
        "max_players_source": (
            "Steam"
            if max_players is not None
            else None
        ),
        "multiplayer_support": multiplayer_support,
        "genres": genres,
        "game_modes": game_modes,
        "availability_status": (
            availability_status
        ),
        "coming_soon": coming_soon,
        "release_date": release_date,
        "availability_verified": (
            availability_verified
        ),
    }


def _metadata_cache_key(url: str) -> str | None:
    source_link = clean_url(url)

    if not source_link:
        return None

    return (
        canonicalise_store_url(source_link)
        or source_link
    ).casefold()


async def clear_store_metadata_cache() -> int:
    """Release parsed store metadata retained by short-lived lookups."""

    global _metadata_cleanup_handle

    async with _metadata_cache_lock:
        removed = len(_metadata_cache)
        _metadata_cache.clear()

        if _metadata_cleanup_handle is not None:
            _metadata_cleanup_handle.cancel()
            _metadata_cleanup_handle = None

    return removed


def _schedule_metadata_cleanup_locked() -> None:
    global _metadata_cleanup_handle

    if _metadata_cleanup_handle is not None:
        _metadata_cleanup_handle.cancel()
        _metadata_cleanup_handle = None

    if not _metadata_cache:
        return

    try:
        loop = asyncio.get_running_loop()

    except RuntimeError:
        return

    next_expiry = min(
        expires_at
        for expires_at, _value in _metadata_cache.values()
    )
    delay = max(
        0.1,
        next_expiry - time.monotonic(),
    )
    _metadata_cleanup_handle = loop.call_later(
        delay,
        _start_scheduled_metadata_cleanup,
    )


def _start_scheduled_metadata_cleanup() -> None:
    global _metadata_cleanup_handle

    _metadata_cleanup_handle = None
    task = asyncio.create_task(
        prune_store_metadata_cache()
    )

    def log_failure(completed_task: asyncio.Task) -> None:
        try:
            completed_task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            # Cache pruning is best-effort and must not affect commands.
            pass

    task.add_done_callback(log_failure)


async def prune_store_metadata_cache() -> int:
    now = time.monotonic()

    async with _metadata_cache_lock:
        expired_keys = [
            cache_key
            for cache_key, (
                expires_at,
                _value,
            ) in _metadata_cache.items()
            if expires_at <= now
        ]

        for cache_key in expired_keys:
            _metadata_cache.pop(cache_key, None)

        _schedule_metadata_cleanup_locked()

    return len(expired_keys)


async def _fetch_and_cache_metadata(
    session: aiohttp.ClientSession,
    url: str,
    cache_key: str,
    *,
    force_refresh: bool = False,
) -> dict | None:
    current_task = asyncio.current_task()

    try:
        result = await _fetch_game_info_from_url(
            session,
            url,
            force_refresh=force_refresh,
        )

        async with _metadata_cache_lock:
            ttl = (
                METADATA_CACHE_TTL_SECONDS
                if result is not None
                else METADATA_FAILURE_CACHE_TTL_SECONDS
            )
            _metadata_cache[cache_key] = (
                time.monotonic() + ttl,
                copy.deepcopy(result),
            )
            _metadata_cache.move_to_end(cache_key)

            while (
                len(_metadata_cache)
                > METADATA_CACHE_MAX_ENTRIES
            ):
                _metadata_cache.popitem(last=False)

            _schedule_metadata_cleanup_locked()

        return result

    finally:
        async with _metadata_cache_lock:
            if (
                _metadata_inflight.get(cache_key)
                is current_task
            ):
                _metadata_inflight.pop(cache_key, None)


async def get_game_info_from_url(
    session: aiohttp.ClientSession,
    url: str,
    *,
    force_refresh: bool = False,
) -> dict | None:
    """Fetch store metadata with short cache and in-flight sharing.

    Full audits use ``force_refresh=True``. Concurrent requests for the
    same URL still share one network task, while every caller receives
    its own copy so downstream metadata adjustments cannot taint cache.
    """

    cache_key = _metadata_cache_key(url)

    if cache_key is None:
        return None

    now = time.monotonic()

    async with _metadata_cache_lock:
        cached = _metadata_cache.get(cache_key)

        if cached is not None:
            expires_at, cached_value = cached

            if expires_at <= now:
                _metadata_cache.pop(cache_key, None)
                _schedule_metadata_cleanup_locked()

            elif not force_refresh:
                _metadata_cache.move_to_end(cache_key)
                return copy.deepcopy(cached_value)

        task = _metadata_inflight.get(cache_key)

        if task is None:
            task = asyncio.create_task(
                _fetch_and_cache_metadata(
                    session,
                    url,
                    cache_key,
                    force_refresh=force_refresh,
                )
            )
            _metadata_inflight[cache_key] = task

    result = await asyncio.shield(task)

    return copy.deepcopy(result)
