import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from utils import epic
from utils import steamgriddb


class FakeResponse:
    def __init__(
        self,
        payload,
        *,
        status=200,
    ):
        self.payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(
        self,
        exc_type,
        exc_value,
        traceback,
    ):
        return False

    async def json(
        self,
        *,
        content_type=None,
    ):
        return self.payload


class FakeSession:
    def __init__(self):
        self.calls = []

    def get(
        self,
        url,
        **kwargs,
    ):
        self.calls.append(
            (url, kwargs)
        )

        if "/search/autocomplete/" in url:
            data = [
                {
                    "id": 999,
                    "name": "Fortnite Festival",
                    "verified": True,
                },
                {
                    "id": 36136,
                    "name": "Fortnite",
                    "verified": True,
                },
            ]

        elif "/grids/game/36136" in url:
            data = [
                {
                    "url": (
                        "https://cdn2.steamgriddb.com/"
                        "grid/fortnite.png"
                    ),
                    "width": 920,
                    "height": 430,
                    "upvotes": 50,
                    "downvotes": 1,
                }
            ]

        elif "/heroes/game/36136" in url:
            data = [
                {
                    "url": (
                        "https://cdn2.steamgriddb.com/"
                        "hero/fortnite.png"
                    ),
                    "width": 1920,
                    "height": 620,
                    "upvotes": 100,
                    "downvotes": 0,
                }
            ]

        else:
            data = []

        return FakeResponse(
            {
                "success": True,
                "data": data,
            }
        )


class SteamGridDBSelectionTests(
    unittest.TestCase
):
    def test_persistent_cache_loads_successes_and_misses(self):
        cache_path = MagicMock()
        cache_path.read_text.return_value = json.dumps(
            {
                "version": 1,
                "entries": {
                    "fortnite": {
                        "url": (
                            "https://cdn2.steamgriddb.com/"
                            "grid/fortnite.png"
                        ),
                        "expires_at": (
                            steamgriddb.time() + 1000
                        ),
                    },
                    "missing game": {
                        "url": None,
                        "expires_at": (
                            steamgriddb.time() + 1000
                        ),
                    },
                },
            }
        )

        with patch.object(
            steamgriddb,
            "STEAMGRIDDB_CACHE_PATH",
            cache_path,
        ):
            loaded = steamgriddb._read_cache_file()

        self.assertEqual(
            loaded["fortnite"][1],
            (
                "https://cdn2.steamgriddb.com/"
                "grid/fortnite.png"
            ),
        )
        self.assertIsNone(
            loaded["missing game"][1]
        )

    def test_exact_title_wins_over_related_title(self):
        selected = steamgriddb._select_game_match(
            "Fortnite",
            [
                {
                    "id": 999,
                    "name": "Fortnite Festival",
                    "verified": True,
                },
                {
                    "id": 36136,
                    "name": "FORTNITE!",
                    "verified": True,
                },
            ],
        )

        self.assertEqual(
            selected["id"],
            36136,
        )

    def test_related_title_is_not_accepted(self):
        selected = steamgriddb._select_game_match(
            "Fortnite",
            [
                {
                    "id": 999,
                    "name": "Fortnite Festival",
                    "verified": True,
                }
            ],
        )

        self.assertIsNone(
            selected
        )

    def test_artwork_prefers_safe_landscape_image(self):
        selected = steamgriddb._select_artwork(
            (
                (
                    "grid",
                    [
                        {
                            "url": (
                                "https://cdn2.steamgriddb.com/"
                                "grid/unsafe.png"
                            ),
                            "width": 920,
                            "height": 430,
                            "nsfw": True,
                        },
                        {
                            "url": (
                                "https://cdn2.steamgriddb.com/"
                                "grid/good.png"
                            ),
                            "width": 920,
                            "height": 430,
                            "nsfw": "false",
                        },
                        {
                            "url": (
                                "https://untrusted.example/"
                                "grid/bad.png"
                            ),
                            "width": 592,
                            "height": 340,
                        },
                    ],
                ),
                (
                    "hero",
                    [
                        {
                            "url": (
                                "https://cdn2.steamgriddb.com/"
                                "hero/wide.png"
                            ),
                            "width": 1920,
                            "height": 620,
                            "upvotes": 500,
                        }
                    ],
                ),
            )
        )

        self.assertEqual(
            selected,
            (
                "https://cdn2.steamgriddb.com/"
                "grid/good.png"
            ),
        )


class SteamGridDBRequestTests(
    unittest.IsolatedAsyncioTestCase
):
    async def asyncSetUp(self):
        steamgriddb._artwork_cache.clear()
        steamgriddb._inflight_requests.clear()
        steamgriddb._cache_loaded = True
        self.persist_patcher = patch.object(
            steamgriddb,
            "_persist_cache",
            AsyncMock(),
        )
        self.persist_patcher.start()
        self.addCleanup(
            self.persist_patcher.stop
        )

    async def test_lookup_uses_bearer_auth_and_returns_grid(self):
        session = FakeSession()

        with patch.object(
            steamgriddb,
            "STEAMGRIDDB_API_KEY",
            "test-key",
        ):
            artwork_url = (
                await steamgriddb.get_steamgriddb_artwork(
                    session,
                    "Fortnite",
                )
            )

        self.assertEqual(
            artwork_url,
            (
                "https://cdn2.steamgriddb.com/"
                "grid/fortnite.png"
            ),
        )
        self.assertEqual(
            len(session.calls),
            3,
        )

        for _, kwargs in session.calls:
            self.assertEqual(
                kwargs["headers"]["Authorization"],
                "Bearer test-key",
            )

    async def test_concurrent_lookups_share_requests(self):
        session = FakeSession()

        with patch.object(
            steamgriddb,
            "STEAMGRIDDB_API_KEY",
            "test-key",
        ):
            first, second = await asyncio.gather(
                steamgriddb.get_steamgriddb_artwork(
                    session,
                    "Fortnite",
                ),
                steamgriddb.get_steamgriddb_artwork(
                    session,
                    "Fortnite",
                ),
            )

        self.assertEqual(
            first,
            second,
        )
        self.assertEqual(
            len(session.calls),
            3,
        )

    async def test_no_match_is_temporarily_cached(self):
        session = object()

        with (
            patch.object(
                steamgriddb,
                "STEAMGRIDDB_API_KEY",
                "test-key",
            ),
            patch.object(
                steamgriddb,
                "_request_data",
                AsyncMock(return_value=[]),
            ) as request_data,
        ):
            first = await steamgriddb.get_steamgriddb_artwork(
                session,
                "Missing Game",
            )
            second = await steamgriddb.get_steamgriddb_artwork(
                session,
                "Missing Game",
            )

        self.assertIsNone(first)
        self.assertIsNone(second)
        request_data.assert_awaited_once()

    async def test_epic_block_uses_steamgriddb_fallback(self):
        blocked_result = {
            "requested_url": "https://example.invalid",
            "final_url": None,
            "http_status": 403,
            "page_html": None,
            "error": None,
        }

        with (
            patch.object(
                epic,
                "_fetch_epic_page",
                AsyncMock(
                    return_value=blocked_result
                ),
            ),
            patch.object(
                epic,
                "get_steamgriddb_artwork",
                AsyncMock(
                    return_value=(
                        "https://cdn2.steamgriddb.com/"
                        "grid/fortnite.png"
                    )
                ),
            ) as fallback,
        ):
            result = await epic.get_epic_game_info(
                object(),
                (
                    "https://store.epicgames.com/"
                    "en-US/p/fortnite"
                ),
            )

        self.assertEqual(
            result["image_url"],
            (
                "https://cdn2.steamgriddb.com/"
                "grid/fortnite.png"
            ),
        )
        self.assertEqual(
            result["verification_status"],
            "complete",
        )
        self.assertEqual(
            result["link_status"],
            "live",
        )
        self.assertEqual(result["max_players"], 100)
        self.assertEqual(
            result["multiplayer_support"]["team_size"],
            4,
        )
        self.assertEqual(
            result["genres"],
            ["Shooter", "Battle Royale", "Action"],
        )
        fallback.assert_awaited_once_with(
            unittest.mock.ANY,
            "Fortnite",
        )

    async def test_epic_block_without_artwork_stays_incomplete(self):
        blocked_result = {
            "requested_url": "https://example.invalid",
            "final_url": None,
            "http_status": 403,
            "page_html": None,
            "error": None,
        }

        with (
            patch.object(
                epic,
                "_fetch_epic_page",
                AsyncMock(
                    return_value=blocked_result
                ),
            ),
            patch.object(
                epic,
                "get_steamgriddb_artwork",
                AsyncMock(return_value=None),
            ),
        ):
            result = await epic.get_epic_game_info(
                object(),
                (
                    "https://store.epicgames.com/"
                    "en-US/p/unknown-game"
                ),
            )

        self.assertEqual(
            result["verification_status"],
            "blocked",
        )
        self.assertEqual(
            result["link_status"],
            "blocked",
        )


if __name__ == "__main__":
    unittest.main()
