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
    """Migrate legacy naive GMT+10 timestamps to explicit UTC."""

    timestamp_columns = (
        ("games", "last_played"),
        ("games", "added_date"),
        ("games", "last_link_check"),
        ("game_history", "played_date"),
        ("store_replacements", "replaced_at"),
        ("gaming_sessions", "created_at"),
        ("gaming_sessions", "ended_at"),
        ("gaming_session_members", "joined_at"),
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

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS removed_games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                store TEXT,
                store_link TEXT,
                external_id TEXT,
                removed_at TEXT NOT NULL,
                UNIQUE(store, external_id),
                UNIQUE(store_link)
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS gaming_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                host_id INTEGER NOT NULL,
                host_name TEXT NOT NULL,
                voice_channel_id INTEGER NOT NULL,
                control_channel_id INTEGER,
                control_message_id INTEGER,
                voice_notice_message_id INTEGER,
                status TEXT NOT NULL DEFAULT 'starting',
                player_count INTEGER NOT NULL,
                manual_player_count INTEGER,
                include_unverified INTEGER NOT NULL DEFAULT 0,
                use_normal_wheel INTEGER NOT NULL DEFAULT 0,
                cache_generation INTEGER NOT NULL DEFAULT 1,
                selected_game_id INTEGER,
                custom_game_name TEXT,
                custom_game_link TEXT,
                custom_game_store TEXT,
                custom_game_image_url TEXT,
                selected_by_id INTEGER,
                selected_by_name TEXT,
                created_at TEXT NOT NULL,
                ended_at TEXT,
                FOREIGN KEY(selected_game_id)
                    REFERENCES games(id)
                    ON DELETE SET NULL
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS gaming_session_members (
                session_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                joined_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'button',
                PRIMARY KEY(session_id, user_id),
                FOREIGN KEY(session_id)
                    REFERENCES gaming_sessions(id)
                    ON DELETE CASCADE
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS gaming_session_games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                game_id INTEGER,
                game_name TEXT NOT NULL,
                game_link TEXT,
                selected_by_id INTEGER,
                selected_by_name TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                lock_message_channel_id INTEGER,
                lock_message_id INTEGER,
                FOREIGN KEY(session_id)
                    REFERENCES gaming_sessions(id)
                    ON DELETE CASCADE,
                FOREIGN KEY(game_id)
                    REFERENCES games(id)
                    ON DELETE SET NULL
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS game_night_weeks (
                week_start TEXT PRIMARY KEY,
                poll_channel_id INTEGER,
                poll_message_id INTEGER,
                poll_created_at TEXT,
                poll_closes_at TEXT,
                winner_day TEXT,
                winner_reason TEXT,
                friday_votes INTEGER,
                saturday_votes INTEGER,
                scheduled_event_id INTEGER,
                event_start_at TEXT,
                reminder_24h_sent INTEGER NOT NULL DEFAULT 0,
                reminder_6h_sent INTEGER NOT NULL DEFAULT 0,
                reminder_1h_sent INTEGER NOT NULL DEFAULT 0,
                checkin_channel_id INTEGER,
                checkin_message_id INTEGER,
                checkin_closed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS game_night_checkins (
                week_start TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                response TEXT NOT NULL CHECK (
                    response IN ('playing', 'maybe', 'cant_make_it')
                ),
                updated_at TEXT NOT NULL,
                PRIMARY KEY(week_start, user_id),
                FOREIGN KEY(week_start)
                    REFERENCES game_night_weeks(week_start)
                    ON DELETE CASCADE
            )
            """
        )

        await db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                active_gaming_session_per_voice
            ON gaming_sessions(guild_id, voice_channel_id)
            WHERE status IN ('starting', 'active')
            """
        )

        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS
                gaming_session_members_by_session
            ON gaming_session_members(session_id)
            """
        )

        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS
                gaming_session_games_by_session
            ON gaming_session_games(session_id, started_at)
            """
        )

        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS
                game_night_weeks_by_event_start
            ON game_night_weeks(event_start_at)
            """
        )

        week_columns = []

        async with db.execute(
            "PRAGMA table_info(game_night_weeks)"
        ) as cursor:
            async for row in cursor:
                week_columns.append(row[1])

        for column_name, column_sql in (
            ("checkin_channel_id", "INTEGER"),
            ("checkin_message_id", "INTEGER"),
            ("checkin_closed", "INTEGER NOT NULL DEFAULT 0"),
        ):
            await _add_column_if_missing(
                db,
                "game_night_weeks",
                week_columns,
                column_name,
                column_sql,
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


async def _is_removed_game(
    db,
    *,
    name: str | None,
    store: str | None,
    store_link: str | None,
    source_link: str | None,
    external_id: str | None,
) -> bool:
    candidate_links = {
        link
        for link in (
            store_link,
            source_link,
        )
        if link
    }

    if external_id and store:
        cursor = await db.execute(
            """
            SELECT 1
            FROM removed_games
            WHERE
                store = ? COLLATE NOCASE
                AND external_id = ? COLLATE NOCASE
            LIMIT 1
            """,
            (
                store,
                external_id,
            ),
        )

        if await cursor.fetchone():
            return True

    for candidate_link in candidate_links:
        cursor = await db.execute(
            """
            SELECT 1
            FROM removed_games
            WHERE store_link = ?
            LIMIT 1
            """,
            (candidate_link,),
        )

        if await cursor.fetchone():
            return True

    if name and store:
        cursor = await db.execute(
            """
            SELECT 1
            FROM removed_games
            WHERE
                name = ? COLLATE NOCASE
                AND store = ? COLLATE NOCASE
            LIMIT 1
            """,
            (
                name,
                store,
            ),
        )
        return bool(await cursor.fetchone())

    return False


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


def _build_game_cache_record(
    *,
    game_id,
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
    multiplayer_support,
    genres,
    themes,
    game_modes,
) -> dict:
    return {
        "id": game_id,
        "name": name,
        "store_link": store_link,
        "store": store,
        "image_url": image_url,
        "external_id": external_id,
        "link_status": link_status,
        "http_status": http_status,
        "availability_status": availability_status,
        "release_date": release_date,
        "coming_soon": bool(coming_soon),
        "max_players": max_players,
        "max_players_source": max_players_source,
        "igdb_id": igdb_id,
        "multiplayer_support": multiplayer_support,
        "genres": genres,
        "themes": themes,
        "game_modes": game_modes,
    }


def _sync_game_result(
    status: str,
    *,
    return_details: bool,
    record: dict | None = None,
    artwork_changed: bool = False,
):
    if not return_details:
        return status

    return {
        "status": status,
        "game_id": (
            record.get("id")
            if record is not None
            else None
        ),
        "record": record,
        "artwork_changed": bool(
            artwork_changed
        ),
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
    return_details: bool = False,
) -> str | dict:
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
        if await _is_removed_game(
            db,
            name=clean_name,
            store=clean_store,
            store_link=clean_store_link,
            source_link=clean_source_link,
            external_id=clean_external_id,
        ):
            return _sync_game_result(
                "removed",
                return_details=return_details,
            )

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

            existing_record = (
                _game_cache_record_from_row(
                    existing
                )
            )

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
                    return _sync_game_result(
                        "unavailable",
                        return_details=return_details,
                        record=existing_record,
                    )

                if (
                    clean_availability
                    == WISHLIST_STATUS
                ):
                    return _sync_game_result(
                        "wishlist_unchanged",
                        return_details=return_details,
                        record=existing_record,
                    )

                return _sync_game_result(
                    "unchanged",
                    return_details=return_details,
                    record=existing_record,
                )

            saved_name = new_name
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
                saved_name = old_name

            await _commit_database_write(db)

            updated_record = _build_game_cache_record(
                game_id=game_id,
                name=saved_name,
                store_link=new_store_link,
                store=new_store,
                image_url=new_image_url,
                external_id=new_external_id,
                link_status=clean_link_status,
                http_status=http_status,
                availability_status=clean_availability,
                release_date=new_release_date,
                coming_soon=clean_coming_soon,
                max_players=new_max_players,
                max_players_source=(
                    new_max_players_source
                ),
                igdb_id=new_igdb_id,
                multiplayer_support=(
                    new_multiplayer_support
                ),
                genres=new_genres,
                themes=new_themes,
                game_modes=new_game_modes,
            )
            artwork_changed = (
                new_image_url != old_image_url
            )

            if clean_link_status == "dead":
                return _sync_game_result(
                    "unavailable",
                    return_details=return_details,
                    record=updated_record,
                    artwork_changed=artwork_changed,
                )

            if (
                old_availability
                == WISHLIST_STATUS
                and clean_availability
                == ACTIVE_STATUS
            ):
                return _sync_game_result(
                    "promoted",
                    return_details=return_details,
                    record=updated_record,
                    artwork_changed=artwork_changed,
                )

            if (
                old_availability
                != WISHLIST_STATUS
                and clean_availability
                == WISHLIST_STATUS
            ):
                return _sync_game_result(
                    "moved_to_wishlist",
                    return_details=return_details,
                    record=updated_record,
                    artwork_changed=artwork_changed,
                )

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
                return _sync_game_result(
                    "moved_to_singleplayer",
                    return_details=return_details,
                    record=updated_record,
                    artwork_changed=artwork_changed,
                )

            if (
                clean_availability == ACTIVE_STATUS
                and old_singleplayer_wheel
                and not new_singleplayer_wheel
            ):
                return _sync_game_result(
                    "moved_to_multiplayer",
                    return_details=return_details,
                    record=updated_record,
                    artwork_changed=artwork_changed,
                )

            if changed:
                if (
                    clean_availability
                    == WISHLIST_STATUS
                ):
                    return _sync_game_result(
                        "wishlist_updated",
                        return_details=return_details,
                        record=updated_record,
                        artwork_changed=artwork_changed,
                    )

                return _sync_game_result(
                    "updated",
                    return_details=return_details,
                    record=updated_record,
                    artwork_changed=artwork_changed,
                )

            if (
                clean_availability
                == WISHLIST_STATUS
            ):
                return _sync_game_result(
                    "wishlist_unchanged",
                    return_details=return_details,
                    record=updated_record,
                    artwork_changed=artwork_changed,
                )

            return _sync_game_result(
                "unchanged",
                return_details=return_details,
                record=updated_record,
                artwork_changed=artwork_changed,
            )

        if clean_link_status == "dead":
            return _sync_game_result(
                "unavailable",
                return_details=return_details,
            )

        try:
            cursor = await db.execute(
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

            inserted_record = _build_game_cache_record(
                game_id=cursor.lastrowid,
                name=clean_name,
                store_link=clean_store_link,
                store=clean_store,
                image_url=clean_image_url,
                external_id=clean_external_id,
                link_status=clean_link_status,
                http_status=http_status,
                availability_status=clean_availability,
                release_date=clean_release_date,
                coming_soon=clean_coming_soon,
                max_players=clean_max_players,
                max_players_source=(
                    clean_max_players_source
                ),
                igdb_id=clean_igdb_id,
                multiplayer_support=(
                    clean_multiplayer_support
                ),
                genres=clean_genres,
                themes=clean_themes,
                game_modes=clean_game_modes,
            )

            if (
                clean_availability
                == WISHLIST_STATUS
            ):
                return _sync_game_result(
                    "wishlisted",
                    return_details=return_details,
                    record=inserted_record,
                    artwork_changed=True,
                )

            if _belongs_on_singleplayer_wheel(
                clean_max_players,
                clean_multiplayer_support,
            ):
                return _sync_game_result(
                    "singleplayer_added",
                    return_details=return_details,
                    record=inserted_record,
                    artwork_changed=True,
                )

            return _sync_game_result(
                "added",
                return_details=return_details,
                record=inserted_record,
                artwork_changed=True,
            )

        except aiosqlite.IntegrityError:
            concurrent_record = await _find_existing_game(
                db,
                name=clean_name,
                store=clean_store,
                store_link=clean_store_link,
                source_link=clean_source_link,
                external_id=clean_external_id,
            )
            return _sync_game_result(
                "unchanged",
                return_details=return_details,
                record=(
                    _game_cache_record_from_row(
                        concurrent_record
                    )
                    if concurrent_record
                    else None
                ),
            )


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
    return_details: bool = False,
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
        return_details=return_details,
    )

    if return_details:
        return result

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
    return _build_game_cache_record(
        game_id=row[0],
        name=row[1],
        store_link=row[2],
        store=row[3],
        image_url=row[4],
        external_id=row[5],
        link_status=row[6],
        http_status=row[7],
        availability_status=row[8],
        release_date=row[9],
        coming_soon=row[10],
        max_players=row[11],
        max_players_source=row[12],
        igdb_id=row[13],
        multiplayer_support=row[14],
        genres=row[15],
        themes=row[16],
        game_modes=row[17],
    )


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


async def get_wheel_game_ids() -> dict[str, frozenset[int]]:
    """Return the current eligible game IDs for each wheel."""

    active_filter = """
        COALESCE(
            availability_status,
            'released'
        ) = 'released'
        AND (
            link_status IS NULL
            OR link_status != 'dead'
        )
    """

    async with database_connection() as db:
        cursor = await db.execute(
            f"""
            SELECT
                id,
                CASE
                    WHEN {MULTIPLAYER_WHEEL_FILTER}
                    THEN 1
                    ELSE 0
                END AS multiplayer_eligible,
                CASE
                    WHEN {SINGLEPLAYER_WHEEL_FILTER}
                    THEN 1
                    ELSE 0
                END AS singleplayer_eligible
            FROM games
            WHERE
                {active_filter}
                AND (
                    ({MULTIPLAYER_WHEEL_FILTER})
                    OR ({SINGLEPLAYER_WHEEL_FILTER})
                )
            ORDER BY id
            """
        )
        rows = await cursor.fetchall()

    return {
        "multiplayer": frozenset(
            int(row["id"])
            for row in rows
            if row["multiplayer_eligible"]
        ),
        "singleplayer": frozenset(
            int(row["id"])
            for row in rows
            if row["singleplayer_eligible"]
        ),
    }


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


async def get_games_missing_igdb_metadata() -> list[dict]:
    """Return active games worth retrying against IGDB."""

    async with database_connection() as db:
        cursor = await db.execute(
            """
            SELECT
                id,
                name,
                store_link,
                store,
                external_id,
                max_players,
                max_players_source,
                igdb_id,
                multiplayer_support_json,
                genres_json,
                themes_json,
                game_modes_json
            FROM games
            WHERE
                LOWER(
                    COALESCE(
                        availability_status,
                        'released'
                    )
                ) = 'released'
                AND LOWER(
                    COALESCE(
                        link_status,
                        'unknown'
                    )
                ) != 'dead'
                AND igdb_id IS NULL
                AND (
                    max_players IS NULL
                    OR LOWER(
                        COALESCE(
                            NULLIF(
                                TRIM(multiplayer_support_json),
                                ''
                            ),
                            '{}'
                        )
                    ) IN ('{}', 'null')
                    OR LOWER(
                        COALESCE(
                            NULLIF(TRIM(genres_json), ''),
                            '[]'
                        )
                    ) IN ('[]', 'null')
                    OR LOWER(
                        COALESCE(
                            NULLIF(TRIM(game_modes_json), ''),
                            '[]'
                        )
                    ) IN ('[]', 'null')
                )
            ORDER BY id
            """
        )
        rows = await cursor.fetchall()

    games = []

    for row in rows:
        game = dict(row)

        for column_name, field_name, expected_type in (
            (
                "multiplayer_support_json",
                "multiplayer_support",
                dict,
            ),
            ("genres_json", "genres", list),
            ("themes_json", "themes", list),
            ("game_modes_json", "game_modes", list),
        ):
            decoded_value = None
            stored_value = game.pop(column_name, None)

            if isinstance(stored_value, str):
                try:
                    candidate_value = json.loads(stored_value)

                except (TypeError, ValueError):
                    candidate_value = None

                if isinstance(candidate_value, expected_type):
                    decoded_value = candidate_value

            game[field_name] = decoded_value

        games.append(game)

    return games


async def save_refreshed_igdb_metadata(
    game_id: int,
    game_info: dict,
) -> bool:
    """Save a daily IGDB retry without touching store metadata."""

    clean_igdb_id = _clean_igdb_id(
        game_info.get("igdb_id")
    )
    clean_support = _clean_json_metadata(
        game_info.get("multiplayer_support"),
        dict,
    )
    clean_genres = _clean_json_metadata(
        game_info.get("genres"),
        list,
    )
    clean_themes = _clean_json_metadata(
        game_info.get("themes"),
        list,
    )
    clean_game_modes = _clean_json_metadata(
        game_info.get("game_modes"),
        list,
    )

    try:
        clean_max_players = int(
            game_info.get("max_players")
        )

    except (TypeError, ValueError):
        clean_max_players = None

    if (
        clean_max_players is not None
        and not 1 <= clean_max_players <= 100
    ):
        clean_max_players = None

    async with database_connection() as db:
        cursor = await db.execute(
            """
            SELECT
                max_players,
                max_players_source,
                igdb_id,
                multiplayer_support_json,
                genres_json,
                themes_json,
                game_modes_json
            FROM games
            WHERE id = ?
            """,
            (game_id,),
        )
        existing = await cursor.fetchone()

        if existing is None:
            return False

        old_max_players = existing["max_players"]
        old_max_players_source = existing[
            "max_players_source"
        ]
        new_max_players = old_max_players
        new_max_players_source = old_max_players_source

        if (
            old_max_players is None
            and clean_max_players is not None
        ):
            new_max_players = clean_max_players
            new_max_players_source = "IGDB"

        new_igdb_id = clean_igdb_id or existing["igdb_id"]
        new_support = (
            clean_support
            if clean_support is not None
            else existing["multiplayer_support_json"]
        )
        new_support = _complete_multiplayer_support_limits(
            new_support,
            new_max_players,
        )
        new_genres = (
            clean_genres
            if clean_genres is not None
            else existing["genres_json"]
        )
        new_themes = (
            clean_themes
            if clean_themes is not None
            else existing["themes_json"]
        )
        new_game_modes = (
            clean_game_modes
            if clean_game_modes is not None
            else existing["game_modes_json"]
        )

        changed = any(
            (
                new_max_players != old_max_players,
                new_max_players_source
                != old_max_players_source,
                new_igdb_id != existing["igdb_id"],
                new_support
                != existing["multiplayer_support_json"],
                new_genres != existing["genres_json"],
                new_themes != existing["themes_json"],
                new_game_modes
                != existing["game_modes_json"],
            )
        )

        if not changed:
            return False

        await db.execute(
            """
            UPDATE games
            SET
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
                new_max_players,
                new_max_players_source,
                new_igdb_id,
                new_support,
                new_genres,
                new_themes,
                new_game_modes,
                game_id,
            ),
        )
        await _commit_database_write(db)

    return True


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


async def get_spin_games_by_ids(
    game_ids,
) -> dict[int, tuple]:
    """Return complete spin rows for a saved set of game IDs."""

    clean_ids = tuple(
        sorted(
            {
                int(game_id)
                for game_id in game_ids
            }
        )
    )

    if not clean_ids:
        return {}

    placeholders = ", ".join(
        "?"
        for _game_id in clean_ids
    )

    async with database_connection() as db:
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
            WHERE
                id IN ({placeholders})
                AND COALESCE(
                    availability_status,
                    'released'
                ) = 'released'
                AND (
                    link_status IS NULL
                    OR link_status != 'dead'
                )
            """,
            clean_ids,
        )
        rows = await cursor.fetchall()

    return {
        int(row[0]): tuple(row)
        for row in rows
    }


async def get_session_wheel_game_ids(
    player_count: int,
    *,
    include_unverified: bool = False,
    use_normal_wheel: bool = False,
) -> dict:
    """Return the multiplayer games eligible for a session size."""

    clean_player_count = max(
        int(player_count),
        1,
    )
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
        cursor = await db.execute(
            f"""
            SELECT id, max_players
            FROM games
            WHERE {active_filter}
            ORDER BY id
            """
        )
        rows = await cursor.fetchall()

    eligible_ids = set()
    unverified_count = 0
    excluded_for_capacity = 0

    for row in rows:
        game_id = int(row["id"])
        max_players = row["max_players"]

        if use_normal_wheel:
            eligible_ids.add(game_id)
        elif max_players is None:
            unverified_count += 1

            if include_unverified:
                eligible_ids.add(game_id)
        elif int(max_players) >= clean_player_count:
            eligible_ids.add(game_id)
        else:
            excluded_for_capacity += 1

    return {
        "game_ids": frozenset(eligible_ids),
        "player_count": clean_player_count,
        "total_games": len(rows),
        "eligible_count": len(eligible_ids),
        "unverified_count": unverified_count,
        "excluded_for_capacity": excluded_for_capacity,
        "include_unverified": bool(include_unverified),
        "use_normal_wheel": bool(use_normal_wheel),
    }


async def get_smart_random_game_for_ids(
    game_ids,
):
    """Choose a recent-aware multiplayer game from an explicit set."""

    clean_ids = tuple(
        sorted(
            {
                int(game_id)
                for game_id in game_ids
            }
        )
    )

    if not clean_ids:
        return None

    placeholders = ", ".join(
        "?"
        for _game_id in clean_ids
    )
    cutoff = (
        utc_now()
        - timedelta(days=30)
    ).isoformat()
    active_filter = f"""
        id IN ({placeholders})
        AND COALESCE(
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
            parameters=(*clean_ids, cutoff),
        )

        if game is None:
            game = await _select_random_game(
                db,
                where_clause=active_filter,
                parameters=clean_ids,
            )

        return game


async def search_session_games(
    query: str,
    *,
    limit: int = 25,
) -> list[dict]:
    """Search active multiplayer-wheel games for manual selection."""

    cleaned_query = str(query or "").strip()
    like_query = f"%{cleaned_query}%"
    clean_limit = min(
        max(int(limit), 1),
        25,
    )

    async with database_connection() as db:
        cursor = await db.execute(
            f"""
            SELECT
                id,
                name,
                store_link,
                store,
                NULLIF(image_url, '') AS image_url,
                max_players,
                max_players_source
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
                AND (? = '' OR name LIKE ? COLLATE NOCASE)
            ORDER BY
                CASE
                    WHEN name = ? COLLATE NOCASE THEN 0
                    WHEN name LIKE ? COLLATE NOCASE THEN 1
                    ELSE 2
                END,
                name COLLATE NOCASE
            LIMIT ?
            """,
            (
                cleaned_query,
                like_query,
                cleaned_query,
                f"{cleaned_query}%",
                clean_limit,
            ),
        )
        rows = await cursor.fetchall()

    return [
        dict(row)
        for row in rows
    ]


async def create_gaming_session(
    *,
    guild_id: int,
    host_id: int,
    host_name: str,
    voice_channel_id: int,
    members: list[tuple[int, str]],
) -> dict:
    """Create one starting session and seed its voice members."""

    now = utc_now_iso()
    unique_members = {
        int(user_id): str(display_name)
        for user_id, display_name in members
    }
    player_count = max(
        len(unique_members),
        1,
    )

    async with database_connection() as db:
        try:
            cursor = await db.execute(
                """
                INSERT INTO gaming_sessions (
                    guild_id,
                    host_id,
                    host_name,
                    voice_channel_id,
                    status,
                    player_count,
                    created_at
                )
                VALUES (?, ?, ?, ?, 'starting', ?, ?)
                """,
                (
                    int(guild_id),
                    int(host_id),
                    str(host_name),
                    int(voice_channel_id),
                    player_count,
                    now,
                ),
            )
            session_id = int(cursor.lastrowid)

            await db.executemany(
                """
                INSERT INTO gaming_session_members (
                    session_id,
                    user_id,
                    display_name,
                    joined_at,
                    source
                )
                VALUES (?, ?, ?, ?, 'voice')
                """,
                (
                    (
                        session_id,
                        user_id,
                        display_name,
                        now,
                    )
                    for user_id, display_name
                    in unique_members.items()
                ),
            )
            await db.commit()

        except Exception:
            await db.rollback()
            raise

    return await get_gaming_session(session_id)


async def activate_gaming_session(
    session_id: int,
    *,
    control_channel_id: int,
    control_message_id: int,
    voice_notice_message_id: int | None = None,
) -> dict | None:
    async with database_connection() as db:
        await db.execute(
            """
            UPDATE gaming_sessions
            SET
                control_channel_id = ?,
                control_message_id = ?,
                voice_notice_message_id = ?,
                status = 'active'
            WHERE id = ? AND status = 'starting'
            """,
            (
                int(control_channel_id),
                int(control_message_id),
                (
                    int(voice_notice_message_id)
                    if voice_notice_message_id is not None
                    else None
                ),
                int(session_id),
            ),
        )
        await db.commit()

    return await get_gaming_session(session_id)


async def get_gaming_session(
    session_id: int,
) -> dict | None:
    async with database_connection() as db:
        cursor = await db.execute(
            """
            SELECT *
            FROM gaming_sessions
            WHERE id = ?
            """,
            (int(session_id),),
        )
        row = await cursor.fetchone()

        if row is None:
            return None

        member_cursor = await db.execute(
            """
            SELECT user_id, display_name, joined_at, source
            FROM gaming_session_members
            WHERE session_id = ?
            ORDER BY joined_at, user_id
            """,
            (int(session_id),),
        )
        members = await member_cursor.fetchall()

        games_cursor = await db.execute(
            """
            SELECT
                id,
                game_id,
                game_name,
                game_link,
                selected_by_id,
                selected_by_name,
                started_at,
                finished_at,
                lock_message_channel_id,
                lock_message_id
            FROM gaming_session_games
            WHERE session_id = ?
            ORDER BY started_at, id
            """,
            (int(session_id),),
        )
        session_games = await games_cursor.fetchall()

    result = dict(row)
    result["members"] = [
        dict(member)
        for member in members
    ]
    result["games_played"] = [
        dict(game)
        for game in session_games
    ]
    result["effective_player_count"] = int(
        result["manual_player_count"]
        or result["player_count"]
        or 1
    )
    return result


async def get_active_gaming_session_for_voice(
    guild_id: int,
    voice_channel_id: int,
) -> dict | None:
    async with database_connection() as db:
        cursor = await db.execute(
            """
            SELECT id
            FROM gaming_sessions
            WHERE
                guild_id = ?
                AND voice_channel_id = ?
                AND status IN ('starting', 'active')
            ORDER BY id DESC
            LIMIT 1
            """,
            (
                int(guild_id),
                int(voice_channel_id),
            ),
        )
        row = await cursor.fetchone()

    if row is None:
        return None

    return await get_gaming_session(
        int(row["id"])
    )


async def get_active_gaming_sessions() -> list[dict]:
    async with database_connection() as db:
        cursor = await db.execute(
            """
            SELECT id
            FROM gaming_sessions
            WHERE status IN ('starting', 'active')
            ORDER BY id
            """
        )
        rows = await cursor.fetchall()

    sessions = []

    for row in rows:
        session = await get_gaming_session(
            int(row["id"])
        )

        if session is not None:
            sessions.append(session)

    return sessions


async def replace_gaming_session_members(
    session_id: int,
    members: list[tuple[int, str]],
) -> dict | None:
    now = utc_now_iso()
    unique_members = {
        int(user_id): str(display_name)
        for user_id, display_name in members
    }
    player_count = max(
        len(unique_members),
        1,
    )

    async with database_connection() as db:
        try:
            await db.execute(
                """
                DELETE FROM gaming_session_members
                WHERE session_id = ?
                """,
                (int(session_id),),
            )
            await db.executemany(
                """
                INSERT INTO gaming_session_members (
                    session_id,
                    user_id,
                    display_name,
                    joined_at,
                    source
                )
                VALUES (?, ?, ?, ?, 'voice')
                """,
                (
                    (
                        int(session_id),
                        user_id,
                        display_name,
                        now,
                    )
                    for user_id, display_name
                    in unique_members.items()
                ),
            )
            await db.execute(
                """
                UPDATE gaming_sessions
                SET
                    player_count = ?,
                    cache_generation = cache_generation + 1
                WHERE id = ? AND status = 'active'
                """,
                (
                    player_count,
                    int(session_id),
                ),
            )
            await db.commit()

        except Exception:
            await db.rollback()
            raise

    return await get_gaming_session(session_id)


async def add_gaming_session_member(
    session_id: int,
    *,
    user_id: int,
    display_name: str,
) -> dict | None:
    now = utc_now_iso()

    async with database_connection() as db:
        await db.execute(
            """
            INSERT INTO gaming_session_members (
                session_id,
                user_id,
                display_name,
                joined_at,
                source
            )
            SELECT ?, ?, ?, ?, 'button'
            WHERE EXISTS (
                SELECT 1
                FROM gaming_sessions
                WHERE id = ? AND status = 'active'
            )
            ON CONFLICT(session_id, user_id)
            DO UPDATE SET display_name = excluded.display_name
            """,
            (
                int(session_id),
                int(user_id),
                str(display_name),
                now,
                int(session_id),
            ),
        )
        await db.commit()

    return await get_gaming_session(session_id)


async def remove_gaming_session_member(
    session_id: int,
    *,
    user_id: int,
) -> dict | None:
    async with database_connection() as db:
        await db.execute(
            """
            DELETE FROM gaming_session_members
            WHERE session_id = ? AND user_id = ?
            """,
            (
                int(session_id),
                int(user_id),
            ),
        )
        await db.commit()

    return await get_gaming_session(session_id)


async def configure_gaming_session(
    session_id: int,
    *,
    manual_player_count: int | None = None,
    clear_manual_player_count: bool = False,
    include_unverified: bool | None = None,
    use_normal_wheel: bool | None = None,
) -> dict | None:
    assignments = [
        "cache_generation = cache_generation + 1"
    ]
    parameters = []

    if clear_manual_player_count:
        assignments.append(
            "manual_player_count = NULL"
        )
    elif manual_player_count is not None:
        assignments.append(
            "manual_player_count = ?"
        )
        parameters.append(
            max(int(manual_player_count), 1)
        )

    if include_unverified is not None:
        assignments.append(
            "include_unverified = ?"
        )
        parameters.append(
            int(bool(include_unverified))
        )

    if use_normal_wheel is not None:
        assignments.append(
            "use_normal_wheel = ?"
        )
        parameters.append(
            int(bool(use_normal_wheel))
        )

    parameters.append(
        int(session_id)
    )

    async with database_connection() as db:
        await db.execute(
            f"""
            UPDATE gaming_sessions
            SET {', '.join(assignments)}
            WHERE id = ? AND status = 'active'
            """,
            tuple(parameters),
        )
        await db.commit()

    return await get_gaming_session(session_id)


async def select_gaming_session_game(
    session_id: int,
    *,
    selected_by_id: int,
    selected_by_name: str,
    game_id: int | None = None,
    custom_name: str | None = None,
    custom_link: str | None = None,
    custom_store: str | None = None,
    custom_image_url: str | None = None,
) -> dict | None:
    async with database_connection() as db:
        await db.execute(
            """
            UPDATE gaming_sessions
            SET
                selected_game_id = ?,
                custom_game_name = ?,
                custom_game_link = ?,
                custom_game_store = ?,
                custom_game_image_url = ?,
                selected_by_id = ?,
                selected_by_name = ?
            WHERE id = ? AND status = 'active'
            """,
            (
                int(game_id) if game_id is not None else None,
                _clean_optional_text(custom_name),
                _clean_optional_text(custom_link),
                _clean_optional_text(custom_store),
                _clean_optional_text(custom_image_url),
                int(selected_by_id),
                str(selected_by_name),
                int(session_id),
            ),
        )
        await db.commit()

    return await get_gaming_session(session_id)


async def start_gaming_session_game(
    session_id: int,
    *,
    game_name: str,
    game_id: int | None = None,
    game_link: str | None = None,
    selected_by_id: int | None = None,
    selected_by_name: str | None = None,
) -> dict | None:
    """Start one timeline entry for the game currently locked in."""

    now = utc_now_iso()

    async with database_connection() as db:
        try:
            await db.execute(
                """
                UPDATE gaming_session_games
                SET finished_at = ?
                WHERE session_id = ? AND finished_at IS NULL
                """,
                (now, int(session_id)),
            )
            await db.execute(
                """
                INSERT INTO gaming_session_games (
                    session_id,
                    game_id,
                    game_name,
                    game_link,
                    selected_by_id,
                    selected_by_name,
                    started_at
                )
                SELECT ?, ?, ?, ?, ?, ?, ?
                WHERE EXISTS (
                    SELECT 1
                    FROM gaming_sessions
                    WHERE id = ? AND status = 'active'
                )
                """,
                (
                    int(session_id),
                    int(game_id) if game_id is not None else None,
                    str(game_name),
                    _clean_optional_text(game_link),
                    (
                        int(selected_by_id)
                        if selected_by_id is not None
                        else None
                    ),
                    _clean_optional_text(selected_by_name),
                    now,
                    int(session_id),
                ),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    return await get_gaming_session(session_id)


async def finish_gaming_session_game(
    session_id: int,
) -> dict | None:
    """Finish and return the active timeline entry, if one exists."""

    now = utc_now_iso()

    async with database_connection() as db:
        cursor = await db.execute(
            """
            SELECT *
            FROM gaming_session_games
            WHERE session_id = ? AND finished_at IS NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (int(session_id),),
        )
        row = await cursor.fetchone()

        if row is None:
            return None

        await db.execute(
            """
            UPDATE gaming_session_games
            SET finished_at = ?
            WHERE id = ?
            """,
            (now, int(row["id"])),
        )
        await db.commit()
        result = dict(row)
        result["finished_at"] = now
        return result


async def save_gaming_session_game_lock_message(
    session_id: int,
    *,
    channel_id: int,
    message_id: int,
) -> dict | None:
    async with database_connection() as db:
        await db.execute(
            """
            UPDATE gaming_session_games
            SET lock_message_channel_id = ?, lock_message_id = ?
            WHERE id = (
                SELECT id
                FROM gaming_session_games
                WHERE session_id = ? AND finished_at IS NULL
                ORDER BY id DESC
                LIMIT 1
            )
            """,
            (
                int(channel_id),
                int(message_id),
                int(session_id),
            ),
        )
        await db.commit()

        cursor = await db.execute(
            """
            SELECT *
            FROM gaming_session_games
            WHERE session_id = ? AND finished_at IS NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (int(session_id),),
        )
        row = await cursor.fetchone()
        return dict(row) if row is not None else None


async def cancel_gaming_session_game(
    session_id: int,
) -> dict | None:
    """Discard the unfinished timeline entry when a lock-in is cancelled."""

    async with database_connection() as db:
        cursor = await db.execute(
            """
            SELECT *
            FROM gaming_session_games
            WHERE session_id = ? AND finished_at IS NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (int(session_id),),
        )
        row = await cursor.fetchone()
        await db.execute(
            """
            DELETE FROM gaming_session_games
            WHERE session_id = ? AND finished_at IS NULL
            """,
            (int(session_id),),
        )
        await db.commit()
        return dict(row) if row is not None else None


async def transfer_gaming_session_host(
    session_id: int,
    *,
    user_id: int,
    display_name: str,
) -> dict | None:
    """Transfer an active session to one of its joined members."""

    async with database_connection() as db:
        cursor = await db.execute(
            """
            UPDATE gaming_sessions
            SET host_id = ?, host_name = ?
            WHERE
                id = ?
                AND status = 'active'
                AND EXISTS (
                    SELECT 1
                    FROM gaming_session_members
                    WHERE session_id = ? AND user_id = ?
                )
            """,
            (
                int(user_id),
                str(display_name),
                int(session_id),
                int(session_id),
                int(user_id),
            ),
        )
        await db.commit()

        if cursor.rowcount == 0:
            return None

    return await get_gaming_session(session_id)


async def clear_gaming_session_selection(
    session_id: int,
) -> dict | None:
    async with database_connection() as db:
        await db.execute(
            """
            UPDATE gaming_sessions
            SET
                selected_game_id = NULL,
                custom_game_name = NULL,
                custom_game_link = NULL,
                custom_game_store = NULL,
                custom_game_image_url = NULL,
                selected_by_id = NULL,
                selected_by_name = NULL,
                cache_generation = cache_generation + 1
            WHERE id = ? AND status = 'active'
            """,
            (int(session_id),),
        )
        await db.commit()

    return await get_gaming_session(session_id)


async def end_gaming_session(
    session_id: int,
) -> dict | None:
    now = utc_now_iso()

    async with database_connection() as db:
        try:
            await db.execute(
                """
                UPDATE gaming_session_games
                SET finished_at = ?
                WHERE session_id = ? AND finished_at IS NULL
                """,
                (now, int(session_id)),
            )
            await db.execute(
                """
                UPDATE gaming_sessions
                SET status = 'ended', ended_at = ?
                WHERE id = ? AND status IN ('starting', 'active')
                """,
                (
                    now,
                    int(session_id),
                ),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    return await get_gaming_session(session_id)


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
            SELECT
                id,
                name,
                store_link,
                store,
                external_id
            FROM games
            WHERE name = ? COLLATE NOCASE
            LIMIT 1
            """,
            (game_name.strip(),),
        )

        row = await cursor.fetchone()

        if not row:
            return False

        (
            game_id,
            stored_name,
            store_link,
            store,
            external_id,
        ) = row

        await db.execute(
            """
            INSERT OR IGNORE INTO removed_games (
                name,
                store,
                store_link,
                external_id,
                removed_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                stored_name,
                store,
                store_link,
                external_id,
                utc_now_iso(),
            ),
        )

        await db.execute(
            """
            DELETE FROM game_history
            WHERE game_id = ?
            """,
            (game_id,),
        )

        await db.execute(
            """
            DELETE FROM store_replacements
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


def _game_night_week_from_row(row) -> dict | None:
    if row is None:
        return None

    record = dict(row)

    for column_name in (
        "reminder_24h_sent",
        "reminder_6h_sent",
        "reminder_1h_sent",
        "checkin_closed",
    ):
        record[column_name] = bool(
            record.get(column_name)
        )

    return record


async def get_game_night_week(
    week_start: str,
) -> dict | None:
    async with database_connection() as db:
        cursor = await db.execute(
            """
            SELECT *
            FROM game_night_weeks
            WHERE week_start = ?
            LIMIT 1
            """,
            (str(week_start),),
        )
        return _game_night_week_from_row(
            await cursor.fetchone()
        )


async def save_game_night_poll(
    *,
    week_start: str,
    channel_id: int,
    message_id: int,
    created_at: str,
    closes_at: str,
) -> dict:
    now = utc_now_iso()

    async with database_connection() as db:
        await db.execute(
            """
            INSERT INTO game_night_weeks (
                week_start,
                poll_channel_id,
                poll_message_id,
                poll_created_at,
                poll_closes_at,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(week_start) DO UPDATE SET
                poll_channel_id = excluded.poll_channel_id,
                poll_message_id = excluded.poll_message_id,
                poll_created_at = excluded.poll_created_at,
                poll_closes_at = excluded.poll_closes_at,
                updated_at = excluded.updated_at
            """,
            (
                str(week_start),
                int(channel_id),
                int(message_id),
                str(created_at),
                str(closes_at),
                now,
                now,
            ),
        )
        await db.commit()

    record = await get_game_night_week(
        week_start
    )
    assert record is not None
    return record


async def save_game_night_result(
    *,
    week_start: str,
    winner_day: str,
    winner_reason: str,
    friday_votes: int,
    saturday_votes: int,
    scheduled_event_id: int,
    event_start_at: str,
) -> dict:
    now = utc_now_iso()

    async with database_connection() as db:
        await db.execute(
            """
            INSERT INTO game_night_weeks (
                week_start,
                winner_day,
                winner_reason,
                friday_votes,
                saturday_votes,
                scheduled_event_id,
                event_start_at,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(week_start) DO UPDATE SET
                winner_day = excluded.winner_day,
                winner_reason = excluded.winner_reason,
                friday_votes = excluded.friday_votes,
                saturday_votes = excluded.saturday_votes,
                scheduled_event_id = excluded.scheduled_event_id,
                event_start_at = excluded.event_start_at,
                updated_at = excluded.updated_at
            """,
            (
                str(week_start),
                str(winner_day),
                str(winner_reason),
                max(0, int(friday_votes)),
                max(0, int(saturday_votes)),
                int(scheduled_event_id),
                str(event_start_at),
                now,
                now,
            ),
        )
        await db.commit()

    record = await get_game_night_week(
        week_start
    )
    assert record is not None
    return record


_GAME_NIGHT_REMINDER_COLUMNS = {
    24: "reminder_24h_sent",
    6: "reminder_6h_sent",
    1: "reminder_1h_sent",
}


async def set_game_night_reminders(
    week_start: str,
    reminder_hours,
    *,
    sent: bool,
) -> None:
    columns = []

    for raw_hours in reminder_hours:
        hours = int(raw_hours)
        column_name = _GAME_NIGHT_REMINDER_COLUMNS.get(
            hours
        )

        if column_name is None:
            raise ValueError(
                f"Unsupported game-night reminder: {hours}h"
            )

        if column_name not in columns:
            columns.append(column_name)

    if not columns:
        return

    assignments = ", ".join(
        f"{column_name} = ?"
        for column_name in columns
    )
    value = 1 if sent else 0

    async with database_connection() as db:
        await db.execute(
            f"""
            UPDATE game_night_weeks
            SET
                {assignments},
                updated_at = ?
            WHERE week_start = ?
            """,
            (
                *([value] * len(columns)),
                utc_now_iso(),
                str(week_start),
            ),
        )
        await db.commit()


async def save_game_night_checkin_message(
    week_start: str,
    *,
    channel_id: int,
    message_id: int,
) -> dict | None:
    async with database_connection() as db:
        await db.execute(
            """
            UPDATE game_night_weeks
            SET
                checkin_channel_id = ?,
                checkin_message_id = ?,
                checkin_closed = 0,
                updated_at = ?
            WHERE week_start = ?
            """,
            (
                int(channel_id),
                int(message_id),
                utc_now_iso(),
                str(week_start),
            ),
        )
        await db.commit()

    return await get_game_night_week(week_start)


async def set_game_night_checkin(
    week_start: str,
    *,
    user_id: int,
    display_name: str,
    response: str,
) -> dict | None:
    clean_response = str(response)

    if clean_response not in {
        "playing",
        "maybe",
        "cant_make_it",
    }:
        raise ValueError("Unsupported Game Night check-in response")

    async with database_connection() as db:
        week_cursor = await db.execute(
            """
            SELECT checkin_closed
            FROM game_night_weeks
            WHERE week_start = ?
            """,
            (str(week_start),),
        )
        week = await week_cursor.fetchone()

        if week is None or bool(week["checkin_closed"]):
            return None

        await db.execute(
            """
            INSERT INTO game_night_checkins (
                week_start,
                user_id,
                display_name,
                response,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(week_start, user_id) DO UPDATE SET
                display_name = excluded.display_name,
                response = excluded.response,
                updated_at = excluded.updated_at
            """,
            (
                str(week_start),
                int(user_id),
                str(display_name),
                clean_response,
                utc_now_iso(),
            ),
        )
        await db.commit()

    return await get_game_night_checkins(week_start)


async def get_game_night_checkins(
    week_start: str,
) -> dict:
    responses = {
        "playing": [],
        "maybe": [],
        "cant_make_it": [],
    }

    async with database_connection() as db:
        cursor = await db.execute(
            """
            SELECT user_id, display_name, response, updated_at
            FROM game_night_checkins
            WHERE week_start = ?
            ORDER BY updated_at, user_id
            """,
            (str(week_start),),
        )
        rows = await cursor.fetchall()

    for row in rows:
        responses[str(row["response"])].append(dict(row))

    return {
        "responses": responses,
        "counts": {
            response: len(members)
            for response, members in responses.items()
        },
        "total": len(rows),
    }


async def close_game_night_checkin(
    week_start: str,
) -> dict | None:
    async with database_connection() as db:
        await db.execute(
            """
            UPDATE game_night_weeks
            SET checkin_closed = 1, updated_at = ?
            WHERE week_start = ?
            """,
            (utc_now_iso(), str(week_start)),
        )
        await db.commit()

    return await get_game_night_week(week_start)
