import asyncio
import contextvars
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager, closing
from datetime import timedelta
from pathlib import Path

import aiosqlite

from settings import DATABASE_PATH
from utils.time_utils import (
    normalise_stored_datetime,
    utc_now,
    utc_now_iso,
)


DATABASE = str(
    DATABASE_PATH
)

BACKUP_DIRECTORY = (
    DATABASE_PATH.parent / "backups"
)
AUTOMATIC_BACKUP_PREFIX = "games-auto-"

ACTIVE_STATUS = "released"
WISHLIST_STATUS = "coming_soon"
DATABASE_POOL_SIZE = 4

PLAYER_SOURCE_PRIORITIES = {
    "igdb": 10,
    "steam": 20,
    "saved": 90,
    "manual": 100,
}

ONLINE_MULTIPLAYER_FILTER = """
    (
        INSTR(
            COALESCE(multiplayer_support_json, ''),
            '"online_'
        ) > 0
        OR INSTR(
            COALESCE(multiplayer_support_json, ''),
            '"split_screen_online"'
        ) > 0
    )
"""

LOCAL_MULTIPLAYER_FILTER = """
    (
        INSTR(
            COALESCE(multiplayer_support_json, ''),
            '"offline_'
        ) > 0
        OR INSTR(
            COALESCE(multiplayer_support_json, ''),
            '"lan_coop"'
        ) > 0
        OR INSTR(
            COALESCE(multiplayer_support_json, ''),
            '"split_screen"'
        ) > 0
    )
"""

LOCAL_ONLY_MULTIPLAYER_FILTER = f"""
    ({LOCAL_MULTIPLAYER_FILTER})
    AND NOT ({ONLINE_MULTIPLAYER_FILTER})
"""

MULTIPLAYER_WHEEL_FILTER = f"""
    (
        ({ONLINE_MULTIPLAYER_FILTER})
        OR (
            (
                max_players IS NULL
                OR max_players != 1
            )
            AND NOT ({LOCAL_ONLY_MULTIPLAYER_FILTER})
        )
    )
"""

SINGLEPLAYER_WHEEL_FILTER = f"""
    (
        ({LOCAL_ONLY_MULTIPLAYER_FILTER})
        OR (
            max_players = 1
            AND NOT ({ONLINE_MULTIPLAYER_FILTER})
        )
    )
"""


def _canonical_player_source(
    value,
) -> str | None:
    cleaned = str(
        value or ""
    ).strip()

    if not cleaned:
        return None

    known_sources = {
        "igdb": "IGDB",
        "steam": "Steam",
        "saved": "Saved",
        "manual": "Manual",
    }
    return known_sources.get(
        cleaned.casefold(),
        cleaned[:40],
    )


def _player_source_priority(
    value,
) -> int:
    return PLAYER_SOURCE_PRIORITIES.get(
        str(value or "").casefold(),
        50,
    )


def _clean_igdb_id(value) -> int | None:
    try:
        igdb_id = int(value)

    except (TypeError, ValueError):
        return None

    return igdb_id if igdb_id > 0 else None


def _clean_json_metadata(
    value,
    expected_type,
) -> str | None:
    if value is None:
        return None

    if isinstance(value, str):
        try:
            value = json.loads(value)

        except (TypeError, ValueError):
            return None

    if not isinstance(value, expected_type):
        return None

    if expected_type is list:
        cleaned = []

        for item in value:
            text = str(item or "").strip()

            if (
                text
                and text.casefold() not in {
                    existing.casefold()
                    for existing in cleaned
                }
            ):
                cleaned.append(text[:100])

        value = cleaned

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    except (TypeError, ValueError):
        return None


def _complete_multiplayer_support_limits(
    value,
    max_players,
) -> str | None:
    """Use a trusted overall limit for supported modes."""

    if value is None:
        return None

    if isinstance(value, str):
        try:
            support = json.loads(value)

        except (TypeError, ValueError):
            return value

    elif isinstance(value, dict):
        support = dict(value)

    else:
        return value

    if not isinstance(support, dict):
        return value

    try:
        player_limit = int(max_players)

    except (TypeError, ValueError):
        player_limit = None

    if (
        player_limit is not None
        and 2 <= player_limit <= 100
        and not support.get("variable_capacity")
        and not support.get("mmo")
    ):
        support.pop("capacity_tba", None)

        for flag_name, count_name in (
            ("online_coop", "online_coop_max"),
            (
                "online_multiplayer",
                "online_max",
            ),
            ("offline_coop", "offline_coop_max"),
            (
                "offline_multiplayer",
                "offline_max",
            ),
        ):
            if (
                support.get(flag_name)
                and not support.get(count_name)
            ):
                support[count_name] = player_limit

    return _clean_json_metadata(
        support,
        dict,
    )


def _multiplayer_support_flags(
    value,
) -> tuple[bool, bool]:
    if isinstance(value, str):
        try:
            value = json.loads(value)

        except (TypeError, ValueError):
            return False, False

    if not isinstance(value, dict):
        return False, False

    has_online = any(
        key.startswith("online_")
        or key == "split_screen_online"
        for key in value
    )
    has_local = any(
        key.startswith("offline_")
        or key in {
            "lan_coop",
            "split_screen",
        }
        for key in value
    )
    return has_online, has_local


def _is_local_only_multiplayer(value) -> bool:
    has_online, has_local = (
        _multiplayer_support_flags(value)
    )
    return has_local and not has_online


def _belongs_on_singleplayer_wheel(
    max_players,
    multiplayer_support,
) -> bool:
    has_online, has_local = (
        _multiplayer_support_flags(
            multiplayer_support
        )
    )

    if has_online:
        return False

    if has_local:
        return True

    return max_players == 1


class SQLiteConnectionPool:
    """A small pool of long-lived aiosqlite connections."""

    def __init__(
        self,
        database_path: str,
        *,
        size: int = DATABASE_POOL_SIZE,
    ) -> None:
        self.database_path = database_path
        self.size = max(1, size)
        self._available = asyncio.Queue(
            maxsize=self.size
        )
        self._connections = []
        self._closing = False

    async def start(self) -> None:
        try:
            for _ in range(self.size):
                db = await aiosqlite.connect(
                    self.database_path,
                    timeout=30,
                )
                db.row_factory = aiosqlite.Row

                await db.execute(
                    "PRAGMA journal_mode=WAL"
                )
                await db.execute(
                    "PRAGMA busy_timeout=30000"
                )
                await db.execute(
                    "PRAGMA foreign_keys=ON"
                )

                self._connections.append(db)
                self._available.put_nowait(db)

        except Exception:
            await self.close()
            raise

    @asynccontextmanager
    async def acquire(self):
        if self._closing:
            raise RuntimeError(
                "The SQLite connection pool is closing."
            )

        db = await self._available.get()

        try:
            yield db

        finally:
            # No unfinished transaction should leak into the
            # next command that borrows this connection.
            if db.in_transaction:
                await db.rollback()

            if not self._closing:
                self._available.put_nowait(db)

    async def close(self) -> None:
        self._closing = True

        if self._connections:
            await asyncio.gather(
                *(
                    db.close()
                    for db in self._connections
                ),
                return_exceptions=True,
            )

        self._connections.clear()


_database_pool: SQLiteConnectionPool | None = None
_active_database_connection = contextvars.ContextVar(
    "active_database_connection",
    default=None,
)
_active_write_batch = contextvars.ContextVar(
    "active_write_batch",
    default=None,
)


async def open_database() -> None:
    """Open the bot's reusable SQLite connections once."""

    global _database_pool

    if _database_pool is not None:
        return

    pool = SQLiteConnectionPool(DATABASE)
    await pool.start()
    _database_pool = pool


async def close_database() -> None:
    """Close all reusable SQLite connections on shutdown."""

    global _database_pool

    pool = _database_pool
    _database_pool = None

    if pool is not None:
        await pool.close()


@asynccontextmanager
async def database_connection():
    """Borrow one of the bot's persistent SQLite connections."""

    if _database_pool is None:
        raise RuntimeError(
            "Database is not open. Call setup_database() first."
        )

    current_task = asyncio.current_task()
    active_connection = (
        _active_database_connection.get()
    )

    if (
        active_connection is not None
        and active_connection[1] is current_task
    ):
        yield active_connection[0]
        return

    async with _database_pool.acquire() as db:
        token = _active_database_connection.set(
            (db, current_task)
        )

        try:
            yield db

        finally:
            _active_database_connection.reset(token)


@asynccontextmanager
async def batched_database_writes(
    *,
    batch_size: int = 25,
):
    """Commit a run of related writes in small, safe batches."""

    current_task = asyncio.current_task()
    existing_batch = _active_write_batch.get()

    if (
        existing_batch is not None
        and existing_batch["task"] is current_task
    ):
        yield
        return

    async with database_connection() as db:
        batch = {
            "task": current_task,
            "db": db,
            "pending": 0,
            "batch_size": max(1, int(batch_size)),
        }
        token = _active_write_batch.set(batch)

        try:
            yield

        except BaseException:
            if db.in_transaction:
                await db.rollback()
            raise

        else:
            if db.in_transaction:
                await db.commit()

        finally:
            _active_write_batch.reset(token)


async def _commit_database_write(db) -> None:
    batch = _active_write_batch.get()

    if (
        batch is None
        or batch["task"] is not asyncio.current_task()
        or batch["db"] is not db
    ):
        await db.commit()
        return

    batch["pending"] += 1

    if batch["pending"] >= batch["batch_size"]:
        await db.commit()
        batch["pending"] = 0


def _steam_app_id(store_link: str | None) -> str | None:
    if not store_link:
        return None

    match = re.search(
        r"store\.steampowered\.com/(?:agecheck/)?app/(\d+)",
        store_link,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def _steam_image_from_url(store_link: str | None) -> str | None:
    app_id = _steam_app_id(store_link)

    if not app_id:
        return None

    return (
        "https://cdn.cloudflare.steamstatic.com/"
        f"steam/apps/{app_id}/header.jpg"
    )


def _clean_optional_text(value) -> str | None:
    if value is None:
        return None

    cleaned_value = str(value).strip()
    return cleaned_value or None


def _clean_availability_status(
    value,
    coming_soon: bool = False,
) -> str:
    if coming_soon:
        return WISHLIST_STATUS

    cleaned_value = (
        _clean_optional_text(value)
        or ACTIVE_STATUS
    ).casefold()

    if cleaned_value == WISHLIST_STATUS:
        return WISHLIST_STATUS

    return ACTIVE_STATUS


async def _add_column_if_missing(
    db,
    table_name: str,
    existing_columns: list[str],
    column_name: str,
    declaration: str,
) -> None:
    if column_name in existing_columns:
        return

    await db.execute(
        f"""
        ALTER TABLE {table_name}
        ADD COLUMN {column_name} {declaration}
        """
    )
    existing_columns.append(column_name)


async def _normalise_timestamp_columns(
    db,
) -> int:
    """Migrate legacy naive local timestamps to explicit UTC."""

    timestamp_columns = (
        ("games", "last_played"),
        ("games", "added_date"),
        ("games", "last_link_check"),
        ("game_history", "played_date"),
        ("store_replacements", "replaced_at"),
    )
    updated = 0

    for table_name, column_name in timestamp_columns:
        cursor = await db.execute(
            f"""
            SELECT rowid, {column_name}
            FROM {table_name}
            WHERE
                {column_name} IS NOT NULL
                AND {column_name} != ''
            """
        )
        rows = await cursor.fetchall()
        replacements = []

        for row_id, raw_value in rows:
            normalised = normalise_stored_datetime(
                raw_value
            )

            if (
                normalised is not None
                and normalised != str(raw_value)
            ):
                replacements.append(
                    (normalised, row_id)
                )

        if replacements:
            await db.executemany(
                f"""
                UPDATE {table_name}
                SET {column_name} = ?
                WHERE rowid = ?
                """,
                replacements,
            )
            updated += len(replacements)

    return updated


async def setup_database() -> None:
    await open_database()

    async with database_connection() as db:
        # WAL lets reads and writes happen concurrently
        # instead of blocking each other, which matters
        # once several commands/background checks are
        # hitting the database at the same time.
        await db.execute(
            "PRAGMA journal_mode=WAL"
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE,
                store_link TEXT,
                store TEXT,
                suggested_by TEXT,
                times_played INTEGER DEFAULT 0,
                last_played TEXT,
                added_date TEXT,
                image_url TEXT,
                external_id TEXT,
                link_status TEXT DEFAULT 'unknown',
                http_status INTEGER,
                last_link_check TEXT,
                availability_status TEXT DEFAULT 'released',
                release_date TEXT,
                coming_soon INTEGER DEFAULT 0,
                max_players INTEGER,
                max_players_source TEXT,
                igdb_id INTEGER,
                multiplayer_support_json TEXT,
                genres_json TEXT,
                themes_json TEXT,
                game_modes_json TEXT
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS game_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER,
                played_date TEXT,
                locked_by TEXT,
                FOREIGN KEY(game_id) REFERENCES games(id)
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS store_replacements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store TEXT NOT NULL,
                old_external_id TEXT,
                old_store_link TEXT,
                old_name TEXT,
                game_id INTEGER,
                new_external_id TEXT,
                new_store_link TEXT,
                new_name TEXT,
                replaced_at TEXT NOT NULL,
                UNIQUE(store, old_external_id),
                FOREIGN KEY(game_id) REFERENCES games(id)
            )
            """
        )

        game_columns = []

        async with db.execute(
            "PRAGMA table_info(games)"
        ) as cursor:
            async for row in cursor:
                game_columns.append(row[1])

        if "store_link" not in game_columns:
            await db.execute(
                """
                ALTER TABLE games
                ADD COLUMN store_link TEXT
                """
            )

            if "steam_link" in game_columns:
                await db.execute(
                    """
                    UPDATE games
                    SET store_link = steam_link
                    WHERE store_link IS NULL
                    """
                )

            game_columns.append("store_link")

        columns_to_add = (
            ("store", "TEXT DEFAULT 'Steam'"),
            ("times_played", "INTEGER DEFAULT 0"),
            ("last_played", "TEXT"),
            ("added_date", "TEXT"),
            ("image_url", "TEXT"),
            ("external_id", "TEXT"),
            ("link_status", "TEXT DEFAULT 'unknown'"),
            ("http_status", "INTEGER"),
            ("last_link_check", "TEXT"),
            (
                "availability_status",
                "TEXT DEFAULT 'released'",
            ),
            ("release_date", "TEXT"),
            ("coming_soon", "INTEGER DEFAULT 0"),
            ("max_players", "INTEGER"),
            ("max_players_source", "TEXT"),
            ("igdb_id", "INTEGER"),
            ("multiplayer_support_json", "TEXT"),
            ("genres_json", "TEXT"),
            ("themes_json", "TEXT"),
            ("game_modes_json", "TEXT"),
        )

        for column_name, declaration in columns_to_add:
            await _add_column_if_missing(
                db,
                "games",
                game_columns,
                column_name,
                declaration,
            )

        await db.execute(
            """
            UPDATE games
            SET max_players_source = 'Saved'
            WHERE
                max_players IS NOT NULL
                AND (
                    max_players_source IS NULL
                    OR max_players_source = ''
                )
            """
        )

        history_columns = []

        async with db.execute(
            "PRAGMA table_info(game_history)"
        ) as cursor:
            async for row in cursor:
                history_columns.append(row[1])

        await _add_column_if_missing(
            db,
            "game_history",
            history_columns,
            "locked_by",
            "TEXT",
        )

        await db.execute(
            """
            UPDATE games
            SET
                availability_status = 'released',
                coming_soon = 0
            WHERE
                availability_status IS NULL
                OR availability_status = ''
            """
        )

        cursor = await db.execute(
            """
            SELECT id, store_link
            FROM games
            WHERE
                store = 'Steam'
                AND (
                    external_id IS NULL
                    OR external_id = ''
                )
            """
        )

        for game_id, store_link in await cursor.fetchall():
            app_id = _steam_app_id(store_link)

            if not app_id:
                continue

            canonical_link = (
                "https://store.steampowered.com/"
                f"app/{app_id}/"
            )

            await db.execute(
                """
                UPDATE games
                SET
                    external_id = ?,
                    store_link = ?
                WHERE id = ?
                """,
                (
                    app_id,
                    canonical_link,
                    game_id,
                ),
            )

        cursor = await db.execute(
            """
            SELECT id, store_link
            FROM games
            WHERE
                (
                    image_url IS NULL
                    OR image_url = ''
                )
                AND store = 'Steam'
            """
        )

        for game_id, store_link in await cursor.fetchall():
            image_url = _steam_image_from_url(store_link)

            if image_url:
                await db.execute(
                    """
                    UPDATE games
                    SET image_url = ?
                    WHERE id = ?
                    """,
                    (
                        image_url,
                        game_id,
                    ),
                )

        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_games_store_external_id
            ON games(store, external_id)
            """
        )

        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_games_store_link
            ON games(store_link)
            """
        )

        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_games_availability
            ON games(availability_status)
            """
        )

        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_games_name_nocase
            ON games(name COLLATE NOCASE)
            """
        )

        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_games_igdb_id
            ON games(igdb_id)
            """
        )

        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_game_history_game_id
            ON game_history(game_id)
            """
        )

        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_game_history_played_date
            ON game_history(played_date DESC)
            """
        )

        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_store_replacements_old_link
            ON store_replacements(old_store_link)
            """
        )

        await _normalise_timestamp_columns(db)

        await db.commit()

        cursor = await db.execute(
            "PRAGMA quick_check"
        )
        integrity_result = await cursor.fetchone()

        if (
            not integrity_result
            or integrity_result[0] != "ok"
        ):
            raise RuntimeError(
                "SQLite quick_check failed during startup."
            )


async def _find_existing_game(
    db,
    *,
    name: str | None,
    store: str | None,
    store_link: str | None,
    source_link: str | None,
    external_id: str | None,
):
    select_columns = """
        id,
        name,
        store_link,
        store,
        image_url,
        external_id,
        link_status,
        http_status,
        availability_status,
        release_date,
        coming_soon,
        max_players,
        max_players_source,
        igdb_id,
        multiplayer_support_json,
        genres_json,
        themes_json,
        game_modes_json
    """

    if external_id and store:
        cursor = await db.execute(
            f"""
            SELECT {select_columns}
            FROM games
            WHERE
                store = ?
                AND external_id = ?
            LIMIT 1
            """,
            (
                store,
                external_id,
            ),
        )

        existing = await cursor.fetchone()

        if existing:
            return existing

    candidate_links = {
        link
        for link in (
            store_link,
            source_link,
        )
        if link
    }

    for candidate_link in candidate_links:
        cursor = await db.execute(
            f"""
            SELECT {select_columns}
            FROM games
            WHERE store_link = ?
            LIMIT 1
            """,
            (candidate_link,),
        )

        existing = await cursor.fetchone()

        if existing:
            return existing

    if name:
        cursor = await db.execute(
            f"""
            SELECT {select_columns}
            FROM games
            WHERE LOWER(name) = LOWER(?)
            LIMIT 1
            """,
            (name,),
        )

        return await cursor.fetchone()

    return None


async def get_store_replacement(
    *,
    store: str,
    external_id: str | None = None,
    store_link: str | None = None,
) -> dict | None:
    clean_store = _clean_optional_text(
        store
    )

    clean_external_id = _clean_optional_text(
        external_id
    )

    clean_store_link = _clean_optional_text(
        store_link
    )

    if not clean_store:
        return None

    async with database_connection() as db:
        cursor = await db.execute(
            """
            SELECT
                old_external_id,
                old_store_link,
                old_name,
                game_id,
                new_external_id,
                new_store_link,
                new_name,
                replaced_at
            FROM store_replacements
            WHERE
                store = ?
                AND (
                    (
                        ? IS NOT NULL
                        AND old_external_id = ?
                    )
                    OR (
                        ? IS NOT NULL
                        AND old_store_link = ?
                    )
                )
            ORDER BY id DESC
            LIMIT 1
            """,
            (
                clean_store,
                clean_external_id,
                clean_external_id,
                clean_store_link,
                clean_store_link,
            ),
        )

        row = await cursor.fetchone()

    if not row:
        return None

    return {
        "old_external_id": row[0],
        "old_store_link": row[1],
        "old_name": row[2],
        "game_id": row[3],
        "new_external_id": row[4],
        "new_store_link": row[5],
        "new_name": row[6],
        "replaced_at": row[7],
    }


async def save_store_replacement(
    *,
    store: str,
    old_external_id: str | None,
    old_store_link: str | None,
    old_name: str | None,
    game_id: int,
    new_external_id: str | None,
    new_store_link: str | None,
    new_name: str | None,
) -> None:
    replaced_at = utc_now_iso()

    async with database_connection() as db:
        await db.execute(
            """
            INSERT INTO store_replacements (
                store,
                old_external_id,
                old_store_link,
                old_name,
                game_id,
                new_external_id,
                new_store_link,
                new_name,
                replaced_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(store, old_external_id)
            DO UPDATE SET
                old_store_link = excluded.old_store_link,
                old_name = excluded.old_name,
                game_id = excluded.game_id,
                new_external_id = excluded.new_external_id,
                new_store_link = excluded.new_store_link,
                new_name = excluded.new_name,
                replaced_at = excluded.replaced_at
            """,
            (
                store,
                _clean_optional_text(
                    old_external_id
                ),
                _clean_optional_text(
                    old_store_link
                ),
                _clean_optional_text(
                    old_name
                ),
                game_id,
                _clean_optional_text(
                    new_external_id
                ),
                _clean_optional_text(
                    new_store_link
                ),
                _clean_optional_text(
                    new_name
                ),
                replaced_at,
            ),
        )

        await db.commit()


async def upgrade_obsolete_steam_demo(
    *,
    old_app_id: str,
    old_store_link: str,
    old_name: str | None,
    new_app_id: str,
    new_store_link: str,
    new_name: str,
    new_image_url: str | None = None,
    release_date: str | None = None,
) -> dict:
    """
    Replace an obsolete Steam demo record with its released
    full-game record while preserving history and play data.

    If the full game already exists separately, its history
    and play counts are merged into the old demo row before
    the duplicate row is removed.
    """

    checked_at = utc_now_iso()

    async with database_connection() as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            """
            SELECT *
            FROM games
            WHERE
                LOWER(
                    COALESCE(store, '')
                ) LIKE '%steam%'
                AND (
                    external_id = ?
                    OR store_link LIKE ?
                )
            ORDER BY id
            LIMIT 1
            """,
            (
                str(old_app_id),
                f"%/app/{old_app_id}/%",
            ),
        )

        old_game = await cursor.fetchone()

        if not old_game:
            return {
                "status": "old_record_not_found",
            }

        old_game_id = old_game["id"]

        duplicate_cursor = await db.execute(
            """
            SELECT *
            FROM games
            WHERE
                id != ?
                AND LOWER(
                    COALESCE(store, '')
                ) LIKE '%steam%'
                AND (
                    external_id = ?
                    OR store_link LIKE ?
                    OR LOWER(name) = LOWER(?)
                )
            ORDER BY id
            LIMIT 1
            """,
            (
                old_game_id,
                str(new_app_id),
                f"%/app/{new_app_id}/%",
                new_name,
            ),
        )

        duplicate = await duplicate_cursor.fetchone()

        duplicate_merged = False

        combined_times_played = int(
            old_game["times_played"] or 0
        )

        last_played_values = [
            value
            for value in (
                old_game["last_played"],
            )
            if value
        ]

        if duplicate:
            duplicate_merged = True

            combined_times_played += int(
                duplicate["times_played"] or 0
            )

            if duplicate["last_played"]:
                last_played_values.append(
                    duplicate["last_played"]
                )

            await db.execute(
                """
                UPDATE game_history
                SET game_id = ?
                WHERE game_id = ?
                """,
                (
                    old_game_id,
                    duplicate["id"],
                ),
            )

            await db.execute(
                """
                DELETE FROM games
                WHERE id = ?
                """,
                (
                    duplicate["id"],
                ),
            )

        combined_last_played = (
            max(last_played_values)
            if last_played_values
            else None
        )

        final_image_url = (
            _clean_optional_text(
                new_image_url
            )
            or old_game["image_url"]
        )

        await db.execute(
            """
            UPDATE games
            SET
                name = ?,
                store_link = ?,
                store = 'Steam',
                image_url = ?,
                external_id = ?,
                times_played = ?,
                last_played = ?,
                link_status = 'live',
                http_status = 200,
                last_link_check = ?,
                availability_status = 'released',
                release_date = ?,
                coming_soon = 0
            WHERE id = ?
            """,
            (
                new_name,
                new_store_link,
                final_image_url,
                str(new_app_id),
                combined_times_played,
                combined_last_played,
                checked_at,
                _clean_optional_text(
                    release_date
                ),
                old_game_id,
            ),
        )

        await db.execute(
            """
            INSERT INTO store_replacements (
                store,
                old_external_id,
                old_store_link,
                old_name,
                game_id,
                new_external_id,
                new_store_link,
                new_name,
                replaced_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(store, old_external_id)
            DO UPDATE SET
                old_store_link = excluded.old_store_link,
                old_name = excluded.old_name,
                game_id = excluded.game_id,
                new_external_id = excluded.new_external_id,
                new_store_link = excluded.new_store_link,
                new_name = excluded.new_name,
                replaced_at = excluded.replaced_at
            """,
            (
                "Steam",
                str(old_app_id),
                old_store_link,
                (
                    _clean_optional_text(
                        old_name
                    )
                    or old_game["name"]
                ),
                old_game_id,
                str(new_app_id),
                new_store_link,
                new_name,
                checked_at,
            ),
        )

        await db.commit()

        return {
            "status": "upgraded",
            "game_id": old_game_id,
            "old_name": (
                _clean_optional_text(
                    old_name
                )
                or old_game["name"]
            ),
            "new_name": new_name,
            "duplicate_merged": (
                duplicate_merged
            ),
        }


async def finalise_obsolete_steam_demo(
    *,
    old_app_id: str,
    old_store_link: str,
    old_name: str,
    new_app_id: str,
    new_store_link: str,
    new_name: str,
    new_image_url: str | None = None,
    release_date: str | None = None,
) -> dict:
    """
    Point an obsolete demo link at an existing full-game
    record and merge any surviving demo row into it.
    """

    checked_at = utc_now_iso()

    async with database_connection() as db:
        db.row_factory = aiosqlite.Row

        full_cursor = await db.execute(
            """
            SELECT *
            FROM games
            WHERE
                LOWER(
                    COALESCE(store, '')
                ) LIKE '%steam%'
                AND (
                    external_id = ?
                    OR store_link LIKE ?
                    OR LOWER(name) = LOWER(?)
                )
            ORDER BY
                CASE
                    WHEN external_id = ? THEN 0
                    WHEN store_link LIKE ? THEN 1
                    ELSE 2
                END,
                id
            LIMIT 1
            """,
            (
                str(new_app_id),
                f"%/app/{new_app_id}/%",
                new_name,
                str(new_app_id),
                f"%/app/{new_app_id}/%",
            ),
        )

        full_game = await full_cursor.fetchone()

        if not full_game:
            return {
                "status": "full_record_not_found",
            }

        full_game_id = full_game["id"]

        old_cursor = await db.execute(
            """
            SELECT *
            FROM games
            WHERE
                id != ?
                AND LOWER(
                    COALESCE(store, '')
                ) LIKE '%steam%'
                AND (
                    external_id = ?
                    OR store_link LIKE ?
                )
            ORDER BY id
            LIMIT 1
            """,
            (
                full_game_id,
                str(old_app_id),
                f"%/app/{old_app_id}/%",
            ),
        )

        old_game = await old_cursor.fetchone()

        combined_times_played = int(
            full_game["times_played"] or 0
        )

        last_played_values = [
            value
            for value in (
                full_game["last_played"],
            )
            if value
        ]

        old_record_merged = False

        if old_game:
            old_record_merged = True

            combined_times_played += int(
                old_game["times_played"] or 0
            )

            if old_game["last_played"]:
                last_played_values.append(
                    old_game["last_played"]
                )

            await db.execute(
                """
                UPDATE game_history
                SET game_id = ?
                WHERE game_id = ?
                """,
                (
                    full_game_id,
                    old_game["id"],
                ),
            )

            await db.execute(
                """
                DELETE FROM games
                WHERE id = ?
                """,
                (
                    old_game["id"],
                ),
            )

        combined_last_played = (
            max(last_played_values)
            if last_played_values
            else None
        )

        final_image_url = (
            _clean_optional_text(
                new_image_url
            )
            or full_game["image_url"]
        )

        await db.execute(
            """
            UPDATE games
            SET
                name = ?,
                store_link = ?,
                store = 'Steam',
                image_url = ?,
                external_id = ?,
                times_played = ?,
                last_played = ?,
                link_status = 'live',
                http_status = 200,
                last_link_check = ?,
                availability_status = 'released',
                release_date = ?,
                coming_soon = 0
            WHERE id = ?
            """,
            (
                new_name,
                new_store_link,
                final_image_url,
                str(new_app_id),
                combined_times_played,
                combined_last_played,
                checked_at,
                _clean_optional_text(
                    release_date
                ),
                full_game_id,
            ),
        )

        await db.execute(
            """
            INSERT INTO store_replacements (
                store,
                old_external_id,
                old_store_link,
                old_name,
                game_id,
                new_external_id,
                new_store_link,
                new_name,
                replaced_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(store, old_external_id)
            DO UPDATE SET
                old_store_link = excluded.old_store_link,
                old_name = excluded.old_name,
                game_id = excluded.game_id,
                new_external_id = excluded.new_external_id,
                new_store_link = excluded.new_store_link,
                new_name = excluded.new_name,
                replaced_at = excluded.replaced_at
            """,
            (
                "Steam",
                str(old_app_id),
                old_store_link,
                old_name,
                full_game_id,
                str(new_app_id),
                new_store_link,
                new_name,
                checked_at,
            ),
        )

        await db.commit()

        return {
            "status": "upgraded",
            "game_id": full_game_id,
            "old_record_merged": (
                old_record_merged
            ),
            "old_name": old_name,
            "new_name": new_name,
        }


async def sync_game(
    *,
    name: str,
    store_link: str,
    store: str,
    suggested_by: str,
    image_url: str | None = None,
    source_link: str | None = None,
    external_id: str | None = None,
    link_status: str = "unknown",
    http_status: int | None = None,
    availability_status: str = ACTIVE_STATUS,
    release_date: str | None = None,
    coming_soon: bool = False,
    max_players: int | None = None,
    max_players_source: str | None = None,
    igdb_id: int | None = None,
    multiplayer_support=None,
    genres=None,
    themes=None,
    game_modes=None,
) -> str:
    clean_name = (
        _clean_optional_text(name)
        or "Unknown Game"
    )
    clean_store_link = _clean_optional_text(
        store_link
    )
    clean_store = (
        _clean_optional_text(store)
        or "Unknown Store"
    )
    clean_suggester = (
        _clean_optional_text(suggested_by)
        or "Unknown"
    )
    clean_image_url = _clean_optional_text(
        image_url
    )
    clean_source_link = _clean_optional_text(
        source_link
    )
    clean_external_id = _clean_optional_text(
        external_id
    )
    clean_link_status = (
        _clean_optional_text(link_status)
        or "unknown"
    )
    clean_release_date = _clean_optional_text(
        release_date
    )
    clean_availability = (
        _clean_availability_status(
            availability_status,
            coming_soon=bool(coming_soon),
        )
    )
    clean_coming_soon = int(
        clean_availability
        == WISHLIST_STATUS
    )

    try:
        clean_max_players = int(
            max_players
        )

    except (
        TypeError,
        ValueError,
    ):
        clean_max_players = None

    if (
        clean_max_players is not None
        and not 1 <= clean_max_players <= 100
    ):
        clean_max_players = None

    clean_max_players_source = (
        _canonical_player_source(
            max_players_source
        )
        if clean_max_players is not None
        else None
    )

    if (
        clean_max_players is not None
        and clean_max_players_source is None
    ):
        clean_max_players_source = "Saved"

    clean_igdb_id = _clean_igdb_id(igdb_id)
    clean_multiplayer_support = _clean_json_metadata(
        multiplayer_support,
        dict,
    )
    clean_multiplayer_support = (
        _complete_multiplayer_support_limits(
            clean_multiplayer_support,
            clean_max_players,
        )
    )
    clean_genres = _clean_json_metadata(
        genres,
        list,
    )
    clean_themes = _clean_json_metadata(
        themes,
        list,
    )
    clean_game_modes = _clean_json_metadata(
        game_modes,
        list,
    )

    checked_at = utc_now_iso()

    async with database_connection() as db:
        existing = await _find_existing_game(
            db,
            name=clean_name,
            store=clean_store,
            store_link=clean_store_link,
            source_link=clean_source_link,
            external_id=clean_external_id,
        )

        if existing:
            (
                game_id,
                old_name,
                old_store_link,
                old_store,
                old_image_url,
                old_external_id,
                old_link_status,
                old_http_status,
                old_availability,
                old_release_date,
                old_coming_soon,
                old_max_players,
                old_max_players_source,
                old_igdb_id,
                old_multiplayer_support,
                old_genres,
                old_themes,
                old_game_modes,
            ) = existing

            old_availability = (
                old_availability
                or ACTIVE_STATUS
            )

            new_name = (
                clean_name
                if clean_name != "Unknown Game"
                else old_name
            )
            new_store_link = (
                clean_store_link
                or old_store_link
            )
            new_store = clean_store or old_store
            new_image_url = (
                clean_image_url
                or old_image_url
            )
            new_external_id = (
                clean_external_id
                or old_external_id
            )
            new_release_date = (
                clean_release_date
                if clean_release_date is not None
                else old_release_date
            )
            new_igdb_id = (
                clean_igdb_id
                if clean_igdb_id is not None
                else old_igdb_id
            )
            new_multiplayer_support = (
                clean_multiplayer_support
                if clean_multiplayer_support is not None
                else old_multiplayer_support
            )
            new_genres = (
                clean_genres
                if clean_genres is not None
                else old_genres
            )
            new_themes = (
                clean_themes
                if clean_themes is not None
                else old_themes
            )
            new_game_modes = (
                clean_game_modes
                if clean_game_modes is not None
                else old_game_modes
            )

            canonical_old_source = (
                _canonical_player_source(
                    old_max_players_source
                )
                if old_max_players is not None
                else None
            )

            if (
                old_max_players is not None
                and canonical_old_source is None
            ):
                canonical_old_source = "Saved"

            if old_max_players is None:
                new_max_players = clean_max_players
                new_max_players_source = (
                    clean_max_players_source
                )

            elif clean_max_players is None:
                new_max_players = old_max_players
                new_max_players_source = (
                    canonical_old_source
                )

            elif (
                clean_max_players_source
                == canonical_old_source
                or _player_source_priority(
                    clean_max_players_source
                )
                > _player_source_priority(
                    canonical_old_source
                )
            ):
                new_max_players = clean_max_players
                new_max_players_source = (
                    clean_max_players_source
                )

            else:
                new_max_players = old_max_players
                new_max_players_source = (
                    canonical_old_source
                )

            new_multiplayer_support = (
                _complete_multiplayer_support_limits(
                    new_multiplayer_support,
                    new_max_players,
                )
            )

            changed = any(
                (
                    new_name != old_name,
                    new_store_link
                    != old_store_link,
                    new_store != old_store,
                    new_image_url != old_image_url,
                    new_external_id
                    != old_external_id,
                    clean_link_status
                    != old_link_status,
                    http_status != old_http_status,
                    clean_availability
                    != old_availability,
                    new_release_date
                    != old_release_date,
                    clean_coming_soon
                    != int(old_coming_soon or 0),
                    new_max_players
                    != old_max_players,
                    new_max_players_source
                    != old_max_players_source,
                    new_igdb_id != old_igdb_id,
                    new_multiplayer_support
                    != old_multiplayer_support,
                    new_genres != old_genres,
                    new_themes != old_themes,
                    new_game_modes != old_game_modes,
                )
            )

            if not changed:
                # Preserve the useful verification time
                # without rewriting every metadata column
                # during an unchanged sync.
                await db.execute(
                    """
                    UPDATE games
                    SET last_link_check = ?
                    WHERE id = ?
                    """,
                    (
                        checked_at,
                        game_id,
                    ),
                )

                await _commit_database_write(db)

                if clean_link_status == "dead":
                    return "unavailable"

                if (
                    clean_availability
                    == WISHLIST_STATUS
                ):
                    return "wishlist_unchanged"

                return "unchanged"

            try:
                await db.execute(
                    """
                    UPDATE games
                    SET
                        name = ?,
                        store_link = ?,
                        store = ?,
                        image_url = ?,
                        external_id = ?,
                        link_status = ?,
                        http_status = ?,
                        last_link_check = ?,
                        availability_status = ?,
                        release_date = ?,
                        coming_soon = ?,
                        max_players = ?,
                        max_players_source = ?,
                        igdb_id = ?,
                        multiplayer_support_json = ?,
                        genres_json = ?,
                        themes_json = ?,
                        game_modes_json = ?
                    WHERE id = ?
                    """,
                    (
                        new_name,
                        new_store_link,
                        new_store,
                        new_image_url,
                        new_external_id,
                        clean_link_status,
                        http_status,
                        checked_at,
                        clean_availability,
                        new_release_date,
                        clean_coming_soon,
                        new_max_players,
                        new_max_players_source,
                        new_igdb_id,
                        new_multiplayer_support,
                        new_genres,
                        new_themes,
                        new_game_modes,
                        game_id,
                    ),
                )

            except aiosqlite.IntegrityError:
                await db.execute(
                    """
                    UPDATE games
                    SET
                        store_link = ?,
                        store = ?,
                        image_url = ?,
                        external_id = ?,
                        link_status = ?,
                        http_status = ?,
                        last_link_check = ?,
                        availability_status = ?,
                        release_date = ?,
                        coming_soon = ?,
                        max_players = ?,
                        max_players_source = ?,
                        igdb_id = ?,
                        multiplayer_support_json = ?,
                        genres_json = ?,
                        themes_json = ?,
                        game_modes_json = ?
                    WHERE id = ?
                    """,
                    (
                        new_store_link,
                        new_store,
                        new_image_url,
                        new_external_id,
                        clean_link_status,
                        http_status,
                        checked_at,
                        clean_availability,
                        new_release_date,
                        clean_coming_soon,
                        new_max_players,
                        new_max_players_source,
                        new_igdb_id,
                        new_multiplayer_support,
                        new_genres,
                        new_themes,
                        new_game_modes,
                        game_id,
                    ),
                )
                changed = True

            await _commit_database_write(db)

            if clean_link_status == "dead":
                return "unavailable"

            if (
                old_availability
                == WISHLIST_STATUS
                and clean_availability
                == ACTIVE_STATUS
            ):
                return "promoted"

            if (
                old_availability
                != WISHLIST_STATUS
                and clean_availability
                == WISHLIST_STATUS
            ):
                return "moved_to_wishlist"

            old_singleplayer_wheel = (
                _belongs_on_singleplayer_wheel(
                    old_max_players,
                    old_multiplayer_support,
                )
            )
            new_singleplayer_wheel = (
                _belongs_on_singleplayer_wheel(
                    new_max_players,
                    new_multiplayer_support,
                )
            )

            if (
                clean_availability == ACTIVE_STATUS
                and not old_singleplayer_wheel
                and new_singleplayer_wheel
            ):
                return "moved_to_singleplayer"

            if (
                clean_availability == ACTIVE_STATUS
                and old_singleplayer_wheel
                and not new_singleplayer_wheel
            ):
                return "moved_to_multiplayer"

            if changed:
                if (
                    clean_availability
                    == WISHLIST_STATUS
                ):
                    return "wishlist_updated"

                return "updated"

            if (
                clean_availability
                == WISHLIST_STATUS
            ):
                return "wishlist_unchanged"

            return "unchanged"

        if clean_link_status == "dead":
            return "unavailable"

        try:
            await db.execute(
                """
                INSERT INTO games (
                    name,
                    store_link,
                    store,
                    suggested_by,
                    times_played,
                    added_date,
                    image_url,
                    external_id,
                    link_status,
                    http_status,
                    last_link_check,
                    availability_status,
                    release_date,
                    coming_soon,
                    max_players,
                    max_players_source,
                    igdb_id,
                    multiplayer_support_json,
                    genres_json,
                    themes_json,
                    game_modes_json
                )
                VALUES (
                    ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?
                )
                """,
                (
                    clean_name,
                    clean_store_link,
                    clean_store,
                    clean_suggester,
                    checked_at,
                    clean_image_url,
                    clean_external_id,
                    clean_link_status,
                    http_status,
                    checked_at,
                    clean_availability,
                    clean_release_date,
                    clean_coming_soon,
                    clean_max_players,
                    clean_max_players_source,
                    clean_igdb_id,
                    clean_multiplayer_support,
                    clean_genres,
                    clean_themes,
                    clean_game_modes,
                ),
            )

            await _commit_database_write(db)

            if (
                clean_availability
                == WISHLIST_STATUS
            ):
                return "wishlisted"

            if _belongs_on_singleplayer_wheel(
                clean_max_players,
                clean_multiplayer_support,
            ):
                return "singleplayer_added"

            return "added"

        except aiosqlite.IntegrityError:
            return "unchanged"


async def add_game(
    name: str,
    store_link: str,
    store: str,
    suggested_by: str,
    image_url: str | None = None,
    source_link: str | None = None,
    external_id: str | None = None,
    link_status: str = "unknown",
    http_status: int | None = None,
    availability_status: str = ACTIVE_STATUS,
    release_date: str | None = None,
    coming_soon: bool = False,
    max_players: int | None = None,
    max_players_source: str | None = None,
    igdb_id: int | None = None,
    multiplayer_support=None,
    genres=None,
    themes=None,
    game_modes=None,
    return_status: bool = False,
):
    result = await sync_game(
        name=name,
        store_link=store_link,
        store=store,
        suggested_by=suggested_by,
        image_url=image_url,
        source_link=(
            source_link
            or store_link
        ),
        external_id=(
            external_id
            or (
                _steam_app_id(store_link)
                if store == "Steam"
                else None
            )
        ),
        link_status=link_status,
        http_status=http_status,
        availability_status=(
            availability_status
        ),
        release_date=release_date,
        coming_soon=coming_soon,
        max_players=max_players,
        max_players_source=max_players_source,
        igdb_id=igdb_id,
        multiplayer_support=multiplayer_support,
        genres=genres,
        themes=themes,
        game_modes=game_modes,
    )

    if return_status:
        return result

    return result == "added"


async def get_game_cache_record(
    *,
    name: str | None = None,
    store: str | None = None,
    store_link: str | None = None,
    external_id: str | None = None,
) -> dict | None:
    async with database_connection() as db:
        existing = await _find_existing_game(
            db,
            name=_clean_optional_text(name),
            store=_clean_optional_text(store),
            store_link=_clean_optional_text(
                store_link
            ),
            source_link=None,
            external_id=_clean_optional_text(
                external_id
            ),
        )

    if not existing:
        return None

    return _game_cache_record_from_row(existing)


def _game_cache_record_from_row(row) -> dict:
    return {
        "id": row[0],
        "name": row[1],
        "store_link": row[2],
        "store": row[3],
        "image_url": row[4],
        "external_id": row[5],
        "link_status": row[6],
        "http_status": row[7],
        "availability_status": row[8],
        "release_date": row[9],
        "coming_soon": bool(row[10]),
        "max_players": row[11],
        "max_players_source": row[12],
        "igdb_id": row[13],
        "multiplayer_support": row[14],
        "genres": row[15],
        "themes": row[16],
        "game_modes": row[17],
    }


async def get_all_game_cache_records() -> list[dict]:
    """Return the sync cache records in one database read."""

    async with database_connection() as db:
        cursor = await db.execute(
            """
            SELECT
                id,
                name,
                store_link,
                store,
                image_url,
                external_id,
                link_status,
                http_status,
                availability_status,
                release_date,
                coming_soon,
                max_players,
                max_players_source,
                igdb_id,
                multiplayer_support_json,
                genres_json,
                themes_json,
                game_modes_json
            FROM games
            """
        )
        rows = await cursor.fetchall()

    return [
        _game_cache_record_from_row(row)
        for row in rows
    ]


async def get_all_store_replacements(
    *,
    store: str | None = None,
) -> list[dict]:
    """Return saved store replacements in one database read."""

    clean_store = _clean_optional_text(store)
    query = """
        SELECT
            store,
            old_external_id,
            old_store_link,
            old_name,
            game_id,
            new_external_id,
            new_store_link,
            new_name,
            replaced_at
        FROM store_replacements
    """
    parameters = ()

    if clean_store:
        query += " WHERE store = ?"
        parameters = (clean_store,)

    query += " ORDER BY id DESC"

    async with database_connection() as db:
        cursor = await db.execute(
            query,
            parameters,
        )
        rows = await cursor.fetchall()

    return [
        {
            "store": row[0],
            "old_external_id": row[1],
            "old_store_link": row[2],
            "old_name": row[3],
            "game_id": row[4],
            "new_external_id": row[5],
            "new_store_link": row[6],
            "new_name": row[7],
            "replaced_at": row[8],
        }
        for row in rows
    ]


async def get_all_games():
    async with database_connection() as db:
        cursor = await db.execute(
            f"""
            SELECT
                name,
                store,
                suggested_by,
                times_played,
                last_played
            FROM games
            WHERE
                COALESCE(
                    availability_status,
                    'released'
                ) = 'released'
                AND (
                    link_status IS NULL
                    OR link_status != 'dead'
                )
                AND {MULTIPLAYER_WHEEL_FILTER}
            ORDER BY name COLLATE NOCASE
            """
        )

        return await cursor.fetchall()


async def get_all_artwork_records() -> list[dict]:
    """Return every stored game that can own a local artwork file."""

    async with database_connection() as db:
        cursor = await db.execute(
            """
            SELECT id, name, image_url
            FROM games
            ORDER BY id
            """
        )
        rows = await cursor.fetchall()

    return [
        {
            "id": row["id"],
            "name": row["name"],
            "image_url": row["image_url"],
        }
        for row in rows
    ]


async def get_game_metadata_audit_records() -> list[dict]:
    """Return every game and the fields used by the metadata audit."""

    async with database_connection() as db:
        cursor = await db.execute(
            f"""
            SELECT
                id,
                name,
                store_link,
                store,
                image_url,
                external_id,
                link_status,
                availability_status,
                release_date,
                max_players,
                max_players_source,
                igdb_id,
                multiplayer_support_json,
                genres_json,
                game_modes_json,
                CASE
                    WHEN LOWER(
                        COALESCE(
                            availability_status,
                            'released'
                        )
                    ) = 'coming_soon'
                    THEN 'Wishlist'
                    WHEN LOWER(
                        COALESCE(
                            link_status,
                            'unknown'
                        )
                    ) = 'dead'
                    THEN 'Unavailable'
                    WHEN {SINGLEPLAYER_WHEEL_FILTER}
                    THEN 'Single-player wheel'
                    WHEN {MULTIPLAYER_WHEEL_FILTER}
                    THEN 'Multiplayer wheel'
                    ELSE 'Unclassified'
                END AS library_section
            FROM games
            ORDER BY
                CASE
                    WHEN LOWER(
                        COALESCE(
                            availability_status,
                            'released'
                        )
                    ) = 'released'
                    THEN 0
                    ELSE 1
                END,
                name COLLATE NOCASE
            """
        )
        rows = await cursor.fetchall()

    return [
        dict(row)
        for row in rows
    ]


async def get_all_singleplayer_games():
    async with database_connection() as db:
        cursor = await db.execute(
            f"""
            SELECT
                name,
                store,
                suggested_by,
                times_played,
                last_played,
                CASE
                    WHEN ({LOCAL_ONLY_MULTIPLAYER_FILTER})
                    THEN 'local_only'
                    ELSE 'single_player'
                END AS wheel_reason
            FROM games
            WHERE
                COALESCE(
                    availability_status,
                    'released'
                ) = 'released'
                AND (
                    link_status IS NULL
                    OR link_status != 'dead'
                )
                AND {SINGLEPLAYER_WHEEL_FILTER}
            ORDER BY name COLLATE NOCASE
            """
        )

        return await cursor.fetchall()


async def get_wishlist_games():
    async with database_connection() as db:
        cursor = await db.execute(
            """
            SELECT
                id,
                name,
                store_link,
                store,
                suggested_by,
                release_date,
                NULLIF(image_url, '') AS display_image_url,
                added_date
            FROM games
            WHERE
                availability_status = 'coming_soon'
                AND (
                    link_status IS NULL
                    OR link_status != 'dead'
                )
            ORDER BY
                CASE
                    WHEN release_date IS NULL
                         OR release_date = ''
                         OR LOWER(release_date)
                            LIKE '%announc%'
                         OR LOWER(release_date)
                            LIKE '%tba%'
                         OR LOWER(release_date)
                            LIKE '%coming soon%'
                    THEN 1
                    ELSE 0
                END,
                release_date COLLATE NOCASE,
                name COLLATE NOCASE
            """
        )

        return await cursor.fetchall()


async def _select_random_game(
    db,
    *,
    where_clause: str,
    parameters: tuple = (),
):
    count_cursor = await db.execute(
        f"""
        SELECT COUNT(*)
        FROM games
        WHERE {where_clause}
        """,
        parameters,
    )
    row_count = (await count_cursor.fetchone())[0]

    if not row_count:
        return None

    random_offset = secrets.randbelow(row_count)
    cursor = await db.execute(
        f"""
        SELECT
            id,
            name,
            store_link,
            store,
            suggested_by,
            times_played,
            last_played,
            NULLIF(image_url, '') AS display_image_url,
            max_players,
            image_url AS source_image_url,
            igdb_id,
            multiplayer_support_json,
            genres_json,
            themes_json,
            game_modes_json
        FROM games
        WHERE {where_clause}
        ORDER BY id
        LIMIT 1 OFFSET ?
        """,
        (*parameters, random_offset),
    )
    return await cursor.fetchone()


async def get_smart_random_game():
    cutoff = (
        utc_now()
        - timedelta(days=30)
    ).isoformat()
    active_filter = f"""
        COALESCE(
            availability_status,
            'released'
        ) = 'released'
        AND (
            link_status IS NULL
            OR link_status != 'dead'
        )
        AND {MULTIPLAYER_WHEEL_FILTER}
    """

    async with database_connection() as db:
        game = await _select_random_game(
            db,
            where_clause=(
                f"{active_filter} AND ("
                "last_played IS NULL OR last_played < ?)"
            ),
            parameters=(cutoff,),
        )

        if game is None:
            game = await _select_random_game(
                db,
                where_clause=active_filter,
            )

        return game


async def get_smart_random_singleplayer_game():
    cutoff = (
        utc_now()
        - timedelta(days=30)
    ).isoformat()
    active_filter = f"""
        COALESCE(
            availability_status,
            'released'
        ) = 'released'
        AND (
            link_status IS NULL
            OR link_status != 'dead'
        )
        AND {SINGLEPLAYER_WHEEL_FILTER}
    """

    async with database_connection() as db:
        game = await _select_random_game(
            db,
            where_clause=(
                f"{active_filter} AND ("
                "last_played IS NULL OR last_played < ?)"
            ),
            parameters=(cutoff,),
        )

        if game is None:
            game = await _select_random_game(
                db,
                where_clause=active_filter,
            )

        return game


async def mark_game_played(
    game_id: int,
    locked_by: str,
) -> None:
    now = utc_now_iso()

    async with database_connection() as db:
        await db.execute(
            """
            UPDATE games
            SET
                times_played = times_played + 1,
                last_played = ?
            WHERE
                id = ?
                AND COALESCE(
                    availability_status,
                    'released'
                ) = 'released'
            """,
            (
                now,
                game_id,
            ),
        )

        await db.execute(
            """
            INSERT INTO game_history (
                game_id,
                played_date,
                locked_by
            )
            SELECT
                ?, ?, ?
            WHERE EXISTS (
                SELECT 1
                FROM games
                WHERE
                    id = ?
                    AND COALESCE(
                        availability_status,
                        'released'
                    ) = 'released'
            )
            """,
            (
                game_id,
                now,
                locked_by,
                game_id,
            ),
        )
        await db.commit()


async def get_recent_history(
    limit: int = 10,
):
    async with database_connection() as db:
        cursor = await db.execute(
            """
            SELECT
                games.name,
                game_history.played_date,
                game_history.locked_by
            FROM game_history
            JOIN games
                ON games.id = game_history.game_id
            ORDER BY game_history.played_date DESC
            LIMIT ?
            """,
            (limit,),
        )

        return await cursor.fetchall()


async def get_latest_history_entry() -> dict | None:
    async with database_connection() as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            """
            SELECT
                game_history.id AS history_id,
                game_history.game_id,
                games.name,
                game_history.played_date,
                game_history.locked_by,
                games.times_played
            FROM game_history
            JOIN games
                ON games.id = game_history.game_id
            ORDER BY
                game_history.played_date DESC,
                game_history.id DESC
            LIMIT 1
            """
        )

        row = await cursor.fetchone()

        return dict(row) if row else None


async def undo_latest_history_entry(
    *,
    expected_history_id: int,
) -> dict:
    """Undo one confirmed lock-in without disturbing older history."""

    async with database_connection() as db:
        db.row_factory = aiosqlite.Row

        try:
            # Hold the write lock from the latest-entry check through
            # the update so two moderators cannot undo the same row.
            await db.execute(
                "BEGIN IMMEDIATE"
            )

            cursor = await db.execute(
                """
                SELECT
                    game_history.id AS history_id,
                    game_history.game_id,
                    games.name,
                    game_history.played_date,
                    game_history.locked_by
                FROM game_history
                JOIN games
                    ON games.id = game_history.game_id
                ORDER BY
                    game_history.played_date DESC,
                    game_history.id DESC
                LIMIT 1
                """
            )

            latest = await cursor.fetchone()

            if latest is None:
                await db.rollback()
                return {
                    "status": "empty",
                }

            if (
                latest["history_id"]
                != expected_history_id
            ):
                await db.rollback()
                return {
                    "status": "stale",
                    "latest": dict(latest),
                }

            await db.execute(
                """
                DELETE FROM game_history
                WHERE id = ?
                """,
                (latest["history_id"],),
            )

            await db.execute(
                """
                UPDATE games
                SET
                    times_played = (
                        SELECT COUNT(*)
                        FROM game_history
                        WHERE game_id = ?
                    ),
                    last_played = (
                        SELECT MAX(played_date)
                        FROM game_history
                        WHERE game_id = ?
                    )
                WHERE id = ?
                """,
                (
                    latest["game_id"],
                    latest["game_id"],
                    latest["game_id"],
                ),
            )

            cursor = await db.execute(
                """
                SELECT times_played, last_played
                FROM games
                WHERE id = ?
                """,
                (latest["game_id"],),
            )

            updated_game = await cursor.fetchone()

            await db.commit()

        except Exception:
            await db.rollback()
            raise

        return {
            "status": "undone",
            "history_id": latest["history_id"],
            "game_id": latest["game_id"],
            "name": latest["name"],
            "played_date": latest["played_date"],
            "locked_by": latest["locked_by"],
            "times_played": updated_game[0],
            "last_played": updated_game[1],
        }


def _create_automatic_backup_sync(
    *,
    minimum_interval_hours: float,
    retention: int,
) -> dict:
    if retention < 1:
        raise ValueError(
            "Backup retention must be at least 1."
        )

    BACKUP_DIRECTORY.mkdir(
        parents=True,
        exist_ok=True,
    )

    existing_backups = sorted(
        BACKUP_DIRECTORY.glob(
            f"{AUTOMATIC_BACKUP_PREFIX}*.db"
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )

    now_timestamp = time.time()

    if existing_backups:
        newest_age_seconds = (
            now_timestamp
            - existing_backups[0].stat().st_mtime
        )

        if newest_age_seconds < (
            minimum_interval_hours * 60 * 60
        ):
            return {
                "status": "not_due",
                "path": str(existing_backups[0]),
                "removed": 0,
            }

    timestamp = utc_now().strftime(
        "%Y%m%d-%H%M%SZ"
    )

    backup_path = BACKUP_DIRECTORY / (
        f"{AUTOMATIC_BACKUP_PREFIX}{timestamp}.db"
    )
    temporary_path = Path(
        f"{backup_path}.tmp"
    )

    temporary_path.unlink(
        missing_ok=True
    )

    try:
        source_uri = (
            DATABASE_PATH.resolve().as_uri()
            + "?mode=ro"
        )

        with closing(
            sqlite3.connect(
                source_uri,
                uri=True,
                timeout=30,
            )
        ) as source_db:
            with closing(
                sqlite3.connect(
                    temporary_path,
                    timeout=30,
                )
            ) as backup_db:
                source_db.backup(
                    backup_db
                )

                backup_db.commit()

                integrity_result = (
                    backup_db.execute(
                        "PRAGMA quick_check"
                    ).fetchone()
                )

                if (
                    not integrity_result
                    or integrity_result[0] != "ok"
                ):
                    raise RuntimeError(
                        "SQLite quick_check failed for "
                        "the new backup."
                    )

        os.replace(
            temporary_path,
            backup_path,
        )

    finally:
        temporary_path.unlink(
            missing_ok=True
        )

    all_backups = sorted(
        BACKUP_DIRECTORY.glob(
            f"{AUTOMATIC_BACKUP_PREFIX}*.db"
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )

    removed = 0

    for old_backup in all_backups[retention:]:
        old_backup.unlink()
        removed += 1

    return {
        "status": "created",
        "path": str(backup_path),
        "removed": removed,
    }


async def create_automatic_backup(
    *,
    minimum_interval_hours: float = 24,
    retention: int = 7,
) -> dict:
    """Create and validate a consistent live SQLite snapshot."""

    return await asyncio.to_thread(
        _create_automatic_backup_sync,
        minimum_interval_hours=(
            minimum_interval_hours
        ),
        retention=retention,
    )


async def get_stats():
    async with database_connection() as db:
        active_filter = f"""
            COALESCE(
                availability_status,
                'released'
            ) = 'released'
            AND (
                link_status IS NULL
                OR link_status != 'dead'
            )
            AND {MULTIPLAYER_WHEEL_FILTER}
        """

        cursor = await db.execute(
            f"""
            SELECT
                COUNT(
                    CASE
                        WHEN {active_filter}
                        THEN 1
                    END
                ) AS total_games,
                COUNT(
                    CASE
                        WHEN
                            {active_filter}
                            AND COALESCE(
                                times_played,
                                0
                            ) = 0
                        THEN 1
                    END
                ) AS never_played,
                COUNT(
                    CASE
                        WHEN
                            COALESCE(
                                availability_status,
                                'released'
                            ) = 'released'
                            AND {SINGLEPLAYER_WHEEL_FILTER}
                            AND (
                                link_status IS NULL
                                OR link_status != 'dead'
                            )
                        THEN 1
                    END
                ) AS singleplayer_games,
                COUNT(
                    CASE
                        WHEN
                            availability_status = 'coming_soon'
                            AND (
                                link_status IS NULL
                                OR link_status != 'dead'
                            )
                        THEN 1
                    END
                ) AS wishlist_games
            FROM games
            """
        )
        counts = await cursor.fetchone()
        total_games = counts[0]
        never_played = counts[1]
        singleplayer_games = counts[2]
        wishlist_games = counts[3]

        cursor = await db.execute(
            f"""
            SELECT
                name,
                times_played
            FROM games
            WHERE {active_filter}
            ORDER BY
                times_played DESC,
                name COLLATE NOCASE
            LIMIT 1
            """
        )
        most_played = await cursor.fetchone()

        cursor = await db.execute(
            """
            SELECT
                games.name,
                game_history.played_date
            FROM game_history
            JOIN games
                ON games.id = game_history.game_id
            ORDER BY
                game_history.played_date DESC
            LIMIT 1
            """
        )
        last_played = await cursor.fetchone()

        cursor = await db.execute(
            f"""
            SELECT
                suggested_by,
                COUNT(*)
            FROM games
            WHERE {active_filter}
            GROUP BY suggested_by
            ORDER BY COUNT(*) DESC
            LIMIT 1
            """
        )
        top_suggester = await cursor.fetchone()

        return {
            "total_games": total_games,
            "never_played": never_played,
            "most_played": most_played,
            "last_played": last_played,
            "top_suggester": top_suggester,
            "wishlist_games": wishlist_games,
            "singleplayer_games": singleplayer_games,
        }


async def reset_all_play_history() -> dict:
    async with database_connection() as db:
        cursor = await db.execute(
            """
            SELECT COUNT(*)
            FROM game_history
            """
        )
        history_entries_deleted = (
            await cursor.fetchone()
        )[0]

        cursor = await db.execute(
            """
            SELECT COUNT(*)
            FROM games
            WHERE
                times_played != 0
                OR last_played IS NOT NULL
            """
        )
        games_reset = (
            await cursor.fetchone()
        )[0]

        await db.execute(
            """
            UPDATE games
            SET
                times_played = 0,
                last_played = NULL
            """
        )

        await db.execute(
            """
            DELETE FROM game_history
            """
        )

        await db.commit()

        return {
            "games_reset": games_reset,
            "history_entries_deleted": (
                history_entries_deleted
            ),
        }


async def delete_game_by_name(
    game_name: str,
) -> bool:
    async with database_connection() as db:
        cursor = await db.execute(
            """
            SELECT id
            FROM games
            WHERE name = ? COLLATE NOCASE
            LIMIT 1
            """,
            (game_name.strip(),),
        )

        row = await cursor.fetchone()

        if not row:
            return False

        game_id = row[0]

        await db.execute(
            """
            DELETE FROM game_history
            WHERE game_id = ?
            """,
            (game_id,),
        )

        await db.execute(
            """
            DELETE FROM games
            WHERE id = ?
            """,
            (game_id,),
        )

        await db.commit()
        return True
