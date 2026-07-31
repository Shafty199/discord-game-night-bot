import os
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from database import database


class DatabaseMaintenanceTests(
    unittest.IsolatedAsyncioTestCase
):
    async def asyncSetUp(self):
        self.root = (
            Path.cwd()
            / ".test-tmp"
            / "database-maintenance-tests"
        )
        self.root.mkdir(
            parents=True,
            exist_ok=True,
        )
        self.database_path = (
            self.root / "database" / "games.db"
        )
        self.database_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        self.backup_directory = (
            self.database_path.parent / "backups"
        )

        for suffix in ("", "-shm", "-wal"):
            Path(
                f"{self.database_path}{suffix}"
            ).unlink(missing_ok=True)

        if self.backup_directory.exists():
            for backup_path in (
                self.backup_directory.glob("*.db")
            ):
                backup_path.unlink(missing_ok=True)

        self.patchers = (
            patch.object(
                database,
                "DATABASE",
                str(self.database_path),
            ),
            patch.object(
                database,
                "DATABASE_PATH",
                self.database_path,
            ),
            patch.object(
                database,
                "BACKUP_DIRECTORY",
                self.backup_directory,
            ),
        )

        for patcher in self.patchers:
            patcher.start()

        await database.setup_database()

    async def asyncTearDown(self):
        await database.close_database()

        for patcher in reversed(
            self.patchers
        ):
            patcher.stop()

        for suffix in ("", "-shm", "-wal"):
            Path(
                f"{self.database_path}{suffix}"
            ).unlink(missing_ok=True)

    async def _insert_history(self):
        import aiosqlite

        async with aiosqlite.connect(
            self.database_path
        ) as db:
            cursor = await db.execute(
                """
                INSERT INTO games (
                    name,
                    store_link,
                    store,
                    suggested_by,
                    times_played,
                    last_played,
                    availability_status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "Test Game",
                    "https://example.com/test-game",
                    "Test Store",
                    "Tester",
                    2,
                    "2021-07-28T20:00:00",
                    "released",
                ),
            )
            game_id = cursor.lastrowid

            await db.executemany(
                """
                INSERT INTO game_history (
                    game_id,
                    played_date,
                    locked_by
                )
                VALUES (?, ?, ?)
                """,
                (
                    (
                        game_id,
                        "2020-07-21T20:00:00",
                        "First Moderator",
                    ),
                    (
                        game_id,
                        "2021-07-28T20:00:00",
                        "Second Moderator",
                    ),
                ),
            )
            await db.commit()

        return game_id

    async def test_undo_recalculates_game_history(self):
        game_id = await self._insert_history()
        latest = await database.get_latest_history_entry()

        result = await database.undo_latest_history_entry(
            expected_history_id=latest["history_id"]
        )

        self.assertEqual(
            result["status"],
            "undone",
        )
        self.assertEqual(
            result["times_played"],
            1,
        )
        self.assertEqual(
            result["last_played"],
            "2020-07-21T20:00:00",
        )

        with closing(
            sqlite3.connect(
                self.database_path
            )
        ) as db:
            history_count = db.execute(
                """
                SELECT COUNT(*)
                FROM game_history
                WHERE game_id = ?
                """,
                (game_id,),
            ).fetchone()[0]

        self.assertEqual(
            history_count,
            1,
        )

    async def test_undo_rejects_a_stale_confirmation(self):
        await self._insert_history()
        latest = await database.get_latest_history_entry()

        await database.mark_game_played(
            game_id=latest["game_id"],
            locked_by="New Moderator",
        )

        result = await database.undo_latest_history_entry(
            expected_history_id=latest["history_id"]
        )

        self.assertEqual(
            result["status"],
            "stale",
        )

    async def test_backup_is_valid_and_rotates_only_auto_files(self):
        await self._insert_history()
        self.backup_directory.mkdir(
            parents=True,
            exist_ok=True,
        )

        for index in range(3):
            old_backup = self.backup_directory / (
                f"games-auto-old-{index}.db"
            )
            old_backup.touch()
            old_timestamp = 1_600_000_000 + index
            os.utime(
                old_backup,
                (old_timestamp, old_timestamp),
            )

        manual_backup = (
            self.backup_directory
            / "games-before-manual-change.db"
        )
        manual_backup.touch()

        result = await database.create_automatic_backup(
            minimum_interval_hours=0,
            retention=2,
        )

        self.assertEqual(
            result["status"],
            "created",
        )
        self.assertTrue(
            manual_backup.exists()
        )
        self.assertEqual(
            len(
                list(
                    self.backup_directory.glob(
                        "games-auto-*.db"
                    )
                )
            ),
            2,
        )

        with closing(
            sqlite3.connect(
                result["path"]
            )
        ) as backup_db:
            self.assertEqual(
                backup_db.execute(
                    "PRAGMA quick_check"
                ).fetchone()[0],
                "ok",
            )

    async def test_connection_pool_reuses_open_connections(self):
        pool = database._database_pool

        self.assertIsNotNone(pool)
        self.assertEqual(
            len(pool._connections),
            database.DATABASE_POOL_SIZE,
        )

        seen_connection_ids = []

        for _ in range(
            database.DATABASE_POOL_SIZE * 2
        ):
            async with database.database_connection() as db:
                seen_connection_ids.append(id(db))

        self.assertEqual(
            len(set(seen_connection_ids)),
            database.DATABASE_POOL_SIZE,
        )

    async def test_stats_uses_all_game_categories(self):
        async with database.database_connection() as db:
            await db.executemany(
                """
                INSERT INTO games (
                    name,
                    times_played,
                    availability_status,
                    link_status,
                    max_players,
                    suggested_by
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        "Unplayed Multiplayer",
                        0,
                        "released",
                        "live",
                        4,
                        "Alex",
                    ),
                    (
                        "Played Multiplayer",
                        3,
                        "released",
                        "live",
                        None,
                        "Alex",
                    ),
                    (
                        "Singleplayer",
                        0,
                        "released",
                        "live",
                        1,
                        "Sam",
                    ),
                    (
                        "Wishlist",
                        0,
                        "coming_soon",
                        "unknown",
                        4,
                        "Sam",
                    ),
                    (
                        "Dead Link",
                        0,
                        "released",
                        "dead",
                        4,
                        "Sam",
                    ),
                ),
            )
            await db.commit()

        stats = await database.get_stats()

        self.assertEqual(stats["total_games"], 2)
        self.assertEqual(stats["never_played"], 1)
        self.assertEqual(stats["singleplayer_games"], 1)
        self.assertEqual(stats["wishlist_games"], 1)

    async def test_random_selection_falls_back_to_recent_games(self):
        async with database.database_connection() as db:
            await db.execute(
                """
                INSERT INTO games (
                    name,
                    times_played,
                    last_played,
                    availability_status,
                    link_status,
                    max_players,
                    image_url
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "Recent Multiplayer",
                    1,
                    "2026-07-28T20:00:00",
                    "released",
                    "live",
                    4,
                    "https://example.com/permanent.jpg",
                ),
            )
            await db.commit()

        with patch.object(
            database.secrets,
            "randbelow",
            return_value=0,
        ):
            game = await database.get_smart_random_game()

        self.assertIsNotNone(game)
        self.assertEqual(game["name"], "Recent Multiplayer")
        self.assertEqual(
            game["display_image_url"],
            "https://example.com/permanent.jpg",
        )
        self.assertEqual(
            game["source_image_url"],
            "https://example.com/permanent.jpg",
        )

    async def test_player_metadata_respects_source_priority(self):
        common = {
            "name": "Metadata Game",
            "store_link": (
                "https://store.steampowered.com/app/123/"
            ),
            "store": "Steam",
            "suggested_by": "Tester",
            "external_id": "123",
            "link_status": "live",
        }

        await database.sync_game(
            **common,
            max_players=4,
            max_players_source="IGDB",
        )
        await database.sync_game(
            **common,
            max_players=6,
            max_players_source="Steam",
        )
        await database.sync_game(
            **common,
            max_players=8,
            max_players_source="IGDB",
        )

        record = await database.get_game_cache_record(
            name="Metadata Game",
            store="Steam",
            external_id="123",
        )

        self.assertEqual(record["max_players"], 6)
        self.assertEqual(
            record["max_players_source"],
            "Steam",
        )

    async def test_igdb_metadata_is_saved_and_selected_for_spin(self):
        await database.sync_game(
            name="Rich Metadata Game",
            store_link=(
                "https://store.steampowered.com/app/789/"
            ),
            store="Steam",
            suggested_by="Tester",
            external_id="789",
            link_status="live",
            max_players=4,
            max_players_source="IGDB",
            igdb_id=12345,
            multiplayer_support={
                "online_coop": True,
                "online_coop_max": 4,
                "platform": "PC",
            },
            genres=["Adventure", "Indie"],
            themes=["Comedy"],
            game_modes=["Multiplayer", "Co-operative"],
        )

        record = await database.get_game_cache_record(
            name="Rich Metadata Game",
            store="Steam",
            external_id="789",
        )

        self.assertEqual(record["igdb_id"], 12345)
        self.assertEqual(
            record["genres"],
            '["Adventure","Indie"]',
        )

        with patch.object(
            database.secrets,
            "randbelow",
            return_value=0,
        ):
            game = await database.get_smart_random_game()

        self.assertEqual(game["igdb_id"], 12345)
        self.assertEqual(
            game["game_modes_json"],
            '["Multiplayer","Co-operative"]',
        )

    async def test_local_only_games_move_to_singleplayer_wheel(self):
        games = (
            (
                "Local Co-op",
                4,
                '{"offline_coop":true,"offline_coop_max":4,"platform":"PC"}',
            ),
            (
                "Local and Online",
                1,
                '{"offline_coop":true,"online_coop":true,"platform":"PC"}',
            ),
            (
                "Online Multiplayer",
                8,
                '{"online_max":8,"platform":"PC"}',
            ),
            (
                "Unknown Multiplayer",
                4,
                None,
            ),
            (
                "Single Player",
                1,
                None,
            ),
        )

        async with database.database_connection() as db:
            await db.executemany(
                """
                INSERT INTO games (
                    name,
                    max_players,
                    multiplayer_support_json,
                    availability_status,
                    link_status,
                    suggested_by
                )
                VALUES (?, ?, ?, 'released', 'live', 'Tester')
                """,
                games,
            )
            await db.commit()

        multiplayer_games = await database.get_all_games()
        singleplayer_games = (
            await database.get_all_singleplayer_games()
        )

        multiplayer_names = {
            game[0]
            for game in multiplayer_games
        }
        singleplayer_reasons = {
            game[0]: game[5]
            for game in singleplayer_games
        }

        self.assertEqual(
            multiplayer_names,
            {
                "Local and Online",
                "Online Multiplayer",
                "Unknown Multiplayer",
            },
        )
        self.assertEqual(
            singleplayer_reasons,
            {
                "Local Co-op": "local_only",
                "Single Player": "single_player",
            },
        )

        stats = await database.get_stats()
        self.assertEqual(stats["total_games"], 3)
        self.assertEqual(stats["singleplayer_games"], 2)

    async def test_sync_reports_local_only_wheel_moves(self):
        common = {
            "name": "Changing Support",
            "store_link": (
                "https://store.steampowered.com/app/987/"
            ),
            "store": "Steam",
            "suggested_by": "Tester",
            "external_id": "987",
            "link_status": "live",
            "max_players": 4,
            "max_players_source": "IGDB",
        }

        added = await database.sync_game(**common)
        moved_local = await database.sync_game(
            **common,
            multiplayer_support={
                "offline_coop": True,
                "offline_coop_max": 4,
                "platform": "PC",
            },
        )
        moved_online = await database.sync_game(
            **common,
            multiplayer_support={
                "online_coop": True,
                "online_coop_max": 4,
                "platform": "PC",
            },
        )

        self.assertEqual(added, "added")
        self.assertEqual(
            moved_local,
            "moved_to_singleplayer",
        )
        self.assertEqual(
            moved_online,
            "moved_to_multiplayer",
        )

    async def test_existing_unsourced_player_value_is_protected(self):
        async with database.database_connection() as db:
            await db.execute(
                """
                INSERT INTO games (
                    name,
                    store_link,
                    store,
                    suggested_by,
                    external_id,
                    link_status,
                    max_players
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "Saved Count",
                    (
                        "https://store.steampowered.com/"
                        "app/456/"
                    ),
                    "Steam",
                    "Tester",
                    "456",
                    "live",
                    5,
                ),
            )
            await db.commit()

        await database.sync_game(
            name="Saved Count",
            store_link=(
                "https://store.steampowered.com/app/456/"
            ),
            store="Steam",
            suggested_by="Tester",
            external_id="456",
            link_status="live",
            max_players=4,
            max_players_source="IGDB",
        )

        record = await database.get_game_cache_record(
            name="Saved Count",
            store="Steam",
            external_id="456",
        )

        self.assertEqual(record["max_players"], 5)
        self.assertEqual(
            record["max_players_source"],
            "Saved",
        )

    async def test_saved_limit_completes_new_support_category(self):
        common = {
            "name": "Saved Support Limit",
            "store_link": (
                "https://store.steampowered.com/app/789/"
            ),
            "store": "Steam",
            "suggested_by": "Tester",
            "external_id": "789",
            "link_status": "live",
        }

        await database.sync_game(
            **common,
            max_players=4,
            max_players_source="Saved",
        )
        await database.sync_game(
            **common,
            multiplayer_support={
                "online_coop": True,
            },
        )

        record = await database.get_game_cache_record(
            name="Saved Support Limit",
            store="Steam",
            external_id="789",
        )

        self.assertIn(
            '"online_coop_max":4',
            record["multiplayer_support"],
        )

    async def test_sync_cache_snapshots_are_loaded_in_bulk(self):
        await database.sync_game(
            name="Snapshot Game",
            store_link=(
                "https://store.steampowered.com/app/321/"
            ),
            store="Steam",
            suggested_by="Tester",
            external_id="321",
            link_status="live",
            max_players=4,
            max_players_source="Steam",
            multiplayer_support={
                "online_multiplayer": True,
                "online_max": 4,
            },
            genres=["Action"],
            game_modes=["Multiplayer"],
        )
        game_records = (
            await database.get_all_game_cache_records()
        )

        self.assertEqual(len(game_records), 1)
        self.assertEqual(
            game_records[0]["external_id"],
            "321",
        )
        self.assertIn(
            '"online_max":4',
            game_records[0]["multiplayer_support"],
        )

        await database.save_store_replacement(
            store="Steam",
            old_external_id="123",
            old_store_link=(
                "https://store.steampowered.com/app/123/"
            ),
            old_name="Old Demo",
            game_id=game_records[0]["id"],
            new_external_id="321",
            new_store_link=game_records[0][
                "store_link"
            ],
            new_name="Snapshot Game",
        )
        replacements = (
            await database.get_all_store_replacements(
                store="Steam"
            )
        )

        self.assertEqual(len(replacements), 1)
        self.assertEqual(
            replacements[0]["old_external_id"],
            "123",
        )

    async def test_wishlist_release_date_is_updated(self):
        common = {
            "name": "Delayed Game",
            "store_link": (
                "https://store.steampowered.com/app/654/"
            ),
            "store": "Steam",
            "suggested_by": "Tester",
            "external_id": "654",
            "link_status": "live",
            "availability_status": "coming_soon",
            "coming_soon": True,
        }

        await database.sync_game(
            **common,
            release_date="August 2026",
        )
        result = await database.sync_game(
            **common,
            release_date="February 2027",
        )
        record = await database.get_game_cache_record(
            store="Steam",
            external_id="654",
        )

        self.assertEqual(result, "wishlist_updated")
        self.assertEqual(
            record["release_date"],
            "February 2027",
        )


if __name__ == "__main__":
    unittest.main()
