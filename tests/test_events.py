import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import discord

import database.database as database
from commands.events import (
    GameNightEvents,
    POLL_DURATION,
    build_weekly_poll,
    choose_winner,
    event_start_for,
    fallback_event_due,
    poll_creation_allowed,
    reminder_hours_due,
    week_start_for,
)


SYDNEY = ZoneInfo("Australia/Sydney")


class GameNightScheduleTests(unittest.TestCase):
    def test_native_poll_contains_two_dated_choices(self):
        poll = build_weekly_poll(
            datetime(2026, 8, 10).date(),
            SYDNEY,
        )

        self.assertEqual(poll.duration, POLL_DURATION)
        self.assertFalse(poll.multiple)
        self.assertEqual(
            [answer.text for answer in poll.answers],
            [
                "Friday — 14 August at 9:00 PM",
                "Saturday — 15 August at 9:00 PM",
            ],
        )

    def test_week_and_event_dates_use_sydney_time(self):
        local_now = datetime(
            2026,
            8,
            12,
            12,
            tzinfo=SYDNEY,
        )
        week_start = week_start_for(local_now)

        self.assertEqual(
            week_start.isoformat(),
            "2026-08-10",
        )
        self.assertEqual(
            event_start_for(
                week_start,
                "friday",
                SYDNEY,
            ).isoformat(),
            "2026-08-14T21:00:00+10:00",
        )
        self.assertEqual(
            event_start_for(
                week_start,
                "saturday",
                SYDNEY,
            ).isoformat(),
            "2026-08-15T21:00:00+10:00",
        )

    def test_event_time_observes_daylight_saving(self):
        local_now = datetime(
            2027,
            1,
            4,
            12,
            tzinfo=SYDNEY,
        )
        event_start = event_start_for(
            week_start_for(local_now),
            "friday",
            SYDNEY,
        )

        self.assertEqual(
            event_start.utcoffset(),
            timedelta(hours=11),
        )
        self.assertEqual(event_start.hour, 21)

    def test_poll_starts_at_eleven_and_has_tuesday_catchup(self):
        self.assertFalse(
            poll_creation_allowed(
                datetime(2026, 8, 10, 10, 59, tzinfo=SYDNEY)
            )
        )
        self.assertTrue(
            poll_creation_allowed(
                datetime(2026, 8, 10, 11, 0, tzinfo=SYDNEY)
            )
        )
        self.assertTrue(
            poll_creation_allowed(
                datetime(2026, 8, 11, 21, 0, tzinfo=SYDNEY)
            )
        )
        self.assertFalse(
            poll_creation_allowed(
                datetime(2026, 8, 11, 21, 1, tzinfo=SYDNEY)
            )
        )
        self.assertTrue(
            fallback_event_due(
                datetime(2026, 8, 12, 11, 0, tzinfo=SYDNEY)
            )
        )

    def test_saturday_is_tie_and_no_vote_fallback(self):
        self.assertEqual(
            choose_winner(4, 2),
            ("friday", "votes"),
        )
        self.assertEqual(
            choose_winner(2, 4),
            ("saturday", "votes"),
        )
        self.assertEqual(
            choose_winner(3, 3),
            ("saturday", "tie"),
        )
        self.assertEqual(
            choose_winner(0, 0),
            ("saturday", "no_votes"),
        )

    def test_only_unsent_due_reminders_are_returned(self):
        event_start = datetime(
            2026,
            8,
            14,
            11,
            tzinfo=timezone.utc,
        )
        record = {
            "event_start_at": event_start.isoformat(),
            "reminder_24h_sent": True,
            "reminder_6h_sent": False,
            "reminder_1h_sent": False,
        }

        self.assertEqual(
            reminder_hours_due(
                record,
                event_start - timedelta(hours=5),
            ),
            (6,),
        )
        self.assertEqual(
            reminder_hours_due(
                record,
                event_start - timedelta(minutes=30),
            ),
            (6, 1),
        )
        self.assertEqual(
            reminder_hours_due(record, event_start),
            (),
        )


class GameNightEventCreationTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_voice_event_uses_guild_only_privacy(self):
        guild = SimpleNamespace(
            create_scheduled_event=AsyncMock(
                return_value=SimpleNamespace(id=123)
            )
        )
        poll_channel = SimpleNamespace(guild=guild)
        voice_channel = SimpleNamespace(id=456)
        cog = object.__new__(GameNightEvents)
        cog.event_timezone = SYDNEY
        cog._configured_channels = AsyncMock(
            return_value=(poll_channel, voice_channel)
        )
        cog._find_existing_event = AsyncMock(
            return_value=None
        )

        await cog._create_event(
            datetime(2026, 8, 3).date(),
            "saturday",
        )

        call = guild.create_scheduled_event.await_args
        self.assertEqual(
            call.kwargs["privacy_level"],
            discord.PrivacyLevel.guild_only,
        )


class GameNightDatabaseTests(
    unittest.IsolatedAsyncioTestCase
):
    async def asyncSetUp(self):
        await database.close_database()
        self.database_path = (
            Path(__file__).resolve().parent
            / f"event-test-{uuid.uuid4().hex}.db"
        )
        database.DATABASE = str(self.database_path)
        await database.setup_database()

    async def asyncTearDown(self):
        await database.close_database()

        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.database_path}{suffix}").unlink(
                missing_ok=True
            )

    async def test_poll_event_and_reminders_persist(self):
        record = await database.save_game_night_poll(
            week_start="2026-08-10",
            channel_id=100,
            message_id=200,
            created_at="2026-08-10T01:00:00+00:00",
            closes_at="2026-08-12T01:00:00+00:00",
        )
        self.assertEqual(record["poll_message_id"], 200)

        record = await database.save_game_night_result(
            week_start="2026-08-10",
            winner_day="friday",
            winner_reason="votes",
            friday_votes=7,
            saturday_votes=4,
            scheduled_event_id=300,
            event_start_at="2026-08-14T11:00:00+00:00",
        )
        self.assertEqual(record["scheduled_event_id"], 300)
        self.assertEqual(record["friday_votes"], 7)
        self.assertFalse(record["reminder_24h_sent"])

        await database.set_game_night_reminders(
            "2026-08-10",
            (24, 6),
            sent=True,
        )
        record = await database.get_game_night_week(
            "2026-08-10"
        )
        self.assertTrue(record["reminder_24h_sent"])
        self.assertTrue(record["reminder_6h_sent"])
        self.assertFalse(record["reminder_1h_sent"])

    async def test_checkin_changes_persist_and_close(self):
        await database.save_game_night_result(
            week_start="2026-08-10",
            winner_day="friday",
            winner_reason="votes",
            friday_votes=7,
            saturday_votes=4,
            scheduled_event_id=300,
            event_start_at="2026-08-14T11:00:00+00:00",
        )
        record = await database.save_game_night_checkin_message(
            "2026-08-10",
            channel_id=100,
            message_id=400,
        )
        self.assertEqual(record["checkin_message_id"], 400)
        self.assertFalse(record["checkin_closed"])

        await database.set_game_night_checkin(
            "2026-08-10",
            user_id=1,
            display_name="Player One",
            response="playing",
        )
        await database.set_game_night_checkin(
            "2026-08-10",
            user_id=2,
            display_name="Player Two",
            response="maybe",
        )
        await database.set_game_night_checkin(
            "2026-08-10",
            user_id=1,
            display_name="Player One",
            response="cant_make_it",
        )
        checkins = await database.get_game_night_checkins(
            "2026-08-10"
        )
        self.assertEqual(
            checkins["counts"],
            {
                "playing": 0,
                "maybe": 1,
                "cant_make_it": 1,
            },
        )

        record = await database.close_game_night_checkin(
            "2026-08-10"
        )
        self.assertTrue(record["checkin_closed"])
        rejected = await database.set_game_night_checkin(
            "2026-08-10",
            user_id=3,
            display_name="Late Player",
            response="playing",
        )
        self.assertIsNone(rejected)


if __name__ == "__main__":
    unittest.main()
