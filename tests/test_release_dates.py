import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from utils import store


class FakeResponse:
    def __init__(self, payload, *, status=200):
        self.payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return False

    async def json(self, *, content_type=None):
        await asyncio.sleep(0)
        return self.payload


class SteamReleaseDateTests(unittest.IsolatedAsyncioTestCase):
    def test_unix_timestamp_uses_fixed_gmt_plus_10(self):
        self.assertEqual(
            store._release_date_from_steam_timestamp(1786986000),
            "18 Aug, 2026",
        )

    async def test_store_browse_release_timestamp_is_parsed(self):
        session = SimpleNamespace(
            get=MagicMock(
                return_value=FakeResponse(
                    {
                        "response": {
                            "store_items": [
                                {
                                    "appid": 4796830,
                                    "release": {
                                        "steam_release_date": 1786986000,
                                        "is_coming_soon": True,
                                    },
                                }
                            ]
                        }
                    }
                )
            )
        )

        self.assertEqual(
            await store.fetch_steam_release_info(
                session,
                "4796830",
            ),
            {
                "release_date": "18 Aug, 2026",
                "coming_soon": True,
            },
        )

    async def test_timestamp_date_beats_steam_server_text(self):
        fetch_page = AsyncMock(
            return_value={
                "final_url": (
                    "https://store.steampowered.com/app/4796830/"
                ),
                "http_status": 200,
                "page_html": (
                    '<meta property="og:title" '
                    'content="WE ARE SO DEAD on Steam">'
                    "Planned Release Date: 17 Aug, 2026 Interested? "
                    "This game is not yet available on Steam"
                ),
                "error": None,
            }
        )
        fetch_details = AsyncMock(
            return_value={
                "name": "WE ARE SO DEAD",
                "image_url": None,
                "price_info": None,
                "max_players": 8,
                "multiplayer_support": {},
                "genres": [],
                "game_modes": [],
                "coming_soon": True,
                "release_date": "17 Aug, 2026",
                "availability_status": "coming_soon",
                "availability_verified": True,
            }
        )
        fetch_release = AsyncMock(
            return_value={
                "release_date": "18 Aug, 2026",
                "coming_soon": True,
            }
        )

        with (
            patch.object(store, "fetch_store_page", fetch_page),
            patch.object(
                store,
                "fetch_steam_app_details",
                fetch_details,
            ),
            patch.object(
                store,
                "fetch_steam_release_info",
                fetch_release,
            ),
        ):
            result = await store._fetch_game_info_from_url(
                object(),
                "https://store.steampowered.com/app/4796830/",
                force_refresh=True,
            )

        self.assertEqual(
            result["release_date"],
            "18 Aug, 2026",
        )


if __name__ == "__main__":
    unittest.main()
