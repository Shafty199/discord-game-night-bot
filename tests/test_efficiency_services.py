import asyncio
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from utils.metadata import (
    reconcile_suggestion_metadata,
    release_info_from_embeds,
)
from utils import steam_api
from utils import spin_runtime
from utils import artwork_cache
from utils import store


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
        await asyncio.sleep(0.01)
        return self.payload


class FakeSteamSession:
    def __init__(
        self,
        *,
        status=200,
    ):
        self.calls = 0
        self.status = status

    def get(
        self,
        url,
        **kwargs,
    ):
        self.calls += 1
        app_id = kwargs["params"]["appids"]

        return FakeResponse(
            {
                app_id: {
                    "success": True,
                    "data": {
                        "name": "Cached Game",
                        "type": "game",
                    },
                }
            },
            status=self.status,
        )


class SteamAPICacheTests(
    unittest.IsolatedAsyncioTestCase
):
    async def asyncSetUp(self):
        steam_api.clear_steam_details_cache()
        steam_api._inflight_requests.clear()

    async def test_concurrent_requests_share_one_http_call(self):
        session = FakeSteamSession()

        first, second = await asyncio.gather(
            steam_api.fetch_steam_app_data(
                session,
                "12345",
            ),
            steam_api.fetch_steam_app_data(
                session,
                "12345",
            ),
        )

        self.assertEqual(
            session.calls,
            1,
        )
        self.assertEqual(
            first,
            second,
        )

        cached = await steam_api.fetch_steam_app_data(
            session,
            "12345",
        )

        self.assertEqual(
            session.calls,
            1,
        )
        self.assertEqual(
            cached["name"],
            "Cached Game",
        )

    async def test_failed_requests_are_not_cached(self):
        session = FakeSteamSession(
            status=503
        )

        self.assertIsNone(
            await steam_api.fetch_steam_app_data(
                session,
                "54321",
            )
        )
        await asyncio.sleep(0)

        self.assertIsNone(
            await steam_api.fetch_steam_app_data(
                session,
                "54321",
            )
        )

        self.assertEqual(
            session.calls,
            2,
        )

    async def test_force_refresh_bypasses_raw_steam_cache(self):
        session = FakeSteamSession()

        await steam_api.fetch_steam_app_data(
            session,
            "24680",
        )
        await steam_api.fetch_steam_app_data(
            session,
            "24680",
            force_refresh=True,
        )

        self.assertEqual(session.calls, 2)


class SpinRuntimeTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_sale_lookup_runs_during_animation(self):
        animation_started = asyncio.Event()
        sale_started = asyncio.Event()

        async def animation():
            animation_started.set()
            await asyncio.wait_for(
                sale_started.wait(),
                timeout=1,
            )

        async def sale_lookup(**kwargs):
            await animation_started.wait()
            sale_started.set()
            return {
                "is_on_sale": True,
            }

        game = (
            1,
            "Test Game",
            (
                "https://store.steampowered.com/"
                "app/12345/"
            ),
            "Steam",
        )

        with patch.object(
            spin_runtime,
            "get_steam_sale_info",
            side_effect=sale_lookup,
        ):
            result = await asyncio.wait_for(
                spin_runtime.animate_with_sale_lookup(
                    animation(),
                    session=object(),
                    game=game,
                ),
                timeout=1,
            )

        self.assertTrue(
            result["is_on_sale"]
        )


class ArtworkLifecycleTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_manifest_card_filename_avoids_directory_scan(self):
        card_directory = MagicMock()
        card_path = MagicMock()
        card_path.stat.return_value.st_size = 120
        card_directory.__truediv__.return_value = card_path
        fallback_lookup = MagicMock()

        with (
            patch.object(
                artwork_cache,
                "STATIC_CARD_CACHE_DIRECTORY",
                card_directory,
            ),
            patch.object(
                artwork_cache,
                "_get_manifest",
                AsyncMock(
                    return_value={
                        "games": {
                            "7": {
                                "card_filename": (
                                    "game-7-abcd1234.jpg"
                                ),
                                "file_size": 120,
                            }
                        }
                    }
                ),
            ),
            patch.object(
                artwork_cache,
                "get_static_game_card_path",
                fallback_lookup,
            ),
        ):
            result = (
                await artwork_cache
                .get_cached_game_card_path(7)
            )

        self.assertIs(result, card_path)
        fallback_lookup.assert_not_called()

    def test_version_one_manifest_is_migrated_in_memory(self):
        manifest_path = MagicMock()
        manifest_path.read_text.return_value = (
            '{"version":1,"games":{"1":{}}}'
        )

        with patch.object(
            artwork_cache,
            "ARTWORK_MANIFEST_PATH",
            manifest_path,
        ):
            manifest = artwork_cache._read_manifest()

        self.assertEqual(
            manifest["version"],
            artwork_cache.ARTWORK_MANIFEST_VERSION,
        )
        self.assertIn("1", manifest["games"])

    async def test_existing_local_artwork_is_reused(self):
        artwork_path = MagicMock()
        artwork_path.is_file.return_value = True
        artwork_path.stat.return_value.st_size = 100
        card_path = MagicMock()
        card_path.is_file.return_value = True
        card_path.stat.return_value.st_size = 100
        download = AsyncMock()

        with (
            patch.object(
                artwork_cache,
                "get_local_artwork_path",
                return_value=artwork_path,
            ),
            patch.object(
                artwork_cache,
                "_download_artwork",
                download,
            ),
            patch.object(
                artwork_cache,
                "get_static_game_card_path",
                return_value=card_path,
            ),
            patch.object(
                artwork_cache,
                "_is_valid_artwork_file",
                AsyncMock(return_value=True),
            ),
            patch.object(
                artwork_cache,
                "_get_manifest",
                AsyncMock(
                    return_value={
                        "games": {
                            "1": {
                                "source_url": (
                                    "https://example.com/art.jpg"
                                )
                            }
                        }
                    }
                ),
            ),
        ):
            result = await artwork_cache.prepare_local_game_artwork(
                bot=SimpleNamespace(
                    http_session=MagicMock()
                ),
                game_record={
                    "id": 1,
                    "name": "Test Game",
                    "image_url": "https://example.com/art.jpg",
                },
            )

        self.assertEqual(result, "already_cached")
        download.assert_not_awaited()
        artwork_path.unlink.assert_called_once_with(
            missing_ok=True
        )

    async def test_missing_artwork_is_downloaded_locally(self):
        bot = SimpleNamespace(
            http_session=MagicMock()
        )
        artwork_path = MagicMock()
        card_path = MagicMock()

        with (
            patch.object(
                artwork_cache,
                "get_local_artwork_path",
                return_value=artwork_path,
            ),
            patch.object(
                artwork_cache,
                "get_static_game_card_path",
                return_value=None,
            ),
            patch.object(
                artwork_cache,
                "_get_manifest",
                AsyncMock(return_value={"games": {}}),
            ),
            patch.object(
                artwork_cache,
                "_is_valid_artwork_file",
                AsyncMock(side_effect=(False, True)),
            ),
            patch.object(
                artwork_cache,
                "_download_artwork",
                AsyncMock(
                    return_value=b"image-data"
                ),
            ),
            patch.object(
                artwork_cache,
                "ensure_local_artwork",
                AsyncMock(
                    return_value=artwork_path
                ),
            ),
            patch.object(
                artwork_cache,
                "prepare_static_game_card",
                return_value=card_path,
            ),
            patch.object(
                artwork_cache,
                "_record_manifest_entry",
                AsyncMock(),
            ),
        ):
            result = await artwork_cache.prepare_local_game_artwork(
                bot=bot,
                game_record={
                    "id": 1,
                    "name": "Local Game",
                    "image_url": (
                        "https://example.com/art.jpg"
                    ),
                },
            )

        self.assertEqual(result, "cached")
        artwork_path.unlink.assert_called_once_with(
            missing_ok=True
        )

    async def test_refresh_redownloads_existing_artwork(self):
        bot = SimpleNamespace(
            http_session=MagicMock()
        )
        artwork_path = MagicMock()
        artwork_path.is_file.return_value = True
        artwork_path.stat.return_value.st_size = 100
        card_path = MagicMock()
        card_path.is_file.return_value = True
        card_path.stat.return_value.st_size = 100
        download = AsyncMock(return_value=b"new-image-data")
        ensure = AsyncMock(return_value=artwork_path)

        with (
            patch.object(
                artwork_cache,
                "get_local_artwork_path",
                return_value=artwork_path,
            ),
            patch.object(
                artwork_cache,
                "_download_artwork",
                download,
            ),
            patch.object(
                artwork_cache,
                "get_static_game_card_path",
                return_value=card_path,
            ),
            patch.object(
                artwork_cache,
                "_get_manifest",
                AsyncMock(return_value={"games": {}}),
            ),
            patch.object(
                artwork_cache,
                "_is_valid_artwork_file",
                AsyncMock(return_value=True),
            ),
            patch.object(
                artwork_cache,
                "ensure_local_artwork",
                ensure,
            ),
            patch.object(
                artwork_cache,
                "prepare_static_game_card",
                return_value=card_path,
            ),
            patch.object(
                artwork_cache,
                "_record_manifest_entry",
                AsyncMock(),
            ),
        ):
            result = await artwork_cache.prepare_local_game_artwork(
                bot=bot,
                game_record={
                    "id": 1,
                    "name": "Refresh Game",
                    "image_url": "https://example.com/art.jpg",
                },
                refresh=True,
            )

        self.assertEqual(result, "cached")
        download.assert_awaited_once()
        ensure.assert_awaited_once_with(
            session=bot.http_session,
            game_id=1,
            image_url="https://example.com/art.jpg",
            image_data=b"new-image-data",
        )

    async def test_delete_removes_local_artwork(self):
        artwork_path = MagicMock()
        artwork_path.is_file.return_value = True
        card_path = MagicMock()
        card_path.is_file.return_value = True
        card_directory = MagicMock()
        card_directory.glob.return_value = [card_path]

        with (
            patch.object(
                artwork_cache,
                "get_local_artwork_path",
                return_value=artwork_path,
            ),
            patch.object(
                artwork_cache,
                "STATIC_CARD_CACHE_DIRECTORY",
                card_directory,
            ),
            patch.object(
                artwork_cache,
                "_remove_manifest_entries",
                AsyncMock(),
            ),
        ):
            result = await artwork_cache.delete_local_game_artwork(
                42
            )

        artwork_path.unlink.assert_called_once_with(
            missing_ok=True
        )
        card_path.unlink.assert_called_once_with(
            missing_ok=True
        )
        self.assertTrue(result)


class MetadataRequestCoordinatorTests(
    unittest.IsolatedAsyncioTestCase
):
    async def asyncSetUp(self):
        store._metadata_cache.clear()
        store._metadata_inflight.clear()

    async def test_concurrent_and_cached_lookups_share_work(self):
        calls = 0

        async def fetch(
            session,
            url,
            *,
            force_refresh=False,
        ):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return {
                "name": "Shared Game",
                "values": [],
            }

        with patch.object(
            store,
            "_fetch_game_info_from_url",
            side_effect=fetch,
        ):
            first, second = await asyncio.gather(
                store.get_game_info_from_url(
                    object(),
                    "https://store.steampowered.com/app/1234/",
                ),
                store.get_game_info_from_url(
                    object(),
                    "https://store.steampowered.com/app/1234/",
                ),
            )
            first["values"].append("changed")
            cached = await store.get_game_info_from_url(
                object(),
                "https://store.steampowered.com/app/1234/",
            )

        self.assertEqual(calls, 1)
        self.assertEqual(second["values"], [])
        self.assertEqual(cached["values"], [])

    async def test_force_refresh_bypasses_cached_result(self):
        calls = 0

        refresh_values = []

        async def fetch(
            session,
            url,
            *,
            force_refresh=False,
        ):
            nonlocal calls
            calls += 1
            refresh_values.append(force_refresh)
            return {"name": f"Result {calls}"}

        with patch.object(
            store,
            "_fetch_game_info_from_url",
            side_effect=fetch,
        ):
            first = await store.get_game_info_from_url(
                object(),
                "https://store.steampowered.com/app/4321/",
            )
            refreshed = await store.get_game_info_from_url(
                object(),
                "https://store.steampowered.com/app/4321/",
                force_refresh=True,
            )

        self.assertEqual(calls, 2)
        self.assertEqual(
            refresh_values,
            [False, True],
        )
        self.assertEqual(first["name"], "Result 1")
        self.assertEqual(refreshed["name"], "Result 2")

    async def test_verified_storefront_date_beats_stale_api_date(self):
        page_html = """
            <meta property="og:title" content="Date Game on Steam">
            Planned Release Date: December 2099 Interested?
            This game is not yet available on Steam
        """
        fetch_page = AsyncMock(
            return_value={
                "final_url": (
                    "https://store.steampowered.com/app/9876/"
                ),
                "http_status": 200,
                "page_html": page_html,
                "error": None,
            }
        )
        fetch_details = AsyncMock(
            return_value={
                "name": "Date Game",
                "image_url": None,
                "price_info": None,
                "max_players": None,
                "multiplayer_support": {},
                "genres": [],
                "game_modes": [],
                "coming_soon": True,
                "release_date": "November 2099",
                "availability_status": "coming_soon",
                "availability_verified": True,
            }
        )
        fetch_release = AsyncMock(
            return_value=None
        )

        with (
            patch.object(
                store,
                "fetch_store_page",
                fetch_page,
            ),
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
                "https://store.steampowered.com/app/9876/",
                force_refresh=True,
            )

        self.assertEqual(
            result["release_date"],
            "December 2099",
        )
        fetch_details.assert_awaited_once_with(
            unittest.mock.ANY,
            "9876",
            force_refresh=True,
        )
        fetch_release.assert_awaited_once_with(
            unittest.mock.ANY,
            "9876",
        )


class SteamReleaseDateNormalisationTests(
    unittest.TestCase
):
    def test_hour_countdown_rolls_into_gmt_plus_10_next_day(self):
        result = store._normalise_steam_release_date(
            (
                "30 Jul, 2026 This game plans to unlock "
                "in approximately 11 hours"
            ),
            now=datetime(
                2026,
                7,
                30,
                16,
                39,
                tzinfo=ZoneInfo(
                    "Australia/Sydney"
                ),
            ),
        )

        self.assertEqual(result, "31 Jul, 2026")

    def test_unix_release_timestamp_uses_gmt_plus_10_date(self):
        self.assertEqual(
            store._release_date_from_steam_timestamp(
                1786986000
            ),
            "18 Aug, 2026",
        )

    def test_approximate_weeks_are_removed_not_recalculated(self):
        result = store._normalise_steam_release_date(
            (
                "17 Aug, 2026 This game plans to unlock "
                "in approximately 2 weeks"
            )
        )

        self.assertEqual(result, "17 Aug, 2026")

    def test_vague_release_window_is_unchanged(self):
        self.assertEqual(
            store._normalise_steam_release_date(
                "September 2026"
            ),
            "September 2026",
        )


class SteamReleaseInfoTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_store_browse_timestamp_becomes_gmt_plus_10_date(self):
        session = SimpleNamespace(
            get=MagicMock(
                return_value=FakeResponse(
                    {
                        "response": {
                            "store_items": [
                                {
                                    "appid": 4796830,
                                    "is_coming_soon": True,
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

        result = await store.fetch_steam_release_info(
            session,
            "4796830",
        )

        self.assertEqual(
            result,
            {
                "release_date": "18 Aug, 2026",
                "coming_soon": True,
            },
        )


class SteamPlayerCountParsingTests(
    unittest.TestCase
):
    def test_plus_range_uses_advertised_group_size(self):
        max_players = store._extract_max_players(
            {
                "short_description": (
                    "A co-op game for 1-6+ players."
                ),
                "about_the_game": (
                    "Online co-op. Up to 250 players, "
                    "although 4 is safe and 8 is rough."
                ),
            }
        )

        self.assertEqual(max_players, 6)

    def test_unicode_range_separator_is_supported(self):
        max_players = store._extract_max_players(
            {
                "short_description": (
                    "Built for 1–8+ players online."
                )
            }
        )

        self.assertEqual(max_players, 8)

    def test_team_range_is_recognised(self):
        max_players = store._extract_max_players(
            {
                "steam_appid": 3059070,
                "short_description": (
                    "Co-op horror where you play in teams "
                    "of 1 to 8 reckless journalists."
                ),
            }
        )

        self.assertEqual(max_players, 8)

    def test_hyphenated_to_range_is_recognised(self):
        max_players = store._extract_max_players(
            {
                "steam_appid": 1272080,
                "short_description": (
                    "PAYDAY 3 is a 1-to-4 player FPS."
                ),
            }
        )

        self.assertEqual(max_players, 4)

    def test_mage_arena_team_format_sets_total_limit(self):
        app_data = {
            "steam_appid": 3716600,
            "about_the_game": (
                "Choose your side in an up to 4v4 voice "
                "controlled battle or challenge a rival 1v1."
            ),
            "categories": [
                {"description": "Online PvP"},
                {"description": "Online Co-op"},
            ],
        }

        self.assertEqual(
            store._extract_max_players(app_data),
            8,
        )
        self.assertEqual(
            store._extract_team_support(app_data),
            {
                "team_format": "4v4",
                "team_count": 2,
                "team_sizes": [4, 4],
                "team_total": 8,
                "team_size": 4,
                "online_multiplayer": True,
                "online_max": 8,
                "online_coop": True,
                "online_coop_max": 4,
                "platform": "PC",
            },
        )

    def test_asymmetric_team_format_uses_total_players(self):
        support = store._extract_team_support(
            {
                "short_description": (
                    "An intense online 1v4 horror match."
                ),
                "categories": [
                    {"description": "Online PvP"},
                ],
            }
        )

        self.assertEqual(support["team_format"], "1v4")
        self.assertEqual(support["team_total"], 5)
        self.assertEqual(support["online_max"], 5)

    def test_verified_limit_fills_missing_steam_text(self):
        max_players = store._extract_max_players(
            {
                "steam_appid": 1062520,
                "short_description": (
                    "Play either solo or with friends."
                ),
            }
        )

        self.assertEqual(max_players, 6)

    def test_superliminal_verified_limit_is_supported(self):
        max_players = store._extract_max_players(
            {
                "steam_appid": 1049410,
            }
        )

        self.assertEqual(max_players, 12)

    def test_catto_pew_pew_verified_limit_is_supported(self):
        max_players = store._extract_max_players(
            {
                "steam_appid": 3665520,
                "short_description": (
                    "A competitive physics-based shooter."
                ),
            }
        )

        self.assertEqual(max_players, 16)

    def test_player_plus_friends_wording_uses_total(self):
        max_players = store._extract_max_players(
            {
                "steam_appid": 2354000,
                "about_the_game": (
                    "If you're afraid to go alone, play "
                    "with 3 friends online."
                ),
            }
        )

        self.assertEqual(max_players, 4)

    def test_unknown_game_without_count_stays_unknown(self):
        max_players = store._extract_max_players(
            {
                "steam_appid": 999999999,
                "short_description": (
                    "Play either solo or with friends."
                ),
            }
        )

        self.assertIsNone(max_players)

    def test_steam_categories_build_online_support(self):
        support = store._extract_multiplayer_support(
            {
                "steam_appid": 1062520,
                "categories": [
                    {"description": "Single-player"},
                    {"description": "Online Co-op"},
                ],
            },
            6,
        )

        self.assertEqual(
            support["online_coop_max"],
            6,
        )
        self.assertNotIn(
            "online_multiplayer",
            support,
        )
        self.assertEqual(
            store._extract_steam_game_modes(
                {
                    "categories": [
                        {"description": "Single-player"},
                        {"description": "Online Co-op"},
                    ],
                },
                support,
            ),
            [
                "Single player",
                "Multiplayer",
                "Co-operative",
            ],
        )

    def test_local_only_categories_stay_local(self):
        support = store._extract_multiplayer_support(
            {
                "categories": [
                    {"description": "Single-player"},
                    {"description": "Local Co-op"},
                    {
                        "description": (
                            "Shared/Split Screen Co-op"
                        )
                    },
                ],
            },
            4,
        )

        self.assertEqual(
            support["offline_coop_max"],
            4,
        )
        self.assertTrue(support["split_screen"])
        self.assertNotIn("online_coop", support)

    def test_steam_genres_drop_indie_when_better_options_exist(self):
        genres = store._extract_steam_genres(
            {
                "genres": [
                    {"description": "Indie"},
                    {"description": "Action"},
                    {"description": "Adventure"},
                    {"description": "Strategy"},
                ]
            }
        )

        self.assertEqual(
            genres,
            ["Action", "Adventure", "Strategy"],
        )

    def test_meowgic_uses_separate_coop_and_arena_limits(self):
        self.assertEqual(
            store._extract_max_players(
                {
                    "steam_appid": 4252290,
                    "short_description": (
                        "An adventure for up to 4 players."
                    ),
                }
            ),
            6,
        )
        support = store._extract_multiplayer_support(
            {
                "steam_appid": 4252290,
                "categories": [
                    {"description": "Online PvP"},
                    {"description": "Online Co-op"},
                ],
            },
            6,
        )

        self.assertEqual(support["online_coop_max"], 4)
        self.assertEqual(support["online_max"], 6)
        self.assertEqual(support["team_format"], "3v3")


class MetadataReconciliationTests(
    unittest.TestCase
):
    def test_current_fallback_artwork_beats_stale_sources(self):
        embed = SimpleNamespace(
            url=(
                "https://store.epicgames.com/"
                "en-US/p/fortnite"
            ),
            title="Fortnite | Epic Games Store",
            description="",
            fields=[],
            image=SimpleNamespace(
                url=(
                    "https://cdn.discordapp.com/"
                    "stale-preview.jpg"
                )
            ),
            thumbnail=None,
        )

        merged = reconcile_suggestion_metadata(
            {
                "name": "Fortnite",
                "store": "Epic Games Store",
                "store_link": embed.url,
                "image_url": (
                    "https://cdn2.steamgriddb.com/"
                    "grid/fortnite.png"
                ),
                "verification_status": "blocked",
                "link_status": "blocked",
            },
            embeds=[embed],
            store_link=embed.url,
            existing_record={
                "name": "Fortnite",
                "store_link": embed.url,
                "image_url": (
                    "https://old.example/fortnite.jpg"
                ),
                "link_status": "live",
                "availability_status": "released",
                "coming_soon": False,
            },
        )

        self.assertEqual(
            merged["image_url"],
            (
                "https://cdn2.steamgriddb.com/"
                "grid/fortnite.png"
            ),
        )
        self.assertEqual(
            merged["link_status"],
            "live",
        )

    def test_release_embed_is_parsed_once_by_shared_service(self):
        embed = SimpleNamespace(
            url=(
                "https://store.steampowered.com/"
                "app/12345/"
            ),
            title="Upcoming Game",
            description="Coming soon",
            fields=[
                SimpleNamespace(
                    name="Release Date",
                    value="December 2099",
                )
            ],
        )

        result = release_info_from_embeds(
            [embed],
            embed.url,
        )

        self.assertTrue(
            result["coming_soon"]
        )
        self.assertEqual(
            result["release_date"],
            "December 2099",
        )


if __name__ == "__main__":
    unittest.main()
