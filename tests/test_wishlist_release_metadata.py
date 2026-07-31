import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from commands import wishlist


class WishlistReleaseMetadataTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_release_check_forces_fresh_store_metadata(self):
        lookup = AsyncMock(
            return_value={"name": "Upcoming Game"}
        )

        with patch.object(
            wishlist,
            "get_game_info_from_url",
            lookup,
        ):
            result = await wishlist._prefetch_wishlist_game(
                session=object(),
                store_link=(
                    "https://store.steampowered.com/app/456/"
                ),
                steam_semaphore=asyncio.Semaphore(1),
                epic_semaphore=asyncio.Semaphore(1),
            )

        self.assertIsNone(result["error"])
        lookup.assert_awaited_once_with(
            unittest.mock.ANY,
            (
                "https://store.steampowered.com/app/456/"
            ),
            force_refresh=True,
        )

    async def test_new_release_is_enriched_before_sync(self):
        game_info = {
            "name": "New Release",
            "store": "Steam",
            "store_link": (
                "https://store.steampowered.com/app/123/"
            ),
            "external_id": "123",
            "availability_verified": True,
            "availability_status": "released",
            "coming_soon": False,
            "link_status": "live",
        }

        async def enrich(_session, games):
            self.assertEqual(games, [game_info])
            game_info.update(
                {
                    "igdb_id": 77,
                    "max_players": 4,
                    "max_players_source": "IGDB",
                    "multiplayer_support": {
                        "online_coop": True,
                        "online_coop_max": 4,
                    },
                    "genres": ["Adventure"],
                    "themes": ["Comedy"],
                    "game_modes": ["Co-operative"],
                }
            )
            return 1

        bot = SimpleNamespace(http_session=object())
        cog = wishlist.Wishlist.__new__(
            wishlist.Wishlist
        )
        cog.bot = bot
        cog._announce_releases = AsyncMock()
        sync = AsyncMock(return_value="updated")

        with (
            patch.object(
                wishlist,
                "get_wishlist_games",
                AsyncMock(
                    return_value=[
                        (
                            1,
                            "New Release",
                            game_info["store_link"],
                            "Steam",
                            "Tester",
                            None,
                            None,
                            None,
                        )
                    ]
                ),
            ),
            patch.object(
                wishlist,
                "_prefetch_wishlist_game",
                AsyncMock(
                    return_value={
                        "game_info": game_info,
                        "error": None,
                    }
                ),
            ),
            patch.object(
                wishlist,
                "enrich_missing_player_metadata",
                side_effect=enrich,
            ) as enrich_mock,
            patch.object(
                wishlist,
                "sync_game",
                sync,
            ),
        ):
            await cog._run_wishlist_release_check()

        enrich_mock.assert_awaited_once()
        synced = sync.await_args.kwargs
        self.assertEqual(synced["igdb_id"], 77)
        self.assertEqual(synced["max_players"], 4)
        self.assertEqual(
            synced["multiplayer_support"][
                "online_coop_max"
            ],
            4,
        )
        self.assertEqual(
            synced["genres"],
            ["Adventure"],
        )
        self.assertEqual(
            synced["game_modes"],
            ["Co-operative"],
        )


if __name__ == "__main__":
    unittest.main()
