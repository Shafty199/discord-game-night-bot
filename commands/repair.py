import asyncio
import contextlib
import html
import json
import logging
import re
import unicodedata
from difflib import SequenceMatcher
from html.parser import HTMLParser
from urllib.parse import urlencode

import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from settings import (
    STEAM_COUNTRY_CODE,
    STEAM_LANGUAGE,
    STORE_ACCEPT_LANGUAGE,
)
from database.database import (
    database_connection,
    save_store_replacement,
)
from utils.artwork_cache import (
    delete_local_game_artwork,
    prepare_local_game_artwork,
)
from utils.igdb import (
    enrich_missing_player_metadata,
)
from utils.http_retry import retrying_request
from utils.steam_api import (
    clear_steam_details_cache,
    fetch_steam_app_data,
)
from utils.time_utils import utc_now_iso


LOGGER = logging.getLogger(__name__)


STEAM_APP_PATTERN = re.compile(
    r"store\.steampowered\.com/(?:agecheck/)?app/(\d+)",
    re.IGNORECASE,
)

REPAIR_REQUEST_TIMEOUT = aiohttp.ClientTimeout(
    total=25,
    connect=8,
)

REPAIR_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36 "
        "GameNightDiscordBot/1.0"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/json;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": STORE_ACCEPT_LANGUAGE,
}

INVALID_GAME_TITLES = {
    "sign in",
    "signin",
    "log in",
    "login",
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

STEAM_SEARCH_SUGGEST_URL = (
    "https://store.steampowered.com/search/suggest"
)

SEARCH_MATCH_THRESHOLD = 0.92
SEARCH_MATCH_MARGIN = 0.08
MAX_SEARCH_CANDIDATES = 8

KNOWN_OBSOLETE_DEMO_REPLACEMENTS = {
    "4018900": "3266950",
    "3911640": "3643170",
    "3957630": "3957560",
}

TITLE_COMPARISON_TRANSLATION = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u2032": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
        "\u2033": '"',
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u00a0": " ",
    }
)


class SteamSearchSuggestionParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.app_ids = []
        self.seen = set()

    def handle_starttag(self, tag, attrs):
        attributes = {
            str(key).lower(): value
            for key, value in attrs
            if key and value is not None
        }

        candidate_values = (
            attributes.get("data-ds-appid"),
            attributes.get("data-appid"),
            attributes.get("data-ds-itemkey"),
            attributes.get("href"),
        )

        for value in candidate_values:
            app_id = extract_app_id_from_value(
                value
            )

            if not app_id:
                continue

            if app_id in self.seen:
                continue

            self.seen.add(
                app_id
            )

            self.app_ids.append(
                app_id
            )


def extract_app_id_from_value(
    value,
) -> str | None:
    if value is None:
        return None

    cleaned_value = str(
        value
    ).strip()

    if not cleaned_value:
        return None

    if cleaned_value.isdigit():
        return cleaned_value

    item_match = re.search(
        r"(?:^|_)app_(\d+)(?:_|$)",
        cleaned_value,
        re.IGNORECASE,
    )

    if item_match:
        return item_match.group(1)

    url_match = STEAM_APP_PATTERN.search(
        cleaned_value
    )

    if url_match:
        return url_match.group(1)

    return None


def extract_steam_app_id(
    store_link: str | None,
) -> str | None:
    if not store_link:
        return None

    match = STEAM_APP_PATTERN.search(
        store_link
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


def normalise_title_for_comparison(
    title: str | None,
) -> str:
    if not title:
        return ""

    cleaned_title = unicodedata.normalize(
        "NFKC",
        html.unescape(
            str(title)
        ),
    )

    cleaned_title = cleaned_title.translate(
        TITLE_COMPARISON_TRANSLATION
    )

    cleaned_title = re.sub(
        r"\s+",
        " ",
        cleaned_title,
    ).strip()

    return cleaned_title.casefold()


def titles_are_equivalent(
    first_title: str | None,
    second_title: str | None,
) -> bool:
    return normalise_title_for_comparison(
        first_title
    ) == normalise_title_for_comparison(
        second_title
    )


def normalise_steam_link_for_comparison(
    store_link: str | None,
) -> str:
    if not store_link:
        return ""

    app_id = extract_steam_app_id(
        str(store_link)
    )

    if app_id:
        return f"steam-app:{app_id}"

    return html.unescape(
        str(store_link)
    ).strip().rstrip("/").casefold()


def steam_links_are_equivalent(
    first_link: str | None,
    second_link: str | None,
) -> bool:
    return normalise_steam_link_for_comparison(
        first_link
    ) == normalise_steam_link_for_comparison(
        second_link
    )


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

    cleaned_title = re.sub(
        r"\s+on Steam$",
        "",
        cleaned_title,
        flags=re.IGNORECASE,
    ).strip()

    return cleaned_title


def has_sale_prefix(
    title: str | None,
) -> bool:
    if not title:
        return False

    cleaned_title = str(
        title
    ).strip()

    return bool(
        re.match(
            (
                r"^save\s+"
                r"(?:up\s+to\s+)?"
                r"\d+(?:\.\d+)?%\s+on\s+"
            ),
            cleaned_title,
            flags=re.IGNORECASE,
        )
    )


def is_invalid_title(
    title: str | None,
) -> bool:
    if not title:
        return True

    cleaned_title = str(
        title
    ).strip()

    if not cleaned_title:
        return True

    normalised_title = cleaned_title.casefold()

    if normalised_title in INVALID_GAME_TITLES:
        return True

    invalid_prefixes = (
        "sign in",
        "login",
        "log in",
        "welcome to steam",
    )

    if normalised_title.startswith(
        invalid_prefixes
    ):
        return True

    if cleaned_title.isdigit():
        return True

    if has_sale_prefix(
        cleaned_title
    ):
        return True

    return False


def looks_like_demo_title(
    title: str | None,
) -> bool:
    if not title:
        return False

    cleaned_title = remove_sale_prefix(
        title
    ).strip()

    return bool(
        re.search(
            r"(?:^|[\s:()\-–—])demo(?:$|[\s:()\-–—])",
            cleaned_title,
            flags=re.IGNORECASE,
        )
    )


def remove_demo_marker(
    title: str | None,
) -> str | None:
    if not title:
        return None

    cleaned_title = remove_sale_prefix(
        title
    )

    patterns = (
        r"^\s*demo\s*[:\-–—]\s*",
        r"\s*[\(\[]\s*demo\s*[\)\]]\s*$",
        r"\s*[:\-–—]\s*demo\s*$",
        r"\s+demo\s*$",
    )

    for pattern in patterns:
        cleaned_title = re.sub(
            pattern,
            "",
            cleaned_title,
            flags=re.IGNORECASE,
        ).strip()

    cleaned_title = re.sub(
        r"\s{2,}",
        " ",
        cleaned_title,
    ).strip(" -–—:()[]")

    return cleaned_title or None


def normalise_title_for_matching(
    title: str | None,
) -> str:
    if not title:
        return ""

    cleaned_title = html.unescape(
        str(title)
    )

    cleaned_title = remove_sale_prefix(
        cleaned_title
    )

    cleaned_title = remove_demo_marker(
        cleaned_title
    ) or cleaned_title

    cleaned_title = cleaned_title.casefold()

    cleaned_title = cleaned_title.replace(
        "&",
        " and ",
    )

    cleaned_title = re.sub(
        r"[^a-z0-9]+",
        " ",
        cleaned_title,
    )

    return re.sub(
        r"\s+",
        " ",
        cleaned_title,
    ).strip()


def title_similarity(
    first_title: str | None,
    second_title: str | None,
) -> float:
    first_normalised = normalise_title_for_matching(
        first_title
    )

    second_normalised = normalise_title_for_matching(
        second_title
    )

    if (
        not first_normalised
        or not second_normalised
    ):
        return 0.0

    if first_normalised == second_normalised:
        return 1.0

    ratio = SequenceMatcher(
        None,
        first_normalised,
        second_normalised,
    ).ratio()

    first_tokens = set(
        first_normalised.split()
    )

    second_tokens = set(
        second_normalised.split()
    )

    if (
        first_tokens
        and second_tokens
    ):
        token_score = (
            len(first_tokens & second_tokens)
            / len(first_tokens | second_tokens)
        )
    else:
        token_score = 0.0

    containment_bonus = 0.0

    if (
        first_normalised in second_normalised
        or second_normalised in first_normalised
    ):
        containment_bonus = 0.04

    return min(
        1.0,
        max(
            ratio,
            token_score,
        )
        + containment_bonus,
    )


def choose_preferred_game(
    first_game: aiosqlite.Row,
    second_game: aiosqlite.Row,
) -> tuple[aiosqlite.Row, aiosqlite.Row]:
    """
    Returns:
        preferred_game, duplicate_game
    """

    first_invalid = is_invalid_title(
        first_game["name"]
    )

    second_invalid = is_invalid_title(
        second_game["name"]
    )

    if first_invalid and not second_invalid:
        return second_game, first_game

    if second_invalid and not first_invalid:
        return first_game, second_game

    first_score = 0
    second_score = 0

    if first_game["image_url"]:
        first_score += 2

    if second_game["image_url"]:
        second_score += 2

    if first_game["times_played"]:
        first_score += 1

    if second_game["times_played"]:
        second_score += 1

    first_name = str(
        first_game["name"] or ""
    ).strip()

    second_name = str(
        second_game["name"] or ""
    ).strip()

    if (
        first_name
        and not has_sale_prefix(
            first_name
        )
    ):
        first_score += 1

    if (
        second_name
        and not has_sale_prefix(
            second_name
        )
    ):
        second_score += 1

    if second_score > first_score:
        return second_game, first_game

    return first_game, second_game


def get_linked_full_game_id(
    app_data: dict,
) -> str | None:
    full_game = app_data.get(
        "fullgame"
    )

    if not isinstance(
        full_game,
        dict,
    ):
        return None

    app_id = str(
        full_game.get(
            "appid",
            "",
        )
    ).strip()

    if not app_id.isdigit():
        return None

    return app_id


def is_released_full_game(
    details: dict | None,
) -> bool:
    if not details:
        return False

    if str(
        details.get(
            "type",
            "",
        )
    ).casefold() != "game":
        return False

    if details.get(
        "coming_soon"
    ) is True:
        return False

    return True


async def fetch_steam_app_details(
    session: aiohttp.ClientSession,
    app_id: str,
) -> dict | None:
    app_data = await fetch_steam_app_data(
        session,
        app_id,
    )

    if not isinstance(
        app_data,
        dict,
    ):
        return None

    raw_name = str(
        app_data.get(
            "name",
            "",
        )
    ).strip()

    name = remove_sale_prefix(
        raw_name
    )

    if not name:
        return None

    if is_invalid_title(
        name
    ):
        return None

    header_image = str(
        app_data.get(
            "header_image",
            "",
        )
    ).strip() or None

    app_type = str(
        app_data.get(
            "type",
            "",
        )
    ).strip().casefold() or None

    release_date = app_data.get(
        "release_date"
    )

    coming_soon = None
    release_date_text = None

    if isinstance(
        release_date,
        dict,
    ):
        coming_soon_value = release_date.get(
            "coming_soon"
        )

        if isinstance(
            coming_soon_value,
            bool,
        ):
            coming_soon = coming_soon_value

        release_date_text = str(
            release_date.get(
                "date",
                "",
            )
        ).strip() or None

    return {
        "app_id": str(app_id),
        "name": name,
        "image_url": header_image,
        "type": app_type,
        "full_game_app_id": (
            get_linked_full_game_id(
                app_data
            )
        ),
        "coming_soon": coming_soon,
        "release_date": release_date_text,
    }


async def search_steam_app_ids(
    session: aiohttp.ClientSession,
    search_term: str,
) -> list[str]:
    clean_term = str(
        search_term
    ).strip()

    if not clean_term:
        return []

    params = {
        "term": clean_term,
        "f": "games",
        "cc": STEAM_COUNTRY_CODE,
        "l": STEAM_LANGUAGE,
        "use_store_query": "1",
        "use_search_spellcheck": "1",
        "search_creators_and_tags": "1",
    }

    request_url = (
        f"{STEAM_SEARCH_SUGGEST_URL}?"
        f"{urlencode(params)}"
    )

    try:
        async with retrying_request(
            session,
            "GET",
            request_url,
            headers=REPAIR_REQUEST_HEADERS,
            timeout=REPAIR_REQUEST_TIMEOUT,
            allow_redirects=True,
        ) as response:
            if response.status != 200:
                LOGGER.warning(
                    "Steam search returned HTTP %s for %r",
                    response.status,
                    clean_term,
                )
                return []

            response_html = await response.text(
                errors="ignore"
            )

    except (
        aiohttp.ClientError,
        TimeoutError,
    ) as error:
        LOGGER.warning(
            "Steam search failed for %r: %s: %s",
            clean_term,
            type(error).__name__,
            error,
        )
        return []

    parser = SteamSearchSuggestionParser()

    try:
        parser.feed(
            response_html
        )

    except Exception:
        LOGGER.exception(
            "Could not parse Steam search results for %r",
            clean_term,
        )
        return []

    return parser.app_ids[
        :MAX_SEARCH_CANDIDATES
    ]


async def find_full_game_by_title(
    session: aiohttp.ClientSession,
    *,
    demo_title: str,
    excluded_app_id: str | None,
    get_details,
) -> dict:
    search_title = remove_demo_marker(
        demo_title
    )

    if not search_title:
        return {
            "status": "no_search_title",
            "search_title": None,
            "match": None,
            "candidates": [],
        }

    candidate_app_ids = await search_steam_app_ids(
        session,
        search_title,
    )

    candidate_results = []
    eligible_app_ids = [
        candidate_app_id
        for candidate_app_id in candidate_app_ids
        if not (
            excluded_app_id
            and candidate_app_id
            == excluded_app_id
        )
    ]
    candidate_details_list = await asyncio.gather(
        *(
            get_details(candidate_app_id)
            for candidate_app_id in eligible_app_ids
        )
    )

    for candidate_app_id, candidate_details in zip(
        eligible_app_ids,
        candidate_details_list,
    ):

        if not is_released_full_game(
            candidate_details
        ):
            continue

        score = title_similarity(
            search_title,
            candidate_details["name"],
        )

        candidate_results.append(
            {
                "app_id": candidate_app_id,
                "details": candidate_details,
                "score": score,
            }
        )

    candidate_results.sort(
        key=lambda item: item["score"],
        reverse=True,
    )

    if not candidate_results:
        return {
            "status": "no_candidates",
            "search_title": search_title,
            "match": None,
            "candidates": [],
        }

    best_candidate = candidate_results[0]
    second_score = (
        candidate_results[1]["score"]
        if len(candidate_results) > 1
        else 0.0
    )

    exact_match = (
        normalise_title_for_matching(
            search_title
        )
        == normalise_title_for_matching(
            best_candidate["details"]["name"]
        )
    )

    confident_match = (
        exact_match
        or (
            best_candidate["score"]
            >= SEARCH_MATCH_THRESHOLD
            and (
                best_candidate["score"]
                - second_score
            )
            >= SEARCH_MATCH_MARGIN
        )
    )

    if not confident_match:
        return {
            "status": "ambiguous",
            "search_title": search_title,
            "match": None,
            "candidates": candidate_results,
        }

    return {
        "status": "matched",
        "search_title": search_title,
        "match": best_candidate,
        "candidates": candidate_results,
    }


class RepairGames(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
    ):
        self.bot = bot

    async def merge_duplicate_games(
        self,
        db: aiosqlite.Connection,
        *,
        preferred_game: aiosqlite.Row,
        duplicate_game: aiosqlite.Row,
    ) -> None:
        preferred_id = preferred_game["id"]
        duplicate_id = duplicate_game["id"]

        preferred_times_played = int(
            preferred_game["times_played"] or 0
        )

        duplicate_times_played = int(
            duplicate_game["times_played"] or 0
        )

        combined_times_played = (
            preferred_times_played
            + duplicate_times_played
        )

        last_played_values = [
            value
            for value in (
                preferred_game["last_played"],
                duplicate_game["last_played"],
            )
            if value
        ]

        combined_last_played = (
            max(last_played_values)
            if last_played_values
            else None
        )

        await db.execute(
            """
            UPDATE game_history
            SET game_id = ?
            WHERE game_id = ?
            """,
            (
                preferred_id,
                duplicate_id,
            ),
        )

        await db.execute(
            """
            UPDATE games
            SET
                times_played = ?,
                last_played = ?
            WHERE id = ?
            """,
            (
                combined_times_played,
                combined_last_played,
                preferred_id,
            ),
        )

        await db.execute(
            """
            DELETE FROM games
            WHERE id = ?
            """,
            (
                duplicate_id,
            ),
        )

    async def find_full_game_duplicate(
        self,
        db: aiosqlite.Connection,
        *,
        current_game_id: int,
        full_game_app_id: str,
        full_game_name: str,
    ) -> aiosqlite.Row | None:
        cursor = await db.execute(
            """
            SELECT
                id,
                name,
                store_link,
                store,
                image_url,
                external_id,
                times_played,
                last_played
            FROM games
            WHERE
                id != ?
                AND LOWER(
                    COALESCE(store, '')
                ) LIKE '%steam%'
                AND (
                    external_id = ?
                    OR store_link LIKE ?
                    OR LOWER(name) = LOWER(?)
                )
            ORDER BY id
            LIMIT 1
            """,
            (
                current_game_id,
                full_game_app_id,
                f"%/app/{full_game_app_id}/%",
                full_game_name,
            ),
        )

        return await cursor.fetchone()

    async def rebuild_local_artwork(
        self,
        *,
        game_id: int,
        name: str,
        store_link: str,
        image_url: str | None,
        external_id: str,
    ) -> str:
        refreshed_record = {
            "id": game_id,
            "name": name,
            "store_link": store_link,
            "store": "Steam",
            "image_url": image_url,
            "external_id": external_id,
        }

        return await prepare_local_game_artwork(
            bot=self.bot,
            game_record=refreshed_record,
            refresh=True,
        )

    async def upgrade_demo_record(
        self,
        *,
        db: aiosqlite.Connection,
        game: aiosqlite.Row,
        demo_app_id: str,
        full_game_details: dict,
        processed_ids: set,
    ) -> dict:
        game_id = game["id"]
        old_name = game["name"]
        old_link = game["store_link"]
        old_image_url = game["image_url"]
        old_external_id = game[
            "external_id"
        ]

        full_game_app_id = full_game_details[
            "app_id"
        ]

        new_name = full_game_details[
            "name"
        ]

        new_link = build_steam_store_url(
            full_game_app_id
        )

        new_image_url = (
            full_game_details["image_url"]
            or old_image_url
        )

        duplicate = await self.find_full_game_duplicate(
            db,
            current_game_id=game_id,
            full_game_app_id=full_game_app_id,
            full_game_name=new_name,
        )

        duplicate_merged = False
        duplicate_name = None

        if duplicate:
            duplicate_name = duplicate["name"]

            await self.merge_duplicate_games(
                db,
                preferred_game=game,
                duplicate_game=duplicate,
            )

            await db.commit()

            await delete_local_game_artwork(
                duplicate["id"]
            )

            processed_ids.add(
                duplicate["id"]
            )

            duplicate_merged = True

        try:
            await db.execute(
                """
                UPDATE games
                SET
                    name = ?,
                    store_link = ?,
                    store = 'Steam',
                    image_url = ?,
                    external_id = ?,
                    link_status = 'live',
                    http_status = 200,
                    last_link_check = ?
                WHERE id = ?
                """,
                (
                    new_name,
                    new_link,
                    new_image_url,
                    full_game_app_id,
                    utc_now_iso(),
                    game_id,
                ),
            )

        except aiosqlite.IntegrityError:
            return {
                "success": False,
                "reason": (
                    "the full-game title conflicts "
                    "with another database entry"
                ),
            }

        await db.commit()

        artwork_result = await self.rebuild_local_artwork(
            game_id=game_id,
            name=new_name,
            store_link=new_link,
            image_url=new_image_url,
            external_id=full_game_app_id,
        )

        await save_store_replacement(
            store="Steam",
            old_external_id=demo_app_id,
            old_store_link=old_link,
            old_name=old_name,
            game_id=game_id,
            new_external_id=full_game_app_id,
            new_store_link=new_link,
            new_name=new_name,
        )

        return {
            "success": True,
            "game_id": game_id,
            "old_name": old_name,
            "new_name": new_name,
            "demo_app_id": demo_app_id,
            "full_game_app_id": full_game_app_id,
            "title_changed": (
                new_name != old_name
            ),
            "id_changed": (
                str(
                    old_external_id or ""
                ).strip()
                != full_game_app_id
            ),
            "link_changed": (
                old_link != new_link
            ),
            "artwork_changed": (
                bool(new_image_url)
                and new_image_url
                != old_image_url
            ),
            "duplicate_merged": duplicate_merged,
            "duplicate_name": duplicate_name,
            "artwork_rebuilt": (
                artwork_result
                in {
                    "cached",
                    "already_cached",
                }
            ),
        }

    async def auto_upgrade_demo_by_app_id(
        self,
        app_id: str,
    ) -> dict:
        clean_app_id = str(
            app_id or ""
        ).strip()

        if not clean_app_id.isdigit():
            return {
                "status": "invalid_app_id",
            }

        async with database_connection() as db:

            cursor = await db.execute(
                """
                SELECT
                    id,
                    name,
                    store_link,
                    store,
                    image_url,
                    external_id,
                    times_played,
                    last_played
                FROM games
                WHERE
                    LOWER(
                        COALESCE(store, '')
                    ) LIKE '%steam%'
                    AND (
                        external_id = ?
                        OR store_link LIKE ?
                    )
                ORDER BY id
                LIMIT 1
                """,
                (
                    clean_app_id,
                    f"%/app/{clean_app_id}/%",
                ),
            )

            game = await cursor.fetchone()

            if not game:
                return {
                    "status": "not_found",
                }

            async with contextlib.nullcontext(
                self.bot.http_session
            ) as session:
                metadata_cache = {}

                async def get_details(
                    requested_app_id: str,
                ) -> dict | None:
                    if (
                        requested_app_id
                        not in metadata_cache
                    ):
                        metadata_cache[
                            requested_app_id
                        ] = (
                            await fetch_steam_app_details(
                                session,
                                requested_app_id,
                            )
                        )

                    return metadata_cache[
                        requested_app_id
                    ]

                details = await get_details(
                    clean_app_id
                )

                known_full_game_app_id = (
                    KNOWN_OBSOLETE_DEMO_REPLACEMENTS.get(
                        clean_app_id
                    )
                )

                if known_full_game_app_id:
                    known_full_game_details = (
                        await get_details(
                            known_full_game_app_id
                        )
                    )

                    if is_released_full_game(
                        known_full_game_details
                    ):
                        result = (
                            await self.upgrade_demo_record(
                                db=db,
                                game=game,
                                demo_app_id=clean_app_id,
                                full_game_details=(
                                    known_full_game_details
                                ),
                                processed_ids=set(),
                            )
                        )

                        if not result.get(
                            "success"
                        ):
                            return {
                                "status": "failed",
                                "name": game["name"],
                                "reason": result.get(
                                    "reason"
                                ),
                            }

                        return {
                            "status": "upgraded",
                            "old_name": (
                                result["old_name"]
                                or (
                                    "Obsolete Steam demo "
                                    f"{clean_app_id}"
                                )
                            ),
                            "new_name": result[
                                "new_name"
                            ],
                            "old_app_id": result[
                                "demo_app_id"
                            ],
                            "new_app_id": result[
                                "full_game_app_id"
                            ],
                            "match_source": (
                                "known obsolete demo mapping"
                            ),
                        }

                title_looks_like_demo = (
                    looks_like_demo_title(
                        game["name"]
                    )
                )

                metadata_says_demo = bool(
                    details
                    and details.get(
                        "type"
                    ) == "demo"
                )

                if not (
                    title_looks_like_demo
                    or metadata_says_demo
                ):
                    return {
                        "status": "not_demo",
                    }

                full_game_details = None
                match_source = None

                if metadata_says_demo:
                    linked_app_id = details.get(
                        "full_game_app_id"
                    )

                    if (
                        linked_app_id
                        and linked_app_id
                        != clean_app_id
                    ):
                        linked_details = await get_details(
                            linked_app_id
                        )

                        if is_released_full_game(
                            linked_details
                        ):
                            full_game_details = (
                                linked_details
                            )
                            match_source = "Steam link"

                        elif linked_details:
                            return {
                                "status": "waiting",
                                "name": game["name"],
                                "reason": (
                                    "linked full game is not "
                                    "released yet"
                                ),
                            }

                if full_game_details is None:
                    search_title = (
                        details["name"]
                        if details
                        else game["name"]
                    )

                    search_result = (
                        await find_full_game_by_title(
                            session,
                            demo_title=search_title,
                            excluded_app_id=clean_app_id,
                            get_details=get_details,
                        )
                    )

                    if (
                        search_result["status"]
                        == "matched"
                    ):
                        full_game_details = (
                            search_result["match"][
                                "details"
                            ]
                        )
                        match_source = "title search"

                    elif (
                        search_result["status"]
                        == "ambiguous"
                    ):
                        return {
                            "status": "ambiguous",
                            "name": game["name"],
                        }

                    else:
                        return {
                            "status": "no_match",
                            "name": game["name"],
                        }

                result = await self.upgrade_demo_record(
                    db=db,
                    game=game,
                    demo_app_id=clean_app_id,
                    full_game_details=full_game_details,
                    processed_ids=set(),
                )

                if not result.get(
                    "success"
                ):
                    return {
                        "status": "failed",
                        "name": game["name"],
                        "reason": result.get(
                            "reason"
                        ),
                    }

                return {
                    "status": "upgraded",
                    "old_name": result[
                        "old_name"
                    ],
                    "new_name": result[
                        "new_name"
                    ],
                    "old_app_id": result[
                        "demo_app_id"
                    ],
                    "new_app_id": result[
                        "full_game_app_id"
                    ],
                    "match_source": match_source,
                }

    @app_commands.command(
        name="repairgames",
        description=(
            "Repair Steam games, recover deleted "
            "demos, and merge duplicates"
        ),
    )
    @app_commands.checks.has_permissions(
        manage_guild=True
    )
    async def repairgames(
        self,
        interaction: discord.Interaction,
    ):
        maintenance_lock = self.bot.maintenance_lock

        if maintenance_lock.locked():
            await interaction.response.send_message(
                "A maintenance command is already running. "
                "Please wait for it to finish before starting "
                "another maintenance task.",
                ephemeral=True,
            )
            return

        async with maintenance_lock:
            try:
                await self._run_repairgames(
                    interaction
                )

            finally:
                released_entries = (
                    clear_steam_details_cache()
                )
                LOGGER.debug(
                    "Released %s Steam metadata cache entries "
                    "after /repairgames",
                    released_entries,
                )

    async def _run_repairgames(
        self,
        interaction: discord.Interaction,
    ):
        await interaction.response.defer(
            ephemeral=True
        )

        scanned = 0
        repaired_titles = 0
        repaired_ids = 0
        repaired_links = 0
        repaired_artwork = 0
        repaired_player_limits = 0
        repaired_igdb_metadata = 0
        artwork_rebuilt = 0
        already_valid = 0
        duplicates_merged = 0
        demos_found = 0
        demos_recovered_by_search = 0
        demos_upgraded = 0
        demos_waiting = 0
        demos_unmatched = 0
        demos_without_link = 0
        demo_matches_ambiguous = 0
        failed = 0

        repaired_names = []
        merged_names = []
        demo_upgrades = []
        waiting_demos = []
        ambiguous_demos = []
        failed_games = []

        async with database_connection() as db:
            cursor = await db.execute(
                """
                SELECT
                    id,
                    name,
                    store_link,
                    store,
                    image_url,
                    external_id,
                    times_played,
                    last_played,
                    max_players,
                    max_players_source,
                    igdb_id,
                    multiplayer_support_json,
                    genres_json,
                    themes_json,
                    game_modes_json
                FROM games
                WHERE LOWER(
                    COALESCE(store, '')
                ) LIKE '%steam%'
                ORDER BY id
                """
            )

            games = await cursor.fetchall()

        async with contextlib.nullcontext(
            self.bot.http_session
        ) as session:
            player_metadata_by_id = {}

            for game in games:
                external_id = str(
                    game["external_id"] or ""
                ).strip()

                if not external_id:
                    external_id = (
                        extract_steam_app_id(
                            game["store_link"]
                        )
                        or ""
                    )

                player_metadata_by_id[game["id"]] = {
                    "name": game["name"],
                    "store": "Steam",
                    "store_link": game["store_link"],
                    "external_id": external_id,
                    "max_players": game["max_players"],
                    "max_players_source": game[
                        "max_players_source"
                    ],
                }

            try:
                await enrich_missing_player_metadata(
                    session,
                    player_metadata_by_id.values(),
                    force_refresh=True,
                )

            except Exception:
                LOGGER.exception(
                    "IGDB enrichment failed during "
                    "/repairgames; Steam repair continued"
                )

            processed_ids = set()
            metadata_tasks = {}
            steam_lookup_limit = asyncio.Semaphore(4)

            async def fetch_limited_details(
                requested_app_id: str,
            ) -> dict | None:
                async with steam_lookup_limit:
                    return await fetch_steam_app_details(
                        session,
                        requested_app_id,
                    )

            def start_details_lookup(
                requested_app_id: str,
            ) -> asyncio.Task:
                if requested_app_id not in metadata_tasks:
                    metadata_tasks[requested_app_id] = (
                        asyncio.create_task(
                            fetch_limited_details(
                                requested_app_id
                            )
                        )
                    )

                return metadata_tasks[requested_app_id]

            async def get_details(
                requested_app_id: str,
            ) -> dict | None:
                return await start_details_lookup(
                    requested_app_id
                )

            # Prime every known Steam lookup together. The
            # semaphore keeps pressure on Steam reasonable.
            for game in games:
                initial_app_id = str(
                    game["external_id"] or ""
                ).strip()

                if not initial_app_id:
                    initial_app_id = (
                        extract_steam_app_id(
                            game["store_link"]
                        )
                        or ""
                    )

                if initial_app_id:
                    start_details_lookup(initial_app_id)

            if metadata_tasks:
                await asyncio.gather(
                    *metadata_tasks.values()
                )

            async with database_connection() as db:
                for original_game in games:
                    original_id = original_game["id"]

                    if original_id in processed_ids:
                        continue

                    game = original_game

                    scanned += 1

                    game_id = game["id"]
                    old_name = game["name"]
                    old_link = game["store_link"]
                    old_image_url = game["image_url"]
                    old_external_id = game[
                        "external_id"
                    ]
                    old_max_players = game[
                        "max_players"
                    ]
                    old_max_players_source = game[
                        "max_players_source"
                    ]
                    old_igdb_id = game["igdb_id"]
                    old_multiplayer_support = game[
                        "multiplayer_support_json"
                    ]
                    old_genres = game["genres_json"]
                    old_themes = game["themes_json"]
                    old_game_modes = game[
                        "game_modes_json"
                    ]

                    app_id = (
                        str(
                            old_external_id
                        ).strip()
                        if old_external_id
                        else None
                    )

                    if not app_id:
                        app_id = extract_steam_app_id(
                            old_link
                        )

                    if not app_id:
                        failed += 1
                        failed_games.append(
                            f"{old_name or 'Unknown Game'} "
                            "(missing Steam app ID)"
                        )
                        continue

                    stored_title_looks_like_demo = (
                        looks_like_demo_title(
                            old_name
                        )
                    )

                    details = await get_details(
                        app_id
                    )

                    metadata_says_demo = bool(
                        details
                        and details.get(
                            "type"
                        ) == "demo"
                    )

                    is_demo_candidate = (
                        stored_title_looks_like_demo
                        or metadata_says_demo
                    )

                    if is_demo_candidate:
                        demos_found += 1

                        full_game_details = None
                        match_source = None
                        waiting_reason = None

                        if metadata_says_demo:
                            full_game_app_id = details.get(
                                "full_game_app_id"
                            )

                            if (
                                full_game_app_id
                                and full_game_app_id != app_id
                            ):
                                linked_details = await get_details(
                                    full_game_app_id
                                )

                                if is_released_full_game(
                                    linked_details
                                ):
                                    full_game_details = (
                                        linked_details
                                    )
                                    match_source = "Steam link"

                                elif linked_details:
                                    if linked_details.get(
                                        "coming_soon"
                                    ) is True:
                                        demos_waiting += 1

                                    else:
                                        demos_unmatched += 1

                                    waiting_reason = (
                                        "full game is still "
                                        "coming soon"
                                        if linked_details.get(
                                            "coming_soon"
                                        ) is True
                                        else (
                                            "linked app is not "
                                            "confirmed as a released "
                                            "full game"
                                        )
                                    )

                                    waiting_demos.append(
                                        (
                                            old_name
                                            or details["name"],
                                            full_game_app_id,
                                            waiting_reason,
                                        )
                                    )

                                else:
                                    waiting_reason = (
                                        "linked full game could "
                                        "not be read"
                                    )

                            elif full_game_app_id == app_id:
                                waiting_reason = (
                                    "Steam returned the demo's "
                                    "own app ID as its full game"
                                )

                        should_try_title_search = (
                            full_game_details is None
                            and waiting_reason
                            not in {
                                "full game is still coming soon",
                                (
                                    "linked app is not confirmed "
                                    "as a released full game"
                                ),
                            }
                        )

                        if should_try_title_search:
                            search_source_title = (
                                details["name"]
                                if details
                                else old_name
                            )

                            search_result = (
                                await find_full_game_by_title(
                                    session,
                                    demo_title=search_source_title,
                                    excluded_app_id=app_id,
                                    get_details=get_details,
                                )
                            )

                            if (
                                search_result["status"]
                                == "matched"
                            ):
                                full_game_details = (
                                    search_result[
                                        "match"
                                    ][
                                        "details"
                                    ]
                                )
                                match_source = "title search"
                                demos_recovered_by_search += 1

                            elif (
                                search_result["status"]
                                == "ambiguous"
                            ):
                                demo_matches_ambiguous += 1

                                candidate_text = []

                                for candidate in (
                                    search_result[
                                        "candidates"
                                    ][:3]
                                ):
                                    candidate_text.append(
                                        (
                                            candidate[
                                                "details"
                                            ][
                                                "name"
                                            ],
                                            candidate[
                                                "app_id"
                                            ],
                                            candidate[
                                                "score"
                                            ],
                                        )
                                    )

                                ambiguous_demos.append(
                                    (
                                        old_name
                                        or search_source_title,
                                        search_result[
                                            "search_title"
                                        ],
                                        candidate_text,
                                    )
                                )

                            else:
                                demos_unmatched += 1

                                if metadata_says_demo:
                                    if not details.get(
                                        "full_game_app_id"
                                    ):
                                        demos_without_link += 1

                                waiting_reason = (
                                    "no trustworthy released "
                                    "full-game match was found"
                                )

                                waiting_demos.append(
                                    (
                                        old_name
                                        or search_source_title,
                                        None,
                                        waiting_reason,
                                    )
                                )

                        if full_game_details:
                            upgrade_result = (
                                await self.upgrade_demo_record(
                                    db=db,
                                    game=game,
                                    demo_app_id=app_id,
                                    full_game_details=(
                                        full_game_details
                                    ),
                                    processed_ids=processed_ids,
                                )
                            )

                            if not upgrade_result[
                                "success"
                            ]:
                                failed += 1
                                failed_games.append(
                                    (
                                        f"{old_name or 'Unknown Demo'} "
                                        f"({upgrade_result['reason']})"
                                    )
                                )
                                continue

                            demos_upgraded += 1

                            repaired_titles += int(
                                upgrade_result[
                                    "title_changed"
                                ]
                            )

                            repaired_ids += int(
                                upgrade_result[
                                    "id_changed"
                                ]
                            )

                            repaired_links += int(
                                upgrade_result[
                                    "link_changed"
                                ]
                            )

                            repaired_artwork += int(
                                upgrade_result[
                                    "artwork_changed"
                                ]
                            )

                            artwork_rebuilt += int(
                                upgrade_result[
                                    "artwork_rebuilt"
                                ]
                            )

                            if upgrade_result[
                                "duplicate_merged"
                            ]:
                                duplicates_merged += 1

                                merged_names.append(
                                    (
                                        upgrade_result[
                                            "duplicate_name"
                                        ],
                                        upgrade_result[
                                            "new_name"
                                        ],
                                        upgrade_result[
                                            "full_game_app_id"
                                        ],
                                    )
                                )

                            demo_upgrades.append(
                                (
                                    upgrade_result[
                                        "old_name"
                                    ]
                                    or (
                                        details["name"]
                                        if details
                                        else "Unknown Demo"
                                    ),
                                    upgrade_result[
                                        "new_name"
                                    ],
                                    upgrade_result[
                                        "demo_app_id"
                                    ],
                                    upgrade_result[
                                        "full_game_app_id"
                                    ],
                                    match_source
                                    or "automatic match",
                                )
                            )

                            processed_ids.add(
                                game_id
                            )

                            continue

                        if metadata_says_demo:
                            linked_full_game_id = (
                                details.get(
                                    "full_game_app_id"
                                )
                            )

                            if (
                                linked_full_game_id
                                and waiting_reason
                                == (
                                    "full game is still "
                                    "coming soon"
                                )
                            ):
                                continue

                        if stored_title_looks_like_demo:
                            continue

                    if not details:
                        failed += 1
                        failed_games.append(
                            f"{old_name or 'Unknown Game'} "
                            f"(Steam app {app_id})"
                        )
                        continue

                    duplicate_cursor = await db.execute(
                        """
                        SELECT
                            id,
                            name,
                            store_link,
                            store,
                            image_url,
                            external_id,
                            times_played,
                            last_played,
                            max_players,
                            max_players_source
                        FROM games
                        WHERE
                            id != ?
                            AND LOWER(
                                COALESCE(store, '')
                            ) LIKE '%steam%'
                            AND (
                                external_id = ?
                                OR store_link LIKE ?
                            )
                        ORDER BY id
                        LIMIT 1
                        """,
                        (
                            game_id,
                            app_id,
                            f"%/app/{app_id}/%",
                        ),
                    )

                    duplicate = await duplicate_cursor.fetchone()

                    if duplicate:
                        preferred, duplicate_to_remove = (
                            choose_preferred_game(
                                game,
                                duplicate,
                            )
                        )

                        await self.merge_duplicate_games(
                            db,
                            preferred_game=preferred,
                            duplicate_game=duplicate_to_remove,
                        )

                        await db.commit()

                        await delete_local_game_artwork(
                            duplicate_to_remove["id"]
                        )

                        processed_ids.add(
                            duplicate_to_remove["id"]
                        )

                        duplicates_merged += 1

                        merged_names.append(
                            (
                                duplicate_to_remove["name"],
                                preferred["name"],
                                app_id,
                            )
                        )

                        game = preferred
                        game_id = preferred["id"]
                        old_name = preferred["name"]
                        old_link = preferred["store_link"]
                        old_image_url = preferred["image_url"]
                        old_external_id = preferred[
                            "external_id"
                        ]
                        old_max_players = preferred[
                            "max_players"
                        ]
                        old_max_players_source = preferred[
                            "max_players_source"
                        ]

                        # This surviving row is being fully
                        # processed now, so do not revisit its
                        # stale copy later in the original list.
                        if game_id != original_id:
                            processed_ids.add(game_id)

                    canonical_link = (
                        build_steam_store_url(
                            app_id
                        )
                    )

                    details_name = remove_sale_prefix(
                        details["name"]
                    )

                    title_changed = (
                        is_invalid_title(
                            old_name
                        )
                        or not titles_are_equivalent(
                            old_name,
                            details_name,
                        )
                    )

                    id_changed = (
                        str(
                            old_external_id or ""
                        ).strip()
                        != app_id
                    )

                    link_changed = (
                        not steam_links_are_equivalent(
                            old_link,
                            canonical_link,
                        )
                    )

                    artwork_changed = (
                        not bool(old_image_url)
                        and bool(details["image_url"])
                    )

                    player_metadata = (
                        player_metadata_by_id.get(
                            game_id,
                            {},
                        )
                    )
                    existing_support = {}

                    if isinstance(
                        old_multiplayer_support,
                        str,
                    ):
                        try:
                            decoded_support = json.loads(
                                old_multiplayer_support
                            )

                        except (
                            TypeError,
                            ValueError,
                        ):
                            decoded_support = {}

                        if isinstance(decoded_support, dict):
                            existing_support = decoded_support

                    elif isinstance(
                        old_multiplayer_support,
                        dict,
                    ):
                        existing_support = dict(
                            old_multiplayer_support
                        )

                    igdb_support = player_metadata.get(
                        "multiplayer_support"
                    )

                    if not isinstance(igdb_support, dict):
                        igdb_support = {}

                    steam_support = details.get(
                        "multiplayer_support"
                    )

                    if not isinstance(steam_support, dict):
                        steam_support = {}

                    combined_support = {
                        **existing_support,
                        **igdb_support,
                        **steam_support,
                    }

                    if combined_support:
                        player_metadata[
                            "multiplayer_support"
                        ] = combined_support

                    igdb_max_players = (
                        player_metadata.get(
                            "max_players"
                        )
                    )
                    steam_max_players = details.get(
                        "max_players"
                    )

                    if (
                        steam_max_players is not None
                        and (
                            old_max_players is None
                            or old_max_players_source
                            != "Steam"
                        )
                    ):
                        new_max_players = steam_max_players
                        new_max_players_source = "Steam"

                    elif (
                        old_max_players is None
                        and igdb_max_players is not None
                        and player_metadata.get(
                            "max_players_source"
                        ) == "IGDB"
                    ):
                        new_max_players = igdb_max_players
                        new_max_players_source = "IGDB"

                    else:
                        new_max_players = old_max_players
                        new_max_players_source = (
                            old_max_players_source
                        )

                    player_limit_changed = bool(
                        new_max_players != old_max_players
                        or new_max_players_source
                        != old_max_players_source
                    )

                    def encoded_metadata(
                        key: str,
                        old_value,
                    ):
                        if key not in player_metadata:
                            return old_value

                        return json.dumps(
                            player_metadata[key],
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        )

                    new_igdb_id = (
                        player_metadata.get("igdb_id")
                        or old_igdb_id
                    )
                    new_multiplayer_support = (
                        encoded_metadata(
                            "multiplayer_support",
                            old_multiplayer_support,
                        )
                    )
                    new_genres = encoded_metadata(
                        "genres",
                        old_genres,
                    )
                    new_themes = encoded_metadata(
                        "themes",
                        old_themes,
                    )
                    new_game_modes = encoded_metadata(
                        "game_modes",
                        old_game_modes,
                    )
                    igdb_metadata_changed = any(
                        (
                            new_igdb_id != old_igdb_id,
                            new_multiplayer_support
                            != old_multiplayer_support,
                            new_genres != old_genres,
                            new_themes != old_themes,
                            new_game_modes != old_game_modes,
                        )
                    )

                    needs_metadata_repair = any(
                        (
                            title_changed,
                            id_changed,
                            link_changed,
                            artwork_changed,
                            player_limit_changed,
                            igdb_metadata_changed,
                        )
                    )

                    if not needs_metadata_repair:
                        already_valid += 1
                        continue

                    new_name = (
                        details_name
                        if title_changed
                        else old_name
                    )

                    new_link = (
                        canonical_link
                        if link_changed
                        else old_link
                    )

                    new_external_id = (
                        app_id
                        if id_changed
                        else old_external_id
                    )

                    new_image_url = (
                        details["image_url"]
                        if artwork_changed
                        else old_image_url
                    )

                    try:
                        await db.execute(
                            """
                            UPDATE games
                            SET
                                name = ?,
                                store_link = ?,
                                store = 'Steam',
                                image_url = ?,
                                external_id = ?,
                                link_status = 'live',
                                http_status = 200,
                                last_link_check = ?,
                                max_players = ?,
                                max_players_source = ?,
                                igdb_id = ?,
                                multiplayer_support_json = ?,
                                genres_json = ?,
                                themes_json = ?,
                                game_modes_json = ?
                            WHERE id = ?
                            """,
                            (
                                new_name,
                                new_link,
                                new_image_url,
                                new_external_id,
                                utc_now_iso(),
                                new_max_players,
                                new_max_players_source,
                                new_igdb_id,
                                new_multiplayer_support,
                                new_genres,
                                new_themes,
                                new_game_modes,
                                game_id,
                            ),
                        )

                    except aiosqlite.IntegrityError:
                        failed += 1
                        failed_games.append(
                            f"{old_name or 'Unknown Game'} "
                            "(corrected title conflicts with "
                            "another database entry)"
                        )
                        continue

                    await db.commit()

                    if title_changed:
                        repaired_titles += 1

                    if id_changed:
                        repaired_ids += 1

                    if link_changed:
                        repaired_links += 1

                    if artwork_changed:
                        repaired_artwork += 1

                    if player_limit_changed:
                        repaired_player_limits += 1

                    if igdb_metadata_changed:
                        repaired_igdb_metadata += 1

                    repaired_fields = []

                    if title_changed:
                        repaired_fields.append("title")

                    if id_changed:
                        repaired_fields.append("app ID")

                    if link_changed:
                        repaired_fields.append("store link")

                    if artwork_changed:
                        repaired_fields.append("artwork URL")

                    if player_limit_changed:
                        repaired_fields.append(
                            "player limit "
                            f"({new_max_players_source or 'metadata'})"
                        )

                    if igdb_metadata_changed:
                        repaired_fields.append(
                            "IGDB game details"
                        )

                    artwork_result = None

                    if any(
                        (
                            title_changed,
                            id_changed,
                            link_changed,
                            artwork_changed,
                        )
                    ):
                        artwork_result = (
                            await self.rebuild_local_artwork(
                                game_id=game_id,
                                name=new_name,
                                store_link=new_link,
                                image_url=new_image_url,
                                external_id=app_id,
                            )
                        )

                    if artwork_result in {
                        "cached",
                        "already_cached",
                    }:
                        artwork_rebuilt += 1

                    repaired_names.append(
                        (
                            old_name or "Unknown Game",
                            new_name,
                            app_id,
                            repaired_fields,
                        )
                    )

        result_lines = [
            "## 🛠️ Game Repair Complete",
            "",
            f"🔍 Steam games scanned: **{scanned}**",
            f"🧪 Demos detected: **{demos_found}**",
            f"🔎 Deleted demos matched by search: "
            f"**{demos_recovered_by_search}**",
            f"🎮 Demos upgraded: **{demos_upgraded}**",
            f"⏳ Known demos awaiting release: "
            f"**{demos_waiting}**",
            f"🔎 Demos without a trustworthy match: "
            f"**{demos_unmatched}**",
            f"🔗 Demos missing a full-game link: "
            f"**{demos_without_link}**",
            f"⚠️ Ambiguous demo matches: "
            f"**{demo_matches_ambiguous}**",
            f"🔀 Duplicate games merged: "
            f"**{duplicates_merged}**",
            f"📝 Titles corrected: **{repaired_titles}**",
            f"🆔 App IDs corrected: **{repaired_ids}**",
            f"🔗 Store links corrected: **{repaired_links}**",
            f"🖼️ Artwork URLs corrected: "
            f"**{repaired_artwork}**",
            f"🧾 IGDB metadata updated: "
            f"**{repaired_igdb_metadata}**",
            f"👥 Player limits corrected: "
            f"**{repaired_player_limits}**",
            f"📥 Local artwork rebuilt: "
            f"**{artwork_rebuilt}**",
            f"👌 Already valid: **{already_valid}**",
            f"❌ Could not repair: **{failed}**",
        ]

        if demo_upgrades:
            result_lines.extend(
                [
                    "",
                    "### 🎮 Demo upgrades",
                ]
            )

            for (
                demo_name,
                full_name,
                demo_app_id,
                full_app_id,
                match_source,
            ) in demo_upgrades[:15]:
                result_lines.append(
                    f"• `{demo_name}` → "
                    f"**{full_name}** "
                    f"(`{demo_app_id}` → `{full_app_id}`, "
                    f"{match_source})"
                )

        if ambiguous_demos:
            result_lines.extend(
                [
                    "",
                    "### ⚠️ Ambiguous demo matches",
                ]
            )

            for (
                demo_name,
                search_title,
                candidates,
            ) in ambiguous_demos[:10]:
                result_lines.append(
                    f"• **{demo_name}** "
                    f"(searched `{search_title}`)"
                )

                for (
                    candidate_name,
                    candidate_app_id,
                    score,
                ) in candidates:
                    result_lines.append(
                        f"  ↳ {candidate_name} "
                        f"(`{candidate_app_id}`, "
                        f"{score:.0%} match)"
                    )

        if merged_names:
            result_lines.extend(
                [
                    "",
                    "### Duplicate games merged",
                ]
            )

            for (
                removed_name,
                kept_name,
                app_id,
            ) in merged_names[:15]:
                result_lines.append(
                    f"• Removed `{removed_name}` and kept "
                    f"**{kept_name}** (`{app_id}`)"
                )

        if repaired_names:
            result_lines.extend(
                [
                    "",
                    "### Metadata repaired",
                ]
            )

            for (
                old_name,
                new_name,
                app_id,
                repaired_fields,
            ) in repaired_names[:15]:
                field_text = ", ".join(
                    repaired_fields
                )

                if "title" in repaired_fields:
                    result_lines.append(
                        f"• `{old_name}` → "
                        f"**{new_name}** (`{app_id}`; "
                        f"{field_text})"
                    )

                else:
                    result_lines.append(
                        f"• **{new_name}** (`{app_id}`; "
                        f"{field_text})"
                    )

        if failed_games:
            result_lines.extend(
                [
                    "",
                    "### Needs manual attention",
                ]
            )

            for failed_game in failed_games[:10]:
                result_lines.append(
                    f"• {failed_game}"
                )

        await interaction.followup.send(
            "\n".join(
                result_lines
            ),
            ephemeral=True,
        )

    @repairgames.error
    async def repairgames_error(
        self,
        interaction: discord.Interaction,
        error,
    ):
        if isinstance(
            error,
            app_commands.errors.MissingPermissions,
        ):
            message = (
                "❌ You need moderator permissions "
                "to use `/repairgames`."
            )

            if interaction.response.is_done():
                await interaction.followup.send(
                    message,
                    ephemeral=True,
                )

            else:
                await interaction.response.send_message(
                    message,
                    ephemeral=True,
                )

            return

        raise error


async def setup(
    bot: commands.Bot,
):
    await bot.add_cog(
        RepairGames(bot)
    )
