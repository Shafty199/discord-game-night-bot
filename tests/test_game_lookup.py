import unittest
from unittest.mock import AsyncMock, patch

from utils.game_lookup import (
    find_game_by_title,
    game_title_similarity,
    normalise_game_title,
)
from utils.store import SteamTitleSearchParser


class GameTitleLookupTests(unittest.IsolatedAsyncioTestCase):
    def test_steam_search_parser_extracts_unique_app_ids(self):
        parser = SteamTitleSearchParser()
        parser.feed(
            '<a data-ds-appid="1966720" '
            'href="https://store.steampowered.com/app/1966720/">A</a>'
            '<a data-ds-itemkey="App_553310">B</a>'
        )
        self.assertEqual(
            parser.app_ids,
            ["1966720", "553310"],
        )

    def test_title_normalisation_handles_symbols_and_accents(self):
        self.assertEqual(
            normalise_game_title("Pokémon™: Co-op!"),
            "pokemon co op",
        )
        self.assertEqual(
            game_title_similarity(
                "Lethal Company",
                "LETHAL COMPANY™",
            ),
            1.0,
        )

    async def test_best_steam_match_is_enriched(self):
        steam_results = [
            {
                "name": "Lethal League Blaze",
                "store": "Steam",
            },
            {
                "name": "Lethal Company",
                "store": "Steam",
                "external_id": "1966720",
            },
        ]

        with (
            patch(
                "utils.game_lookup.search_steam_app_ids_by_title",
                new=AsyncMock(return_value=["553310", "1966720"]),
            ),
            patch(
                "utils.game_lookup.get_game_info_from_url",
                new=AsyncMock(side_effect=steam_results),
            ),
            patch(
                "utils.game_lookup.enrich_missing_player_metadata",
                new=AsyncMock(return_value=1),
            ) as enrich,
        ):
            result = await find_game_by_title(
                object(),
                "Lethal Company",
            )

        self.assertEqual(result["external_id"], "1966720")
        self.assertEqual(
            result["title_lookup_source"],
            "Steam title search",
        )
        self.assertEqual(result["title_lookup_score"], 1.0)
        enrich.assert_awaited_once()

    async def test_igdb_can_enrich_the_typed_title_fallback(self):
        async def enrich_fallback(_session, game_infos):
            game_infos[0]["igdb_id"] = 1234
            game_infos[0]["max_players"] = 8
            game_infos[0]["max_players_source"] = "IGDB"
            return 1

        with (
            patch(
                "utils.game_lookup.search_steam_app_ids_by_title",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "utils.game_lookup.enrich_missing_player_metadata",
                new=AsyncMock(side_effect=enrich_fallback),
            ),
        ):
            result = await find_game_by_title(
                object(),
                "Only on IGDB",
            )

        self.assertEqual(result["igdb_id"], 1234)
        self.assertEqual(result["max_players"], 8)
        self.assertEqual(
            result["title_lookup_source"],
            "IGDB exact-title match",
        )


if __name__ == "__main__":
    unittest.main()
