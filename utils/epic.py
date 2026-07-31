import asyncio
import html
import re
from html.parser import HTMLParser
from urllib.parse import unquote, urlparse

import aiohttp

from settings import (
    EPIC_STORE_LOCALE,
    STORE_ACCEPT_LANGUAGE,
)
from utils.http_retry import retrying_request

from utils.steamgriddb import get_steamgriddb_artwork


EPIC_STORE_HOST = "store.epicgames.com"

VERIFIED_EPIC_GAME_METADATA = {
    # Epic's official Battle Royale listing supports 100
    # players, with squad modes of up to four per team.
    "fortnite": {
        "max_players": 100,
        "max_players_source": "Epic",
        "multiplayer_support": {
            "online_multiplayer": True,
            "online_max": 100,
            "team_format": "up to 4 per team",
            "team_size": 4,
            "platform": "PC",
        },
        "genres": [
            "Shooter",
            "Battle Royale",
            "Action",
        ],
        "themes": [],
        "game_modes": [
            "Multiplayer",
            "Battle Royale",
        ],
    },
}


def _apply_verified_epic_metadata(
    product_key: str,
    game_info: dict,
) -> dict:
    override = VERIFIED_EPIC_GAME_METADATA.get(
        str(product_key or "").casefold()
    )

    if not override:
        return game_info

    enriched = dict(game_info)

    for key, value in override.items():
        if isinstance(value, dict):
            enriched[key] = dict(value)
        elif isinstance(value, list):
            enriched[key] = list(value)
        else:
            enriched[key] = value

    return enriched


class EpicOpenGraphParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = None
        self.image = None
        self.description = None

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
        ).casefold()

        content = attributes.get("content")

        if not content:
            return

        cleaned = html.unescape(
            str(content)
        ).strip()

        if property_name == "og:title":
            self.title = cleaned

        elif property_name == "og:image":
            self.image = cleaned

        elif property_name in {
            "og:description",
            "description",
        }:
            self.description = cleaned


def clean_epic_url(
    value,
) -> str | None:
    if value is None:
        return None

    cleaned = str(
        value
    ).strip().strip(
        "<>[]()\"'"
    )

    if not cleaned:
        return None

    try:
        parsed = urlparse(
            cleaned
        )

    except ValueError:
        return None

    if parsed.scheme.casefold() not in {
        "http",
        "https",
    }:
        return None

    hostname = (
        parsed.hostname
        or ""
    ).casefold()

    if not hostname.endswith(
        "epicgames.com"
    ):
        return None

    return cleaned


def get_epic_product_key(
    url: str,
) -> str | None:
    cleaned = clean_epic_url(
        url
    )

    if not cleaned:
        return None

    parsed = urlparse(
        cleaned
    )

    parts = [
        unquote(part).strip()
        for part in parsed.path.split("/")
        if part.strip()
    ]

    ignored = {
        "p",
        "product",
        "store",
        "en-us",
        "en-gb",
        "en-au",
        EPIC_STORE_LOCALE.casefold(),
    }

    for part in reversed(
        parts
    ):
        if part.casefold() not in ignored:
            return part.casefold()

    return None


def build_epic_url_variants(
    source_url: str,
) -> list[str]:
    product_key = get_epic_product_key(
        source_url
    )

    if not product_key:
        return []

    variants = [
        (
            "https://store.epicgames.com/"
            f"{EPIC_STORE_LOCALE}/p/{product_key}"
        ),
        (
            "https://store.epicgames.com/"
            f"en-US/p/{product_key}"
        ),
        (
            "https://store.epicgames.com/"
            f"p/{product_key}"
        ),
    ]

    seen = set()
    unique = []

    for url in variants:
        comparison = url.casefold()

        if comparison in seen:
            continue

        seen.add(
            comparison
        )
        unique.append(
            url
        )

    return unique


def clean_epic_title(
    title: str | None,
) -> str | None:
    if not title:
        return None

    cleaned = html.unescape(
        str(title)
    ).strip()

    patterns = (
        r"\s*\|\s*Download and Buy Today.*$",
        r"\s*\|\s*Epic Games Store.*$",
        r"\s*[-–—]\s*Epic Games Store.*$",
    )

    for pattern in patterns:
        cleaned = re.sub(
            pattern,
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()

    invalid = {
        "epic games",
        "epic games store",
        "store",
        "home",
        "sign in",
        "login",
        "unknown game",
    }

    if (
        not cleaned
        or cleaned.casefold() in invalid
    ):
        return None

    return cleaned


def fallback_epic_name(
    source_url: str,
) -> str:
    product_key = get_epic_product_key(
        source_url
    )

    if not product_key:
        return "Unknown Game"

    cleaned = re.sub(
        r"[-_]+",
        " ",
        product_key,
    )

    cleaned = re.sub(
        r"\s+",
        " ",
        cleaned,
    ).strip()

    return (
        cleaned.title()
        if cleaned
        else "Unknown Game"
    )


def _parse_metadata(
    page_html: str | None,
) -> dict:
    if not page_html:
        return {
            "name": None,
            "image_url": None,
            "description": None,
        }

    parser = EpicOpenGraphParser()

    try:
        parser.feed(
            page_html
        )

    except Exception:
        return {
            "name": None,
            "image_url": None,
            "description": None,
        }

    return {
        "name": clean_epic_title(
            parser.title
        ),
        "image_url": parser.image,
        "description": parser.description,
    }


EPIC_REQUEST_TIMEOUT = aiohttp.ClientTimeout(
    total=15,
    connect=7,
)

EPIC_REQUEST_HEADERS = {
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
    "Cache-Control": "no-cache",
}


async def _fetch_epic_page(
    session: aiohttp.ClientSession,
    url: str,
) -> dict:
    """
    Fetch a single Epic Games Store page.

    Reuses the shared bot-wide ``session`` passed in by the
    caller instead of opening a new ``aiohttp.ClientSession``
    per request, so connections/TLS handshakes get pooled
    and reused across calls.
    """

    try:
        async with retrying_request(
            session,
            "GET",
            url,
            headers=EPIC_REQUEST_HEADERS,
            timeout=EPIC_REQUEST_TIMEOUT,
            allow_redirects=True,
            max_redirects=10,
        ) as response:
            page_html = None

            if response.status < 400:
                page_html = await response.text(
                    errors="ignore"
                )

            return {
                "requested_url": url,
                "final_url": str(
                    response.url
                ),
                "http_status": (
                    response.status
                ),
                "page_html": page_html,
                "error": None,
            }

    except asyncio.TimeoutError:
        return {
            "requested_url": url,
            "final_url": None,
            "http_status": None,
            "page_html": None,
            "error": "Request timed out",
        }

    except aiohttp.TooManyRedirects:
        return {
            "requested_url": url,
            "final_url": None,
            "http_status": None,
            "page_html": None,
            "error": "Too many redirects",
        }

    except aiohttp.ClientError as error:
        return {
            "requested_url": url,
            "final_url": None,
            "http_status": None,
            "page_html": None,
            "error": str(error),
        }


async def get_epic_game_info(
    session: aiohttp.ClientSession,
    source_url: str,
) -> dict | None:
    product_key = get_epic_product_key(
        source_url
    )

    if not product_key:
        return None

    attempts = []

    for url in build_epic_url_variants(
        source_url
    ):
        result = await _fetch_epic_page(
            session,
            url,
        )

        attempts.append(
            result
        )

        if (
            result.get(
                "http_status"
            ) is not None
            and 200
            <= result["http_status"]
            < 400
            and result.get(
                "page_html"
            )
        ):
            metadata = _parse_metadata(
                result["page_html"]
            )

            game_name = (
                metadata["name"]
                or fallback_epic_name(
                    source_url
                )
            )

            image_url = metadata[
                "image_url"
            ]

            if not image_url:
                image_url = (
                    await get_steamgriddb_artwork(
                        session,
                        game_name,
                    )
                )

            return _apply_verified_epic_metadata(
                product_key,
                {
                "name": game_name,
                "store": "Epic Games Store",
                "store_link": url,
                "source_link": source_url,
                "image_url": image_url,
                "external_id": product_key,
                "link_status": "live",
                "http_status": result[
                    "http_status"
                ],
                "error": None,
                "sale_info": None,
                "availability_status": "released",
                "coming_soon": False,
                "release_date": None,
                "availability_verified": True,
                "verification_status": (
                    "verified"
                ),
                "verification_note": None,
                },
            )

    blocked_attempt = next(
        (
            attempt
            for attempt in attempts
            if attempt.get(
                "http_status"
            )
            in {
                401,
                403,
                429,
            }
        ),
        None,
    )

    final_attempt = (
        blocked_attempt
        or (
            attempts[-1]
            if attempts
            else {}
        )
    )

    blocked = bool(
        blocked_attempt
    )

    game_name = fallback_epic_name(
        source_url
    )

    fallback_image_url = (
        await get_steamgriddb_artwork(
            session,
            game_name,
        )
    )

    fallback_complete = bool(
        fallback_image_url
    )

    return _apply_verified_epic_metadata(
        product_key,
        {
        "name": game_name,
        "store": "Epic Games Store",
        "store_link": (
            build_epic_url_variants(
                source_url
            )[0]
        ),
        "source_link": source_url,
        "image_url": fallback_image_url,
        "external_id": product_key,
        "link_status": (
            "live"
            if fallback_complete
            else (
                "blocked"
                if blocked
                else "unknown"
            )
        ),
        "http_status": final_attempt.get(
            "http_status"
        ),
        "error": final_attempt.get(
            "error"
        ),
        "sale_info": None,
        "availability_status": "released",
        "coming_soon": False,
        "release_date": None,
        "availability_verified": False,
        "verification_status": (
            "complete"
            if fallback_complete
            else (
                "blocked"
                if blocked
                else "unverified"
            )
        ),
        "verification_note": (
            "Epic metadata completed with "
            "SteamGridDB artwork"
            if fallback_complete
            else (
                "Epic blocked automated verification; "
                "saved metadata or Discord preview retained"
                if blocked
                else (
                    final_attempt.get(
                        "error"
                    )
                    or "Epic could not be verified"
                )
            )
        ),
        },
    )
