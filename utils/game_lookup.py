import asyncio
import re
import unicodedata
from difflib import SequenceMatcher

from utils.igdb import enrich_missing_player_metadata
from utils.store import (
    get_game_info_from_url,
    search_steam_app_ids_by_title,
)


STEAM_TITLE_MATCH_THRESHOLD = 0.62


def normalise_game_title(value: str) -> str:
    without_symbols = "".join(
        character
        for character in str(value or "")
        if not unicodedata.category(character).startswith("S")
    )
    text = unicodedata.normalize(
        "NFKD",
        without_symbols,
    ).casefold()
    text = "".join(
        character
        for character in text
        if not unicodedata.combining(character)
    )
    return " ".join(
        re.sub(r"[^a-z0-9]+", " ", text).split()
    )


def game_title_similarity(
    expected: str,
    candidate: str,
) -> float:
    clean_expected = normalise_game_title(expected)
    clean_candidate = normalise_game_title(candidate)

    if not clean_expected or not clean_candidate:
        return 0.0

    if clean_expected == clean_candidate:
        return 1.0

    return SequenceMatcher(
        None,
        clean_expected,
        clean_candidate,
    ).ratio()


async def find_game_by_title(
    session,
    title: str,
) -> dict:
    """Resolve a title through Steam, then enrich or fall back via IGDB."""

    clean_title = str(title or "").strip()
    app_ids = await search_steam_app_ids_by_title(
        session,
        clean_title,
    )
    game_infos = await asyncio.gather(
        *(
            get_game_info_from_url(
                session,
                f"https://store.steampowered.com/app/{app_id}/",
            )
            for app_id in app_ids
        )
    ) if app_ids else []
    candidates = []

    for position, game_info in enumerate(game_infos):
        if not isinstance(game_info, dict):
            continue

        score = game_title_similarity(
            clean_title,
            game_info.get("name"),
        )
        candidates.append(
            (
                score,
                -position,
                game_info,
            )
        )

    candidates.sort(reverse=True, key=lambda item: item[:2])

    if (
        candidates
        and candidates[0][0] >= STEAM_TITLE_MATCH_THRESHOLD
    ):
        score, _position, game_info = candidates[0]
        game_info = dict(game_info)
        game_info["title_lookup_source"] = "Steam title search"
        game_info["title_lookup_score"] = score
        game_info["title_lookup_query"] = clean_title
        await enrich_missing_player_metadata(
            session,
            [game_info],
        )
        return game_info

    fallback = {
        "name": clean_title,
        "store_link": None,
        "source_link": None,
        "store": "Custom",
        "link_status": "unknown",
        "title_lookup_source": "typed title",
        "title_lookup_score": None,
        "title_lookup_query": clean_title,
    }
    await enrich_missing_player_metadata(
        session,
        [fallback],
    )

    if fallback.get("igdb_id") is not None:
        fallback["title_lookup_source"] = "IGDB exact-title match"

    return fallback
