import unittest
from pathlib import Path
from unittest.mock import patch

from database import database


class PlayerMetadataSourceTests(
    unittest.IsolatedAsyncioTestCase
):
    async def asyncSetUp(self):
        await database.close_database()

        self.test_root = (
            Path.cwd()
            / ".test-tmp"
            / "player-source-tests"
        )
        self.test_root.mkdir(
            parents=True,
            exist_ok=True,
        )
        self.database_path = (
            self.test_root / "games.db"
        )

        for suffix in ("", "-shm", "-wal"):
            Path(
                f"{self.database_path}{suffix}"
            ).unlink(
                missing_ok=True
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
                self.test_root / "backups",
            ),
        )

        for patcher in self.patchers:
            patcher.start()

        await database.setup_database()

    async def asyncTearDown(self):
        await database.close_database()

        for patcher in reversed(self.patchers):
            patcher.stop()

    async def test_steam_can_upgrade_igdb_but_not_reverse(self):
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

    async def test_unsourced_existing_value_is_protected(self):
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


if __name__ == "__main__":
    unittest.main()
