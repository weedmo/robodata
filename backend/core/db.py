"""SQLite metadata layer — connection, schema, and version management."""

from __future__ import annotations

import logging
from pathlib import Path

import aiosqlite

from backend.core.config import settings

logger = logging.getLogger(__name__)

_connection: aiosqlite.Connection | None = None
_db_path_override: str | None = None  # for testing

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS datasets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    path            TEXT UNIQUE NOT NULL,
    name            TEXT NOT NULL,
    cell_name       TEXT,
    fps             INTEGER DEFAULT 0,
    total_episodes  INTEGER DEFAULT 0,
    robot_type      TEXT,
    features        TEXT,
    registered_at   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    synced_at       TEXT
);

CREATE TABLE IF NOT EXISTS episode_annotations (
    dataset_id      INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    episode_index   INTEGER NOT NULL,
    grade           TEXT CHECK(grade IN ('good', 'normal', 'bad')),
    tags            TEXT DEFAULT '[]',
    updated_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (dataset_id, episode_index)
);

CREATE TABLE IF NOT EXISTS dataset_stats (
    dataset_id          INTEGER PRIMARY KEY REFERENCES datasets(id) ON DELETE CASCADE,
    graded_count        INTEGER DEFAULT 0,
    good_count          INTEGER DEFAULT 0,
    normal_count        INTEGER DEFAULT 0,
    bad_count           INTEGER DEFAULT 0,
    total_duration_sec  REAL DEFAULT 0,
    good_duration_sec   REAL DEFAULT 0,
    normal_duration_sec REAL DEFAULT 0,
    bad_duration_sec    REAL DEFAULT 0,
    updated_at          TEXT
);
"""

SCHEMA_V2 = """
ALTER TABLE episode_annotations ADD COLUMN reason TEXT;
"""

SCHEMA_V3 = """
"""

SCHEMA_V4 = """
CREATE TABLE IF NOT EXISTS episode_serials (
    dataset_id      INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    episode_index   INTEGER NOT NULL,
    serial_number   TEXT NOT NULL,
    PRIMARY KEY (dataset_id, episode_index)
);
CREATE INDEX IF NOT EXISTS idx_episode_serials_serial ON episode_serials(serial_number);

CREATE TABLE IF NOT EXISTS annotations (
    serial_number   TEXT PRIMARY KEY,
    grade           TEXT CHECK(grade IN ('good','normal','bad')),
    tags            TEXT DEFAULT '[]',
    reason          TEXT,
    updated_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
"""


def _get_db_path() -> Path:
    if _db_path_override:
        return Path(_db_path_override)
    if settings.db_path:
        return Path(settings.db_path)
    return Path.home() / ".local" / "share" / "curation-tools" / "metadata.db"


async def get_db() -> aiosqlite.Connection:
    """Return the singleton DB connection, creating it on first call."""
    global _connection
    if _connection is None:
        db_path = _get_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _connection = await aiosqlite.connect(str(db_path))
        _connection.row_factory = aiosqlite.Row
        await _connection.execute("PRAGMA journal_mode=WAL")
        await _connection.execute("PRAGMA foreign_keys=ON")
    return _connection


async def init_db() -> None:
    """Create tables if needed and run version migrations."""
    db = await get_db()
    async with db.execute("PRAGMA user_version") as cursor:
        row = await cursor.fetchone()
    version = row[0] if row else 0
    if version < 1:
        await db.executescript(SCHEMA_V1)
        await db.execute("PRAGMA user_version = 1")
        await db.commit()
        logger.info("Database initialized (v1) at %s", _get_db_path())
        version = 1
    if version < 2:
        await db.executescript(SCHEMA_V2)
        await db.execute("PRAGMA user_version = 2")
        await db.commit()
        logger.info("Database upgraded to v2 (reason column) at %s", _get_db_path())
    if version < 3:
        await db.executescript(SCHEMA_V3)
        await db.execute("PRAGMA user_version = 3")
        await db.commit()
        logger.info("Database upgraded to v3 (reserved compatibility step) at %s", _get_db_path())
        version = 3
    if version < 4:
        if not await _column_exists(db, "datasets", "info_json_mtime"):
            await db.execute("ALTER TABLE datasets ADD COLUMN info_json_mtime REAL")
        await db.executescript(SCHEMA_V4)
        if await _table_exists(db, "episode_annotations"):
            async with db.execute("SELECT COUNT(*) FROM episode_annotations") as cursor:
                legacy_rows = (await cursor.fetchone())[0]
            if legacy_rows == 0:
                await db.execute("DROP TABLE episode_annotations")
            else:
                logger.warning(
                    "Database %s upgraded to v4 compatibility mode with %d legacy "
                    "episode_annotations rows preserved for lazy migration",
                    _get_db_path(),
                    legacy_rows,
                )
        await db.execute("PRAGMA user_version = 4")
        await db.commit()
        logger.info("Database upgraded to v4 (serial-keyed annotations) at %s", _get_db_path())


async def close_db() -> None:
    """Close the DB connection."""
    global _connection
    if _connection is not None:
        await _connection.close()
        _connection = None


def _reset() -> None:
    """Reset module state (for testing only)."""
    global _connection, _db_path_override
    _connection = None
    _db_path_override = None


async def _table_exists(db: aiosqlite.Connection, table_name: str) -> bool:
    async with db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (table_name,),
    ) as cursor:
        row = await cursor.fetchone()
    return row is not None


async def _column_exists(db: aiosqlite.Connection, table_name: str, column_name: str) -> bool:
    async with db.execute(f"PRAGMA table_info({table_name})") as cursor:
        rows = await cursor.fetchall()
    return any(row[1] == column_name for row in rows)
