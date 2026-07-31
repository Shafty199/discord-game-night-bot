import unittest
from pathlib import Path
from unittest.mock import patch

from ui.embeds import (
    _deduplicate_game_modes,
    _format_multiplayer_support,
    create_spin_embed,
)
from utils import igdb


class IGDBCacheCompletenessTests(unittest.TestCase):
    def test_complete_store_metadata_can_reuse_negative_cache(self):
        self.assertTrue(
            igdb._metadata_complete_with_cached_igdb(
                {
                    "genres": ["Action"],
                    "game_modes": ["Multiplayer"],
                    "max_players": 4,
                    "multiplayer_support": {
                        "online_multiplayer": True,
                        "online_max": 4,
                    },
                },
                None,
            )
        )

    def test_incomplete_store_metadata_bypasses_negative_cache(self):
        self.assertFalse(
            igdb._metadata_complete_with_cached_igdb(
                {
                    "name": "Sparse Game",
                    "store": "Steam",
                },
                None,
            )
        )

    def test_cached_igdb_data_can_complete_store_metadata(self):
        self.assertTrue(
            igdb._metadata_complete_with_cached_igdb(
                {
                    "name": "Party Game",
                    "store": "Steam",
                },
                {
                    "genres": ["Adventure"],
                    "game_modes": ["Co-operative"],
                    "max_players": 4,
                    "multiplayer_support": {
                        "online_coop": True,
                        "online_coop_max": 4,
                    },
                },
            )
        )


class IGDBPlayerSelectionTests(unittest.TestCase):
    def test_pc_online_coop_is_preferred(self):
        game = {
            "id": 42,
            "name": "Party Game",
            "platforms": [6],
            "game_modes": [1, 2, 3],
            "genres": [
                {"id": 10, "name": "Fighting"},
                {"id": 14, "name": "Sport"},
            ],
            "themes": [
                {"id": 27, "name": "Comedy"},
            ],
            "multiplayer_modes": [
                {
                    "platform": 48,
                    "onlinecoopmax": 8,
                    "onlinemax": 32,
                },
                {
                    "platform": 6,
                    "onlinecoopmax": 4,
                    "onlinemax": 16,
                    "offlinecoopmax": 6,
                    "campaigncoop": True,
                    "dropin": True,
                    "splitscreen": True,
                },
            ],
        }

        result = igdb._player_info_from_game(
            game,
            match_method="Steam ID",
        )

        self.assertEqual(result["max_players"], 4)
        self.assertEqual(
            result["player_mode"],
            "online co-op",
        )
        self.assertEqual(
            result["genres"],
            ["Fighting", "Sport"],
        )
        self.assertEqual(
            result["themes"],
            ["Comedy"],
        )
        self.assertEqual(
            result["game_modes"],
            [
                "Single player",
                "Multiplayer",
                "Co-operative",
            ],
        )
        self.assertEqual(
            result["multiplayer_support"],
            {
                "campaign_coop": True,
                "drop_in": True,
                "split_screen": True,
                "offline_coop_max": 6,
                "online_coop_max": 4,
                "online_max": 16,
                "platform": "PC",
            },
        )

    def test_explicit_single_player_mode_returns_one(self):
        result = igdb._player_info_from_game(
            {
                "id": 7,
                "name": "Solo Game",
                "platforms": [6],
                "game_modes": [1],
                "multiplayer_modes": [],
            },
            match_method="exact title",
        )

        self.assertEqual(result["max_players"], 1)
        self.assertEqual(
            result["player_mode"],
            "single-player",
        )

    def test_golf_it_is_classified_as_competitive(self):
        result = igdb._player_info_from_game(
            {
                "id": 571740,
                "name": "Golf It!",
                "platforms": [6],
                "game_modes": [1, 2, 3],
                "multiplayer_modes": [
                    {
                        "platform": 6,
                        "onlinecoop": True,
                        "onlinecoopmax": 30,
                    }
                ],
            },
            match_method="Steam ID",
        )

        self.assertEqual(result["max_players"], 30)
        self.assertEqual(
            result["player_mode"],
            "online multiplayer",
        )
        self.assertEqual(
            result["multiplayer_support"],
            {
                "online_max": 30,
                "platform": "PC",
            },
        )
        self.assertEqual(
            result["game_modes"],
            ["Single player", "Multiplayer"],
        )

    def test_bodycam_uses_ten_player_five_v_five_capacity(self):
        result = igdb._player_info_from_game(
            {
                "id": 2406770,
                "name": "Bodycam",
                "platforms": [6],
                "game_modes": [2, 3],
                "multiplayer_modes": [],
            },
            match_method="Steam ID",
        )

        self.assertEqual(result["max_players"], 10)
        self.assertEqual(
            result["player_mode"],
            "online multiplayer",
        )
        self.assertEqual(
            result["multiplayer_support"],
            {
                "online_multiplayer": True,
                "online_max": 10,
                "team_format": "5v5",
                "team_count": 2,
                "team_size": 5,
                "team_sizes": [5, 5],
                "team_total": 10,
                "platform": "PC",
            },
        )
        self.assertEqual(
            result["game_modes"],
            ["Multiplayer"],
        )

    def test_rust_gets_variable_capacity_mmo_support(self):
        result = igdb._player_info_from_game(
            {
                "id": 252490,
                "name": "Rust",
                "platforms": [6],
                "game_modes": [5],
                "multiplayer_modes": [],
            },
            match_method="Steam ID",
        )

        self.assertNotIn("max_players", result)
        self.assertEqual(
            result["multiplayer_support"],
            {
                "online_multiplayer": True,
                "mmo": True,
                "platform": "PC",
            },
        )
        self.assertIn(
            "Massively Multiplayer Online (MMO)",
            result["game_modes"],
        )
        self.assertIn(
            "Multiplayer",
            result["game_modes"],
        )

    def test_master_duel_uses_team_battle_capacity(self):
        result = igdb._player_info_from_game(
            {
                "id": 1449850,
                "name": "Yu-Gi-Oh! Master Duel",
                "platforms": [6],
                "game_modes": [1, 2],
                "multiplayer_modes": [],
            },
            match_method="Steam ID",
        )

        self.assertEqual(result["max_players"], 10)
        self.assertEqual(
            result["player_mode"],
            "online multiplayer",
        )
        self.assertEqual(
            result["multiplayer_support"],
            {
                "online_max": 10,
                "team_format": "5v5",
                "team_count": 2,
                "team_size": 5,
                "team_sizes": [5, 5],
                "team_total": 10,
                "platform": "PC",
            },
        )

    def test_mage_arena_uses_four_player_teams(self):
        result = igdb._player_info_from_game(
            {
                "id": 3716600,
                "name": "Mage Arena",
                "platforms": [6],
                "game_modes": [2, 3],
                "multiplayer_modes": [
                    {
                        "platform": 6,
                        "onlinecoop": True,
                    }
                ],
            },
            match_method="Steam ID",
        )

        self.assertEqual(result["max_players"], 4)
        self.assertEqual(
            result["player_mode"],
            "online co-op",
        )
        self.assertEqual(
            result["multiplayer_support"]["online_max"],
            8,
        )
        self.assertEqual(
            result["multiplayer_support"]["team_format"],
            "4v4",
        )

    def test_slackers_is_four_player_online_pvp(self):
        result = igdb._player_info_from_game(
            {
                "id": 2354000,
                "name": "Slackers - Carts of Glory",
                "platforms": [6],
                "game_modes": [1],
                "multiplayer_modes": [],
            },
            match_method="Steam ID",
        )

        self.assertEqual(result["max_players"], 4)
        self.assertEqual(
            result["player_mode"],
            "online multiplayer",
        )
        self.assertEqual(
            result["multiplayer_support"],
            {
                "online_multiplayer": True,
                "online_max": 4,
                "platform": "PC",
            },
        )
        self.assertIn(
            "Multiplayer",
            result["game_modes"],
        )

    def test_variable_capacity_online_multiplayer_is_formatted(self):
        support_text = _format_multiplayer_support(
            {
                "online_multiplayer": True,
                "mmo": True,
            }
        )

        self.assertIn("Online multiplayer", support_text)
        self.assertIn("MMO", support_text)
        self.assertNotIn("up to", support_text)

    def test_team_format_is_displayed_with_player_limits(self):
        support_text = _format_multiplayer_support(
            {
                "online_coop_max": 4,
                "online_max": 8,
                "team_format": "4v4",
            }
        )

        self.assertIn("up to 4 players", support_text)
        self.assertIn("up to 8 players", support_text)
        self.assertIn("Teams: **4v4**", support_text)

    def test_genres_promote_action_and_deprioritise_indie(self):
        result = igdb._player_info_from_game(
            {
                "id": 8,
                "name": "Action Indie Game",
                "platforms": [6],
                "game_modes": [2],
                "genres": [
                    {"name": "Indie"},
                    {"name": "Adventure"},
                    {"name": "Shooter"},
                    {"name": "Puzzle"},
                ],
                "themes": [
                    {"name": "Action"},
                ],
                "multiplayer_modes": [],
            },
            match_method="exact title",
        )

        self.assertEqual(
            result["genres"],
            ["Adventure", "Shooter", "Puzzle"],
        )
        self.assertNotIn("Indie", result["genres"])
        self.assertLessEqual(len(result["genres"]), 3)

        indie_only = igdb._preferred_genres(
            [{"name": "Indie"}],
            [{"name": "Action"}],
        )
        self.assertEqual(indie_only, ["Action"])

    def test_title_fallback_requires_exact_normalised_title(self):
        request = {
            "name": "The Finals",
        }

        self.assertIsNone(
            igdb._select_title_game(
                request,
                [
                    {
                        "id": 1,
                        "name": "Final Fantasy",
                        "platforms": [6],
                    }
                ],
            )
        )

        selected = igdb._select_title_game(
            request,
            [
                {
                    "id": 2,
                    "name": "The Finals™",
                    "platforms": [6],
                }
            ],
        )
        self.assertEqual(selected["id"], 2)


class IGDBBatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cache_path = (
            Path.cwd()
            / ".test-tmp"
            / "igdb-cache.json"
        )
        self.cache_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        self.cache_path.unlink(
            missing_ok=True
        )
        self.cache_path.with_suffix(
            ".json.tmp"
        ).unlink(
            missing_ok=True
        )
        igdb.clear_igdb_runtime_cache()

    async def asyncTearDown(self):
        igdb.clear_igdb_runtime_cache()
        self.cache_path.unlink(
            missing_ok=True
        )
        self.cache_path.with_suffix(
            ".json.tmp"
        ).unlink(
            missing_ok=True
        )

    async def test_direct_ids_are_batched_and_cached(self):
        calls = []

        async def fake_request(
            session,
            endpoint,
            query,
        ):
            calls.append((endpoint, query))

            if endpoint == "external_game_sources":
                return [
                    {"id": 100, "name": "Steam"},
                    {
                        "id": 200,
                        "name": "Epic Games Store",
                    },
                ]

            if endpoint == "external_games":
                return [
                    {
                        "uid": "12345",
                        "game": 77,
                    }
                ]

            if endpoint == "games":
                return [
                    {
                        "id": 77,
                        "name": "Batch Game",
                        "platforms": [6],
                        "game_modes": [2, 3],
                        "genres": [
                            {"name": "Indie"},
                        ],
                        "themes": [
                            {"name": "Party"},
                        ],
                        "multiplayer_modes": [
                            {
                                "platform": 6,
                                "onlinecoopmax": 4,
                                "onlinecoop": True,
                            }
                        ],
                    }
                ]

            self.fail(
                f"Unexpected IGDB endpoint: {endpoint}"
            )

        games = [
            {
                "name": "Batch Game",
                "store": "Steam",
                "external_id": "12345",
                "max_players": None,
            },
            {
                "name": "Batch Game",
                "store": "Steam",
                "external_id": "12345",
                "max_players": None,
            },
        ]

        with (
            patch.object(
                igdb,
                "IGDB_CLIENT_ID",
                "client-id",
            ),
            patch.object(
                igdb,
                "IGDB_CLIENT_SECRET",
                "client-secret",
            ),
            patch.object(
                igdb,
                "IGDB_CACHE_PATH",
                self.cache_path,
            ),
            patch.object(
                igdb,
                "_request_json",
                side_effect=fake_request,
            ),
        ):
            enriched = (
                await igdb.enrich_missing_player_metadata(
                    object(),
                    games,
                    force_refresh=True,
                )
            )

            call_count_after_refresh = len(calls)

            cached_game = {
                "name": "Batch Game",
                "store": "Steam",
                "external_id": "12345",
                "max_players": None,
            }
            cached_enriched = (
                await igdb.enrich_missing_player_metadata(
                    object(),
                    [cached_game],
                )
            )

        self.assertEqual(enriched, 2)
        self.assertEqual(cached_enriched, 1)
        self.assertEqual(games[0]["max_players"], 4)
        self.assertEqual(
            games[0]["max_players_source"],
            "IGDB",
        )
        self.assertEqual(games[0]["igdb_id"], 77)
        self.assertEqual(games[0]["genres"], ["Party"])
        self.assertEqual(
            games[0]["themes"],
            ["Party"],
        )
        self.assertEqual(
            games[0]["game_modes"],
            ["Multiplayer", "Co-operative"],
        )
        self.assertEqual(
            games[0]["multiplayer_support"],
            {
                "online_coop": True,
                "online_coop_max": 4,
                "platform": "PC",
            },
        )
        self.assertEqual(cached_game["max_players"], 4)
        self.assertEqual(
            len(calls),
            call_count_after_refresh,
        )
        self.assertEqual(
            [endpoint for endpoint, _ in calls],
            [
                "external_game_sources",
                "external_games",
                "games",
            ],
        )


class IGDBWinnerEmbedTests(unittest.TestCase):
    def test_matching_coop_and_multiplayer_limits_are_collapsed(self):
        support_text = _format_multiplayer_support(
            {
                "online_coop": True,
                "online_coop_max": 4,
                "online_max": 4,
            },
            max_players=4,
        )

        self.assertIn("Online co-op", support_text)
        self.assertNotIn(
            "Online multiplayer",
            support_text,
        )
        self.assertEqual(
            support_text.count("up to 4 players"),
            1,
        )
        self.assertEqual(
            _deduplicate_game_modes(
                [
                    "Single player",
                    "Multiplayer",
                    "Co-operative",
                ],
                {
                    "online_coop_max": 4,
                    "online_max": 4,
                },
            ),
            [
                "Single player",
                "Co-operative",
            ],
        )

    def test_different_coop_and_multiplayer_limits_are_kept(self):
        support_text = _format_multiplayer_support(
            {
                "online_coop": True,
                "online_coop_max": 4,
                "online_max": 12,
            },
            max_players=12,
        )

        self.assertIn("Online co-op", support_text)
        self.assertIn(
            "Online multiplayer",
            support_text,
        )
        self.assertIn("up to 4 players", support_text)
        self.assertIn("up to 12 players", support_text)
        self.assertEqual(
            _deduplicate_game_modes(
                ["Multiplayer", "Co-operative"],
                {
                    "online_coop_max": 4,
                    "online_max": 12,
                },
            ),
            ["Multiplayer", "Co-operative"],
        )

    def test_winner_embed_displays_saved_metadata(self):
        game = (
            1,
            "Party Game",
            "https://store.steampowered.com/app/123/",
            "Steam",
            "Tester",
            0,
            None,
            "https://example.com/art.jpg",
            4,
            "https://example.com/art.jpg",
            77,
            (
                '{"campaign_coop":true,'
                '"online_coop":true,'
                '"online_coop_max":4,'
                '"platform":"PC"}'
            ),
            '["Adventure","Action","Puzzle"]',
            '["Comedy"]',
            '["Multiplayer","Co-operative"]',
        )

        embed = create_spin_embed(game)
        fields = {
            field.name: field.value
            for field in embed.fields
        }

        self.assertIn("🏷️ Genres", fields)
        self.assertIn("Adventure", fields["🏷️ Genres"])
        self.assertNotIn("Themes:", fields["🏷️ Genres"])
        self.assertIn("🎮 Game Modes", fields)
        self.assertIn(
            "Co-operative",
            fields["🎮 Game Modes"],
        )
        self.assertIn("🤝 Multiplayer Support", fields)
        self.assertNotIn("👥 Players", fields)
        self.assertIn(
            "up to 4",
            fields["🤝 Multiplayer Support"],
        )

    def test_winner_embed_falls_back_to_player_count(self):
        game = (
            1,
            "Fallback Game",
            "https://store.steampowered.com/app/456/",
            "Steam",
            "Tester",
            0,
            None,
            "https://example.com/art.jpg",
            6,
            "https://example.com/art.jpg",
            88,
            "{}",
            "[]",
            "[]",
            '["Multiplayer"]',
        )

        embed = create_spin_embed(game)
        fields = {
            field.name: field.value
            for field in embed.fields
        }

        self.assertNotIn("👥 Players", fields)
        self.assertIn("🤝 Multiplayer Support", fields)
        self.assertEqual(
            fields["🤝 Multiplayer Support"],
            "👥 Player limit: **up to 6 players**",
        )


if __name__ == "__main__":
    unittest.main()
