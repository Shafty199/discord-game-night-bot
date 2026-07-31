import asyncio
import unittest
from datetime import datetime, timezone
from time import monotonic
from unittest.mock import AsyncMock, patch

import aiosqlite

from database import database
from utils import http_retry, steam_api, store
from utils.time_utils import (
    DISPLAY_TIMEZONE,
    DISPLAY_TIMEZONE_LABEL,
    format_display_datetime,
    normalise_stored_datetime,
)


class FakeResponse:
    def __init__(self, status, *, headers=None):
        self.status = status
        self.headers = headers or {}


class FakeRequestContext:
    def __init__(self, response):
        self.response = response
        self.exited = False

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, exc_type, exc_value, traceback):
        self.exited = True
        return False


class FakeSession:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        return FakeRequestContext(
            FakeResponse(self.statuses.pop(0))
        )


class HttpRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_responses_retry_twice(self):
        session = FakeSession([503, 429, 200])

        with patch.object(
            http_retry.asyncio,
            "sleep",
            new=AsyncMock(),
        ) as sleep:
            async with http_retry.retrying_request(
                session,
                "GET",
                "https://example.test/data",
            ) as response:
                self.assertEqual(response.status, 200)

        self.assertEqual(session.calls, 3)
        self.assertEqual(sleep.await_count, 2)

    async def test_permanent_failure_is_not_retried(self):
        session = FakeSession([404])

        async with http_retry.retrying_request(
            session,
            "GET",
            "https://example.test/missing",
        ) as response:
            self.assertEqual(response.status, 404)

        self.assertEqual(session.calls, 1)


class TimeHandlingTests(unittest.TestCase):
    def test_legacy_local_value_migrates_to_utc(self):
        expected = datetime(
            2026,
            7,
            31,
            0,
            30,
            tzinfo=DISPLAY_TIMEZONE,
        ).astimezone(timezone.utc).isoformat()

        self.assertEqual(
            normalise_stored_datetime(
                "2026-07-31T00:30:00"
            ),
            expected,
        )

    def test_utc_value_displays_in_configured_timezone(self):
        expected_datetime = datetime(
            2026,
            7,
            30,
            14,
            30,
            tzinfo=timezone.utc,
        ).astimezone(DISPLAY_TIMEZONE)
        expected = (
            expected_datetime.strftime("%d %b %Y, %H:%M")
            + f" {DISPLAY_TIMEZONE_LABEL}"
        )

        self.assertEqual(
            format_display_datetime(
                "2026-07-30T14:30:00+00:00"
            ),
            expected,
        )


class CacheCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        steam_api.clear_steam_details_cache()
        await store.clear_store_metadata_cache()

    async def asyncTearDown(self):
        steam_api.clear_steam_details_cache()
        await store.clear_store_metadata_cache()

    async def test_expired_steam_payload_is_pruned(self):
        steam_api._details_cache["123"] = (
            monotonic()
            - steam_api.STEAM_DETAILS_CACHE_TTL_SECONDS
            - 1,
            {"large": "payload"},
        )

        self.assertEqual(
            steam_api.prune_steam_details_cache(),
            1,
        )
        self.assertFalse(steam_api._details_cache)

    async def test_store_cache_can_be_released_after_sync(self):
        store._metadata_cache["example"] = (
            monotonic() + 300,
            {"name": "Example"},
        )

        self.assertEqual(
            await store.clear_store_metadata_cache(),
            1,
        )
        self.assertFalse(store._metadata_cache)


class TimestampMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_timestamp_columns_are_migrated(self):
        async with aiosqlite.connect(":memory:") as db:
            await db.execute(
                """
                CREATE TABLE games (
                    last_played TEXT,
                    added_date TEXT,
                    last_link_check TEXT
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE game_history (
                    played_date TEXT
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE store_replacements (
                    replaced_at TEXT
                )
                """
            )
            await db.execute(
                """
                INSERT INTO games VALUES (?, ?, ?)
                """,
                (
                    "2026-07-31T00:30:00",
                    "2026-07-30T12:00:00",
                    "2026-07-30T13:00:00",
                ),
            )
            await db.execute(
                "INSERT INTO game_history VALUES (?)",
                ("2026-07-31T00:30:00",),
            )
            await db.execute(
                "INSERT INTO store_replacements VALUES (?)",
                ("2026-07-31T00:30:00",),
            )

            updated = await database._normalise_timestamp_columns(
                db
            )
            row = await (
                await db.execute(
                    "SELECT last_played FROM games"
                )
            ).fetchone()

        self.assertEqual(updated, 5)
        self.assertEqual(
            row[0],
            "2026-07-30T14:30:00+00:00",
        )


if __name__ == "__main__":
    unittest.main()
