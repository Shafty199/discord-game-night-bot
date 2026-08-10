import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from commands.sessions import Sessions


EVENT_START = datetime(
    2026,
    8,
    8,
    11,
    tzinfo=timezone.utc,
)
SESSION = {
    "id": 12,
    "guild_id": 123,
    "voice_channel_id": 456,
}
RECORD = {
    "scheduled_event_id": 789,
    "event_start_at": EVENT_START.isoformat(),
}


class SessionEventSyncTests(
    unittest.IsolatedAsyncioTestCase
):
    def _cog_and_event(self, status):
        event = SimpleNamespace(
            id=789,
            channel_id=456,
            status=status,
            start=AsyncMock(),
            end=AsyncMock(),
        )
        guild = SimpleNamespace(
            fetch_scheduled_event=AsyncMock(
                return_value=event
            )
        )
        bot = SimpleNamespace(
            get_guild=lambda guild_id: (
                guild if guild_id == 123 else None
            )
        )
        cog = object.__new__(Sessions)
        cog.bot = bot
        return cog, guild, event

    async def test_starting_wheel_activates_matching_event(self):
        cog, guild, event = self._cog_and_event(
            discord.EventStatus.scheduled
        )

        with (
            patch(
                "commands.sessions.GAME_NIGHT_VOICE_CHANNEL_ID",
                456,
            ),
            patch(
                "commands.sessions.GAME_NIGHT_TIMEZONE",
                "Australia/Sydney",
            ),
            patch(
                "commands.sessions.get_game_night_week",
                new=AsyncMock(return_value=RECORD),
            ),
        ):
            changed = await cog._sync_weekly_scheduled_event(
                SESSION,
                start=True,
                now_utc=EVENT_START,
            )

        self.assertTrue(changed)
        guild.fetch_scheduled_event.assert_awaited_once_with(789)
        event.start.assert_awaited_once_with(
            reason="Game Night wheel session started"
        )
        event.end.assert_not_awaited()

    async def test_ad_hoc_session_does_not_start_future_event(self):
        cog, guild, event = self._cog_and_event(
            discord.EventStatus.scheduled
        )

        with (
            patch(
                "commands.sessions.GAME_NIGHT_VOICE_CHANNEL_ID",
                456,
            ),
            patch(
                "commands.sessions.GAME_NIGHT_TIMEZONE",
                "Australia/Sydney",
            ),
            patch(
                "commands.sessions.get_game_night_week",
                new=AsyncMock(return_value=RECORD),
            ),
        ):
            changed = await cog._sync_weekly_scheduled_event(
                SESSION,
                start=True,
                now_utc=EVENT_START - timedelta(hours=3),
            )

        self.assertFalse(changed)
        guild.fetch_scheduled_event.assert_not_awaited()
        event.start.assert_not_awaited()

    async def test_ending_wheel_completes_active_event(self):
        cog, _guild, event = self._cog_and_event(
            discord.EventStatus.active
        )

        with (
            patch(
                "commands.sessions.GAME_NIGHT_VOICE_CHANNEL_ID",
                456,
            ),
            patch(
                "commands.sessions.GAME_NIGHT_TIMEZONE",
                "Australia/Sydney",
            ),
            patch(
                "commands.sessions.get_game_night_week",
                new=AsyncMock(return_value=RECORD),
            ),
        ):
            changed = await cog._sync_weekly_scheduled_event(
                SESSION,
                start=False,
                now_utc=EVENT_START + timedelta(hours=4),
            )

        self.assertTrue(changed)
        event.end.assert_awaited_once_with(
            reason="Game Night wheel session ended"
        )


if __name__ == "__main__":
    unittest.main()
