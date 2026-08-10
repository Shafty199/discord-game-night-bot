import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from database import database


class DatabaseMaintenanceTests(
    unittest.IsolatedAsyncioTestCase
):
    async def asyncSetUp(self):
        self.temporary_directory = (
            tempfile.TemporaryDirectory()
        )
        self.root = Path(
            self.temporary_directory.name
        )
        self.database_path = (
            self.root / "database" / "games.db"
        )
        self.database_path.parent.mkdir(
            parents=True
        )
        self.backup_directory = (
            self.database_path.parent / "backups"
        )

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

        self.temporary_directory.cleanup()

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


    async def test_add_game_details_include_saved_record_and_artwork_change(self):
        common = {
            "name": "Detailed Sync Game",
            "store_link": (
                "https://store.steampowered.com/app/2468/"
            ),
            "store": "Steam",
            "suggested_by": "Tester",
            "external_id": "2468",
            "link_status": "live",
        }

        added = await database.add_game(
            **common,
            image_url="https://example.com/first.jpg",
            return_details=True,
        )
        unchanged = await database.add_game(
            **common,
            image_url="https://example.com/first.jpg",
            return_details=True,
        )
        updated = await database.add_game(
            **common,
            image_url="https://example.com/second.jpg",
            return_details=True,
        )

        self.assertEqual(added["status"], "added")
        self.assertIsNotNone(added["game_id"])
        self.assertEqual(
            added["record"]["id"],
            added["game_id"],
        )
        self.assertTrue(added["artwork_changed"])

        self.assertEqual(
            unchanged["status"],
            "unchanged",
        )
        self.assertEqual(
            unchanged["game_id"],
            added["game_id"],
        )
        self.assertFalse(
            unchanged["artwork_changed"]
        )

        self.assertEqual(updated["status"], "updated")
        self.assertEqual(
            updated["record"]["image_url"],
            "https://example.com/second.jpg",
        )
        self.assertTrue(updated["artwork_changed"])


    async def test_daily_igdb_candidates_only_include_incomplete_games(
        self,
    ):
        await database.sync_game(
            name="Missing IGDB Game",
            store_link=(
                "https://store.steampowered.com/app/111/"
            ),
            store="Steam",
            suggested_by="Tester",
            external_id="111",
            link_status="live",
        )
        await database.sync_game(
            name="Complete Without IGDB ID",
            store_link=(
                "https://store.steampowered.com/app/222/"
            ),
            store="Steam",
            suggested_by="Tester",
            external_id="222",
            link_status="live",
            max_players=4,
            max_players_source="Steam",
            multiplayer_support={
                "online_coop": True,
                "online_coop_max": 4,
            },
            genres=["Adventure"],
            game_modes=["Multiplayer"],
        )
        await database.sync_game(
            name="Wishlist Without IGDB ID",
            store_link=(
                "https://store.steampowered.com/app/333/"
            ),
            store="Steam",
            suggested_by="Tester",
            external_id="333",
            link_status="live",
            availability_status="coming_soon",
            coming_soon=True,
        )

        candidates = (
            await database.get_games_missing_igdb_metadata()
        )

        self.assertEqual(
            [game["name"] for game in candidates],
            ["Missing IGDB Game"],
        )
        self.assertIsNone(
            candidates[0]["multiplayer_support"]
        )
        self.assertIsNone(candidates[0]["genres"])

    async def test_daily_igdb_refresh_preserves_steam_limit(
        self,
    ):
        await database.sync_game(
            name="Daily IGDB Game",
            store_link=(
                "https://store.steampowered.com/app/444/"
            ),
            store="Steam",
            suggested_by="Tester",
            external_id="444",
            link_status="live",
            max_players=6,
            max_players_source="Steam",
        )
        candidates = (
            await database.get_games_missing_igdb_metadata()
        )
        game = candidates[0]
        game.update(
            {
                "igdb_id": 9876,
                "max_players": 8,
                "max_players_source": "IGDB",
                "multiplayer_support": {
                    "online_coop": True,
                    "online_coop_max": 8,
                },
                "genres": ["Adventure", "Indie"],
                "themes": ["Comedy"],
                "game_modes": [
                    "Multiplayer",
                    "Co-operative",
                ],
            }
        )

        changed = await database.save_refreshed_igdb_metadata(
            game["id"],
            game,
        )
        record = await database.get_game_cache_record(
            store="Steam",
            external_id="444",
        )

        self.assertTrue(changed)
        self.assertEqual(record["igdb_id"], 9876)
        self.assertEqual(record["max_players"], 6)
        self.assertEqual(
            record["max_players_source"],
            "Steam",
        )
        self.assertEqual(
            record["genres"],
            '["Adventure","Indie"]',
        )
        self.assertEqual(
            await database.get_games_missing_igdb_metadata(),
            [],
        )


if __name__ == "__main__":
    unittest.main()

