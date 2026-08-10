import unittest
import uuid
from pathlib import Path

import database.database as database


class GamingSessionDatabaseTests(
    unittest.IsolatedAsyncioTestCase
):
    async def asyncSetUp(self):
        await database.close_database()
        self.database_path = (
            Path(__file__).resolve().parent
            / f"session-test-{uuid.uuid4().hex}.db"
        )
        database.DATABASE = str(self.database_path)
        await database.setup_database()

    async def asyncTearDown(self):
        await database.close_database()

        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.database_path}{suffix}").unlink(
                missing_ok=True
            )

    async def _insert_multiplayer_game(
        self,
        name: str,
        max_players,
    ) -> int:
        async with database.database_connection() as db:
            cursor = await db.execute(
                """
                INSERT INTO games (
                    name,
                    store,
                    availability_status,
                    link_status,
                    max_players,
                    max_players_source,
                    multiplayer_support_json
                )
                VALUES (?, 'Steam', 'released', 'live', ?, 'IGDB', ?)
                """,
                (
                    name,
                    max_players,
                    '["online_coop"]',
                ),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def test_session_filter_uses_confirmed_capacity(self):
        two_player = await self._insert_multiplayer_game(
            "Two Player",
            2,
        )
        four_player = await self._insert_multiplayer_game(
            "Four Player",
            4,
        )
        eight_player = await self._insert_multiplayer_game(
            "Eight Player",
            8,
        )
        unknown = await self._insert_multiplayer_game(
            "Unknown Capacity",
            None,
        )

        strict = await database.get_session_wheel_game_ids(5)
        self.assertEqual(
            strict["game_ids"],
            frozenset({eight_player}),
        )
        self.assertEqual(strict["unverified_count"], 1)
        self.assertEqual(strict["excluded_for_capacity"], 2)

        permissive = await database.get_session_wheel_game_ids(
            5,
            include_unverified=True,
        )
        self.assertEqual(
            permissive["game_ids"],
            frozenset({eight_player, unknown}),
        )

        normal = await database.get_session_wheel_game_ids(
            5,
            use_normal_wheel=True,
        )
        self.assertEqual(
            normal["game_ids"],
            frozenset(
                {
                    two_player,
                    four_player,
                    eight_player,
                    unknown,
                }
            ),
        )

    async def test_session_lifecycle_and_manual_override(self):
        session = await database.create_gaming_session(
            guild_id=100,
            host_id=200,
            host_name="Host",
            voice_channel_id=300,
            members=[
                (200, "Host"),
                (201, "Friend"),
            ],
        )
        self.assertEqual(session["status"], "starting")
        self.assertEqual(session["effective_player_count"], 2)

        session = await database.activate_gaming_session(
            session["id"],
            control_channel_id=400,
            control_message_id=500,
        )
        self.assertEqual(session["status"], "active")

        session = await database.configure_gaming_session(
            session["id"],
            manual_player_count=7,
            include_unverified=True,
        )
        self.assertEqual(session["effective_player_count"], 7)
        self.assertTrue(session["include_unverified"])

        session = await database.add_gaming_session_member(
            session["id"],
            user_id=202,
            display_name="Late Joiner",
        )
        self.assertEqual(len(session["members"]), 3)
        self.assertEqual(session["effective_player_count"], 7)

        session = await database.configure_gaming_session(
            session["id"],
            clear_manual_player_count=True,
        )
        self.assertEqual(session["effective_player_count"], 2)

        previous_generation = session["cache_generation"]
        session = await database.replace_gaming_session_members(
            session["id"],
            [
                (200, "Host"),
                (201, "Friend"),
                (202, "Late Joiner"),
                (203, "Fourth"),
            ],
        )
        self.assertEqual(session["effective_player_count"], 4)
        self.assertGreater(
            session["cache_generation"],
            previous_generation,
        )

        session = await database.select_gaming_session_game(
            session["id"],
            selected_by_id=200,
            selected_by_name="Host",
            custom_name="Custom Game",
            custom_link="https://example.com/game",
        )
        self.assertEqual(
            session["custom_game_name"],
            "Custom Game",
        )

        session = await database.clear_gaming_session_selection(
            session["id"]
        )
        self.assertIsNone(session["custom_game_name"])

        session = await database.end_gaming_session(
            session["id"]
        )
        self.assertEqual(session["status"], "ended")

    async def test_only_one_active_session_per_voice_channel(self):
        await database.create_gaming_session(
            guild_id=100,
            host_id=200,
            host_name="Host",
            voice_channel_id=300,
            members=[(200, "Host")],
        )

        with self.assertRaises(Exception):
            await database.create_gaming_session(
                guild_id=100,
                host_id=201,
                host_name="Other Host",
                voice_channel_id=300,
                members=[(201, "Other Host")],
            )

    async def test_host_transfer_and_multi_game_timeline(self):
        session = await database.create_gaming_session(
            guild_id=100,
            host_id=200,
            host_name="Host",
            voice_channel_id=300,
            members=[
                (200, "Host"),
                (201, "Friend"),
            ],
        )
        session = await database.activate_gaming_session(
            session["id"],
            control_channel_id=400,
            control_message_id=500,
        )

        rejected = await database.transfer_gaming_session_host(
            session["id"],
            user_id=999,
            display_name="Not Joined",
        )
        self.assertIsNone(rejected)

        session = await database.transfer_gaming_session_host(
            session["id"],
            user_id=201,
            display_name="Friend",
        )
        self.assertEqual(session["host_id"], 201)

        session = await database.select_gaming_session_game(
            session["id"],
            selected_by_id=201,
            selected_by_name="Friend",
            custom_name="First Game",
            custom_link="https://example.com/first",
        )
        session = await database.start_gaming_session_game(
            session["id"],
            game_name="First Game",
            game_link="https://example.com/first",
            selected_by_id=201,
            selected_by_name="Friend",
        )
        self.assertEqual(len(session["games_played"]), 1)
        self.assertIsNone(session["games_played"][0]["finished_at"])

        played = await database.finish_gaming_session_game(
            session["id"]
        )
        self.assertEqual(played["game_name"], "First Game")
        self.assertIsNotNone(played["finished_at"])

        session = await database.clear_gaming_session_selection(
            session["id"]
        )
        session = await database.select_gaming_session_game(
            session["id"],
            selected_by_id=201,
            selected_by_name="Friend",
            custom_name="Second Game",
        )
        await database.start_gaming_session_game(
            session["id"],
            game_name="Second Game",
            selected_by_id=201,
            selected_by_name="Friend",
        )
        session = await database.end_gaming_session(session["id"])

        self.assertEqual(len(session["games_played"]), 2)
        self.assertTrue(
            all(game["finished_at"] for game in session["games_played"])
        )


if __name__ == "__main__":
    unittest.main()
