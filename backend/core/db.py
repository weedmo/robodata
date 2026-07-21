"""Postgres metadata layer with a thin aiosqlite-style compatibility wrapper."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Iterable, Sequence
from urllib.parse import urlsplit, urlunsplit

import asyncpg

from backend.core.config import settings

logger = logging.getLogger(__name__)

_LATEST_SCHEMA_VERSION = 2
_SCHEMA_MIGRATION_LOCK_KEY = "curation_tools_schema_migrations"

_pool: asyncpg.Pool | None = None
_pool_loop: asyncio.AbstractEventLoop | None = None
_db_url_override: str | None = None  # for tests
_db_path_override: str | None = None  # legacy SQLite-era test isolation marker
_db_path_override_initialized: str | None = None
_active_states: dict[int, "_ConnectionState"] = {}


async def _release_pool_connection(pool: asyncpg.Pool, conn: asyncpg.Connection) -> None:
    try:
        await pool.release(conn)
    except asyncpg.exceptions.ConnectionDoesNotExistError:
        logger.warning("Discarding dead Postgres connection during pool release", exc_info=True)
        try:
            conn.terminate()
        except Exception:
            logger.debug("Failed to terminate dead Postgres connection", exc_info=True)


@dataclass
class _ConnectionState:
    owner: "_DB"
    conn: asyncpg.Connection
    task: asyncio.Task[Any] | None
    pending_tx: asyncpg.Transaction | None = None
    explicit_depth: int = 0
    released: bool = False


def _match_dollar_quote_tag(sql: str, start: int) -> str | None:
    if sql[start] != "$":
        return None
    end = sql.find("$", start + 1)
    if end == -1:
        return None
    inner = sql[start + 1 : end]
    if inner and not (inner[0].isalpha() or inner[0] == "_"):
        return None
    if any(not (char.isalnum() or char == "_") for char in inner):
        return None
    return sql[start : end + 1]


def _translate(sql: str) -> str:
    """Translate SQLite-style ? placeholders to asyncpg's $N placeholders.

    Question marks inside strings, identifiers, comments, and dollar-quoted
    strings are preserved.
    """

    if "?" not in sql:
        return sql

    out: list[str] = []
    placeholder_index = 1
    i = 0
    in_single = False
    in_double = False
    in_line_comment = False
    block_comment_depth = 0
    dollar_tag: str | None = None
    saw_qmark = False
    saw_numbered = False

    while i < len(sql):
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < len(sql) else ""

        if in_line_comment:
            out.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue

        if block_comment_depth > 0:
            if ch == "/" and nxt == "*":
                out.extend((ch, nxt))
                block_comment_depth += 1
                i += 2
                continue
            if ch == "*" and nxt == "/":
                out.extend((ch, nxt))
                block_comment_depth -= 1
                i += 2
                continue
            out.append(ch)
            i += 1
            continue

        if dollar_tag is not None:
            if sql.startswith(dollar_tag, i):
                out.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
                continue
            out.append(ch)
            i += 1
            continue

        if in_single:
            out.append(ch)
            if ch == "'":
                if nxt == "'":
                    out.append(nxt)
                    i += 2
                    continue
                in_single = False
            i += 1
            continue

        if in_double:
            out.append(ch)
            if ch == '"':
                if nxt == '"':
                    out.append(nxt)
                    i += 2
                    continue
                in_double = False
            i += 1
            continue

        if ch == "-" and nxt == "-":
            out.extend((ch, nxt))
            in_line_comment = True
            i += 2
            continue

        if ch == "/" and nxt == "*":
            out.extend((ch, nxt))
            block_comment_depth = 1
            i += 2
            continue

        if ch == "'":
            in_single = True
            out.append(ch)
            i += 1
            continue

        if ch == '"':
            in_double = True
            out.append(ch)
            i += 1
            continue

        tag = _match_dollar_quote_tag(sql, i)
        if tag is not None:
            dollar_tag = tag
            out.append(tag)
            i += len(tag)
            continue

        if ch == "$" and nxt.isdigit():
            saw_numbered = True
            if saw_qmark:
                raise ValueError("mixed placeholder styles are ambiguous")
            out.append(ch)
            i += 1
            continue

        if ch == "?":
            saw_qmark = True
            if saw_numbered:
                raise ValueError("mixed placeholder styles are ambiguous")
            out.append(f"${placeholder_index}")
            placeholder_index += 1
        else:
            out.append(ch)
        i += 1

    return "".join(out)


def _analysis_sql(sql: str) -> str:
    """Return SQL with literal/comment bodies blanked out for structural inspection."""

    out: list[str] = []
    i = 0
    in_single = False
    in_double = False
    in_line_comment = False
    block_comment_depth = 0
    dollar_tag: str | None = None

    while i < len(sql):
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < len(sql) else ""

        if in_line_comment:
            out.append("\n" if ch == "\n" else " ")
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue

        if block_comment_depth > 0:
            if ch == "/" and nxt == "*":
                out.extend((" ", " "))
                block_comment_depth += 1
                i += 2
                continue
            if ch == "*" and nxt == "/":
                out.extend((" ", " "))
                block_comment_depth -= 1
                i += 2
                continue
            out.append(" ")
            i += 1
            continue

        if dollar_tag is not None:
            if sql.startswith(dollar_tag, i):
                out.extend(" " * len(dollar_tag))
                i += len(dollar_tag)
                dollar_tag = None
                continue
            out.append(" ")
            i += 1
            continue

        if in_single:
            out.append(" ")
            if ch == "'":
                if nxt == "'":
                    out.append(" ")
                    i += 2
                    continue
                in_single = False
            i += 1
            continue

        if in_double:
            out.append(" ")
            if ch == '"':
                if nxt == '"':
                    out.append(" ")
                    i += 2
                    continue
                in_double = False
            i += 1
            continue

        if ch == "-" and nxt == "-":
            out.extend((" ", " "))
            in_line_comment = True
            i += 2
            continue

        if ch == "/" and nxt == "*":
            out.extend((" ", " "))
            block_comment_depth = 1
            i += 2
            continue

        if ch == "'":
            in_single = True
            out.append(" ")
            i += 1
            continue

        if ch == '"':
            in_double = True
            out.append(" ")
            i += 1
            continue

        tag = _match_dollar_quote_tag(sql, i)
        if tag is not None:
            dollar_tag = tag
            out.extend(" " * len(tag))
            i += len(tag)
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def _rowcount_from_status(status: str) -> int:
    parts = status.split()
    for part in reversed(parts):
        if part.isdigit():
            return int(part)
    return 0


def _status_is_read_only(status: str) -> bool:
    head = status.split(maxsplit=1)[0].upper() if status else ""
    return head in {"SELECT", "SHOW", "EXPLAIN", "FETCH"}


def _is_data_modifying_cte(sql: str) -> bool:
    analysis = _analysis_sql(sql).lower()
    i = 0
    n = len(analysis)

    def skip_ws(pos: int) -> int:
        while pos < n and analysis[pos].isspace():
            pos += 1
        return pos

    def parse_identifier(pos: int) -> int:
        pos = skip_ws(pos)
        start = pos
        while pos < n and (analysis[pos].isalnum() or analysis[pos] == "_"):
            pos += 1
        return pos if pos > start else start

    i = skip_ws(i)
    if not analysis.startswith("with", i):
        return False
    i = skip_ws(i + 4)
    if analysis.startswith("recursive", i):
        i = skip_ws(i + len("recursive"))

    while i < n:
        ident_end = parse_identifier(i)
        if ident_end == i:
            return False
        i = skip_ws(ident_end)

        if i < n and analysis[i] == "(":
            depth = 1
            i += 1
            while i < n and depth > 0:
                if analysis[i] == "(":
                    depth += 1
                elif analysis[i] == ")":
                    depth -= 1
                i += 1
            i = skip_ws(i)

        if not analysis.startswith("as", i):
            return False
        i = skip_ws(i + 2)

        if analysis.startswith("not materialized", i):
            i = skip_ws(i + len("not materialized"))
        elif analysis.startswith("materialized", i):
            i = skip_ws(i + len("materialized"))

        if i >= n or analysis[i] != "(":
            return False

        depth = 1
        body_start = i + 1
        i += 1
        while i < n and depth > 0:
            if analysis[i] == "(":
                depth += 1
            elif analysis[i] == ")":
                depth -= 1
            i += 1
        body = analysis[body_start : i - 1].lstrip()
        if body.startswith(("insert", "update", "delete", "merge")):
            return True

        i = skip_ws(i)
        if i < n and analysis[i] == ",":
            i = skip_ws(i + 1)
            continue
        return False

    return False


def _statement_is_read_only(sql: str, status: str) -> bool:
    return _status_is_read_only(status) and not _is_data_modifying_cte(sql)


def _redacted_db_target() -> str:
    raw = _db_url_override or settings.db_url
    try:
        split = urlsplit(raw)
    except ValueError:
        return "<configured database>"

    if not split.scheme:
        return "<configured database>"

    host = split.hostname or ""
    port = f":{split.port}" if split.port else ""
    path = split.path or ""
    return urlunsplit((split.scheme, f"{host}{port}", path, "", ""))


class _Cursor:
    """Minimal async cursor facade matching what existing call sites use."""

    def __init__(self, rows: Sequence[asyncpg.Record]):
        self._rows = rows
        self._index = 0

    async def fetchone(self) -> asyncpg.Record | None:
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    async def fetchall(self) -> list[asyncpg.Record]:
        if self._index == 0:
            self._index = len(self._rows)
            return list(self._rows)
        remaining = list(self._rows[self._index :])
        self._index = len(self._rows)
        return remaining

    async def __aenter__(self) -> "_Cursor":
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        return None


class _ExecuteOperation:
    """Awaitable + async-context-manager returned by _DB.execute()."""

    def __init__(self, db: "_DB", sql: str, params: tuple[Any, ...]):
        self._db = db
        self._sql = _translate(sql)
        self._params = params

    def __await__(self):
        return self._run().__await__()

    async def _run(self) -> _Cursor:
        state = self._db._current_state()
        if state is not None:
            return await self._run_with_state(state)
        return await self._run_without_state()

    async def _run_with_state(self, state: _ConnectionState) -> _Cursor:
        try:
            return await self._execute_on_connection(state.conn)
        except Exception:
            await self._db._handle_statement_failure(state)
            raise

    async def _run_without_state(self) -> _Cursor:
        conn = await self._db._pool.acquire()
        keep_connection = False
        try:
            stmt = await conn.prepare(self._sql)
            if stmt.get_attributes():
                tx = conn.transaction()
                await tx.start()
                try:
                    rows = await stmt.fetch(*self._params)
                except Exception:
                    await tx.rollback()
                    raise
                status = stmt.get_statusmsg()
                if _statement_is_read_only(self._sql, status):
                    await tx.rollback()
                    return _Cursor(rows)

                state = _ConnectionState(owner=self._db, conn=conn, task=self._db._current_task(), pending_tx=tx)
                self._db._register_state(state)
                self._db.total_changes += _rowcount_from_status(status)
                keep_connection = True
                return _Cursor(rows)

            tx = conn.transaction()
            await tx.start()
            try:
                status = await conn.execute(self._sql, *self._params)
            except Exception:
                await tx.rollback()
                raise

            state = _ConnectionState(owner=self._db, conn=conn, task=self._db._current_task(), pending_tx=tx)
            self._db._register_state(state)
            self._db.total_changes += _rowcount_from_status(status)
            keep_connection = True
            return _Cursor(())
        except Exception:
            raise
        finally:
            if not keep_connection:
                await _release_pool_connection(self._db._pool, conn)

    async def _execute_on_connection(self, conn: asyncpg.Connection) -> _Cursor:
        stmt = await conn.prepare(self._sql)
        if stmt.get_attributes():
            rows = await stmt.fetch(*self._params)
            status = stmt.get_statusmsg()
            if not _statement_is_read_only(self._sql, status):
                self._db.total_changes += _rowcount_from_status(status)
            return _Cursor(rows)

        status = await conn.execute(self._sql, *self._params)
        self._db.total_changes += _rowcount_from_status(status)
        return _Cursor(())

    async def __aenter__(self) -> _Cursor:
        return await self._run()

    async def __aexit__(self, *_exc_info: object) -> None:
        return None


class _DB:
    """Pool-backed facade that preserves the old aiosqlite-style call sites."""

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool
        self.total_changes = 0
        self._task_states: dict[asyncio.Task[Any], _ConnectionState] = {}

    def execute(self, sql: str, params: Iterable[Any] = ()) -> _ExecuteOperation:
        return _ExecuteOperation(self, sql, tuple(params))

    async def executemany(self, sql: str, seq_of_params: Iterable[Iterable[Any]]) -> None:
        for params in seq_of_params:
            await self.execute(sql, params)

    async def executescript(self, sql: str) -> None:
        state = self._current_state()
        if state is not None:
            try:
                await state.conn.execute(sql)
            except Exception:
                await self._handle_statement_failure(state)
                raise
            return

        conn = await self._pool.acquire()
        try:
            async with conn.transaction():
                await conn.execute(sql)
        finally:
            await _release_pool_connection(self._pool, conn)

    async def commit(self) -> None:
        state = self._current_state()
        if state is None or state.pending_tx is None or state.explicit_depth > 0:
            return
        try:
            await state.pending_tx.commit()
        finally:
            await self._release_state(state)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator["_DB"]:
        state = self._current_state()
        created_state = False
        if state is None:
            conn = await self._pool.acquire()
            state = _ConnectionState(owner=self, conn=conn, task=self._current_task())
            self._register_state(state)
            created_state = True

        tx = state.conn.transaction()
        state.explicit_depth += 1
        await tx.start()
        try:
            yield self
        except Exception:
            await tx.rollback()
            raise
        else:
            await tx.commit()
        finally:
            state.explicit_depth -= 1
            if created_state:
                await self._release_state(state)

    async def _handle_statement_failure(self, state: _ConnectionState) -> None:
        if state.pending_tx is None or state.explicit_depth > 0:
            return
        try:
            await state.pending_tx.rollback()
        finally:
            await self._release_state(state)

    def _current_task(self) -> asyncio.Task[Any] | None:
        return asyncio.current_task()

    def _current_state(self) -> _ConnectionState | None:
        task = self._current_task()
        if task is None:
            return None
        state = self._task_states.get(task)
        if state is not None and state.released:
            self._task_states.pop(task, None)
            return None
        return state

    def _register_state(self, state: _ConnectionState) -> None:
        if state.task is not None:
            self._task_states[state.task] = state
        _active_states[id(state)] = state

    async def _release_state(self, state: _ConnectionState) -> None:
        if state.released:
            return
        state.released = True
        if state.task is not None and self._task_states.get(state.task) is state:
            self._task_states.pop(state.task, None)
        _active_states.pop(id(state), None)
        state.pending_tx = None
        if not state.conn.is_closed():
            await _release_pool_connection(self._pool, state.conn)


_current_facade_db: contextvars.ContextVar[_DB | None] = contextvars.ContextVar(
    "current_facade_db",
    default=None,
)


def _pack_params(params: tuple[Any, ...]) -> tuple[Any, ...]:
    if len(params) == 1 and isinstance(params[0], (list, tuple)):
        return tuple(params[0])
    return params


class _DBFacade:
    async def _conn(self) -> _DB:
        return _current_facade_db.get() or await get_db()

    async def execute(self, sql: str, *params: Any) -> None:
        conn = await self._conn()
        await conn.execute(sql, _pack_params(params))
        if _current_facade_db.get() is None:
            # Outside an explicit facade transaction, finalize Spec-1's
            # task-scoped pending transaction so writes persist.
            await conn.commit()

    async def fetch_one(self, sql: str, *params: Any) -> asyncpg.Record | None:
        conn = await self._conn()
        async with conn.execute(sql, _pack_params(params)) as cur:
            row = await cur.fetchone()
        if _current_facade_db.get() is None:
            await conn.commit()
        return row

    async def fetch_all(self, sql: str, *params: Any) -> list[asyncpg.Record]:
        conn = await self._conn()
        async with conn.execute(sql, _pack_params(params)) as cur:
            rows = await cur.fetchall()
        if _current_facade_db.get() is None:
            await conn.commit()
        return rows

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator["_DBFacade"]:
        conn = await get_db()
        async with conn.transaction():
            token = _current_facade_db.set(conn)
            try:
                yield self
            finally:
                _current_facade_db.reset(token)


db = _DBFacade()


def _effective_url() -> str:
    if _db_url_override:
        return _db_url_override
    if _db_path_override is not None:
        return os.environ.get("CURATION_TEST_DB_URL") or settings.db_url
    return settings.db_url


def _clear_active_states() -> None:
    for state in list(_active_states.values()):
        state.released = True
        if state.task is not None and state.owner._task_states.get(state.task) is state:
            state.owner._task_states.pop(state.task, None)
        _active_states.pop(id(state), None)
        state.pending_tx = None


def _terminate_pool() -> None:
    global _pool, _pool_loop
    if _pool is not None:
        try:
            _pool.terminate()
        except RuntimeError as exc:
            if "Event loop is closed" not in str(exc):
                raise
    _pool = None
    _pool_loop = None


async def _reset_legacy_override_data(db: "_DB") -> None:
    global _db_path_override_initialized
    if _db_path_override is None:
        return
    if _db_path_override_initialized == _db_path_override:
        return

    await db.execute(
        """
        TRUNCATE
            episode_curation_states,
            annotations,
            episode_serials,
            dataset_stats,
            datasets,
            jobs
        RESTART IDENTITY CASCADE
        """
    )
    await db.commit()
    _db_path_override_initialized = _db_path_override


async def get_db() -> _DB:
    global _pool, _pool_loop
    current_loop = asyncio.get_running_loop()
    if _pool is not None and (_pool_loop is None or _pool_loop is not current_loop or _pool_loop.is_closed()):
        _clear_active_states()
        _terminate_pool()
    if _pool is None:
        _pool = await asyncpg.create_pool(dsn=_effective_url(), min_size=1, max_size=10)
        _pool_loop = current_loop
    return _DB(_pool)


async def close_db() -> None:
    global _pool, _pool_loop
    if _pool is not None:
        current_loop = asyncio.get_running_loop()
        if _pool_loop is not None and _pool_loop is not current_loop:
            _clear_active_states()
            _terminate_pool()
            return
        for state in list(_active_states.values()):
            if state.released:
                _active_states.pop(id(state), None)
                continue
            if state.conn.is_closed():
                await state.owner._release_state(state)
                continue
            try:
                if state.conn.is_in_transaction():
                    await state.conn.execute("ROLLBACK")
            finally:
                await state.owner._release_state(state)
        await _pool.close()
        _pool = None
        _pool_loop = None


async def init_db() -> None:
    """Ensure the Postgres schema required by the metadata layer exists."""

    connection = await get_db()
    await connection.executescript(_SCHEMA_ENUM_COMPAT)
    async with connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext(?))",
            (_SCHEMA_MIGRATION_LOCK_KEY,),
        )
        await connection.executescript(_SCHEMA_V1)
        await connection.execute(
            "INSERT INTO schema_versions(version) VALUES (1) ON CONFLICT (version) DO NOTHING"
        )
        async with connection.execute(
            "SELECT 1 FROM schema_versions WHERE version = ?",
            (_LATEST_SCHEMA_VERSION,),
        ) as cursor:
            latest_applied = await cursor.fetchone()
        if latest_applied is None:
            await connection.executescript(_SCHEMA_V2_REMOVE_RRD)
            await connection.execute(
                "INSERT INTO schema_versions(version) VALUES (?)",
                (_LATEST_SCHEMA_VERSION,),
            )
    await _reset_legacy_override_data(connection)
    logger.info(
        "Database initialized (v%s) at %s",
        _LATEST_SCHEMA_VERSION,
        _redacted_db_target(),
    )


def _reset() -> None:
    """Reset module state for tests."""

    global _db_url_override, _db_path_override, _db_path_override_initialized
    _clear_active_states()
    _terminate_pool()
    _db_url_override = None
    _db_path_override = None
    _db_path_override_initialized = None


_SCHEMA_ENUM_COMPAT = """
DO $$ BEGIN
    CREATE TYPE job_type AS ENUM (
        'convert',
        'split',
        'merge',
        'delete',
        'sync_good_episodes',
        'stamp_cycles',
        'auto_bad_camera_flicker',
        'bronze_silver_batch'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

ALTER TYPE job_type ADD VALUE IF NOT EXISTS 'convert';
ALTER TYPE job_type ADD VALUE IF NOT EXISTS 'split';
ALTER TYPE job_type ADD VALUE IF NOT EXISTS 'merge';
ALTER TYPE job_type ADD VALUE IF NOT EXISTS 'delete';
ALTER TYPE job_type ADD VALUE IF NOT EXISTS 'sync_good_episodes';
ALTER TYPE job_type ADD VALUE IF NOT EXISTS 'stamp_cycles';
ALTER TYPE job_type ADD VALUE IF NOT EXISTS 'auto_bad_camera_flicker';
ALTER TYPE job_type ADD VALUE IF NOT EXISTS 'bronze_silver_batch';

DO $$ BEGIN
    CREATE TYPE job_status AS ENUM (
        'queued',
        'running',
        'complete',
        'failed',
        'cancel_requested',
        'cancelled'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'queued';
ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'running';
ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'complete';
ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'failed';
ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'cancel_requested';
ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'cancelled';
"""


_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_versions (
    version    INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS datasets (
    id              SERIAL PRIMARY KEY,
    path            TEXT UNIQUE NOT NULL,
    name            TEXT NOT NULL,
    cell_name       TEXT,
    fps             INTEGER NOT NULL DEFAULT 0,
    total_episodes  INTEGER NOT NULL DEFAULT 0,
    robot_type      TEXT,
    features        JSONB,
    registered_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    synced_at       TIMESTAMPTZ,
    info_json_mtime DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS dataset_stats (
    dataset_id          INTEGER PRIMARY KEY REFERENCES datasets(id) ON DELETE CASCADE,
    graded_count        INTEGER NOT NULL DEFAULT 0,
    good_count          INTEGER NOT NULL DEFAULT 0,
    normal_count        INTEGER NOT NULL DEFAULT 0,
    bad_count           INTEGER NOT NULL DEFAULT 0,
    total_duration_sec  DOUBLE PRECISION NOT NULL DEFAULT 0,
    good_duration_sec   DOUBLE PRECISION NOT NULL DEFAULT 0,
    normal_duration_sec DOUBLE PRECISION NOT NULL DEFAULT 0,
    bad_duration_sec    DOUBLE PRECISION NOT NULL DEFAULT 0,
    updated_at          TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS episode_serials (
    dataset_id      INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    episode_index   INTEGER NOT NULL,
    serial_number   TEXT NOT NULL,
    PRIMARY KEY (dataset_id, episode_index)
);

CREATE INDEX IF NOT EXISTS idx_episode_serials_serial
    ON episode_serials(serial_number);

CREATE TABLE IF NOT EXISTS annotations (
    serial_number   TEXT PRIMARY KEY,
    grade           TEXT CHECK(grade IN ('good','normal','bad')),
    tags            JSONB NOT NULL DEFAULT '[]'::jsonb,
    reason          TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS episode_curation_states (
    serial_number         TEXT PRIMARY KEY,
    cell                  TEXT NOT NULL,
    task                  TEXT NOT NULL,
    bronze_path           TEXT NOT NULL,
    silver_path           TEXT NOT NULL,
    state                 TEXT NOT NULL CHECK (
        state IN (
            'bronze_detected',
            'silver_processing',
            'silver_ready',
            'human_curating',
            'gold_ready',
            'silver_failed'
        )
    ),
    failure_reason        TEXT,
    processing_started_at TIMESTAMPTZ,
    retry_required        BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_episode_curation_states_state
    ON episode_curation_states(state, updated_at);
CREATE INDEX IF NOT EXISTS idx_episode_curation_states_cell_task
    ON episode_curation_states(cell, task);

DO $$ BEGIN
    CREATE TYPE job_type AS ENUM (
        'convert',
        'split',
        'merge',
        'delete',
        'sync_good_episodes',
        'stamp_cycles',
        'auto_bad_camera_flicker',
        'bronze_silver_batch'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE job_status AS ENUM (
        'queued',
        'running',
        'complete',
        'failed',
        'cancel_requested',
        'cancelled'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS jobs (
    id                  BIGSERIAL PRIMARY KEY,
    external_id         TEXT       NOT NULL DEFAULT gen_random_uuid()::text,
    type                job_type   NOT NULL,
    status              job_status NOT NULL DEFAULT 'queued',
    payload             JSONB      NOT NULL DEFAULT '{}'::jsonb,
    progress            JSONB      NOT NULL DEFAULT '{}'::jsonb,
    result              JSONB,
    error               TEXT,
    attempts            INTEGER    NOT NULL DEFAULT 0,
    worker_id           TEXT,
    dedupe_key          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at          TIMESTAMPTZ,
    heartbeat_at        TIMESTAMPTZ,
    cancel_requested_at TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ
);

ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS external_id TEXT NOT NULL DEFAULT gen_random_uuid()::text;

CREATE INDEX IF NOT EXISTS idx_jobs_queued
    ON jobs(type, created_at) WHERE status = 'queued';

CREATE INDEX IF NOT EXISTS idx_jobs_running
    ON jobs(type, worker_id) WHERE status IN ('running', 'cancel_requested');
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_external_id
    ON jobs(external_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_active_dedupe
    ON jobs(type, dedupe_key)
    WHERE dedupe_key IS NOT NULL AND status IN ('queued', 'running', 'cancel_requested');

-- Worker control plane (API-only Worker Control 보강안 §4)
DO $$ BEGIN
  CREATE TYPE worker_state AS ENUM ('running', 'paused', 'draining', 'stopped');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS worker_controls (
    worker_id     TEXT PRIMARY KEY,
    desired_state worker_state NOT NULL DEFAULT 'running',
    updated_by    TEXT,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    note          TEXT
);

CREATE TABLE IF NOT EXISTS worker_heartbeats (
    worker_id        TEXT PRIMARY KEY,
    actual_state     worker_state NOT NULL,
    pid              INTEGER,
    container_id     TEXT,
    last_beat_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    in_flight_job_id BIGINT REFERENCES jobs(id),
    detail           JSONB
);
CREATE INDEX IF NOT EXISTS idx_worker_heartbeats_recent
    ON worker_heartbeats(last_beat_at);

INSERT INTO worker_controls (worker_id, desired_state)
VALUES ('converter', 'running'), ('curation-worker', 'running')
ON CONFLICT (worker_id) DO NOTHING;
"""


_SCHEMA_V2_REMOVE_RRD = """
-- Remove the legacy file-based RRD contract from existing databases.
ALTER TABLE episode_curation_states
    DROP CONSTRAINT IF EXISTS episode_curation_states_state_check;
UPDATE episode_curation_states
SET state = 'silver_ready'
WHERE state = 'silver_ready_rrd';
ALTER TABLE episode_curation_states
    DROP COLUMN IF EXISTS rrd_path;
ALTER TABLE episode_curation_states
    ADD CONSTRAINT episode_curation_states_state_check CHECK (
        state IN (
            'bronze_detected',
            'silver_processing',
            'silver_ready',
            'human_curating',
            'gold_ready',
            'silver_failed'
        )
    ) NOT VALID;
ALTER TABLE episode_curation_states
    VALIDATE CONSTRAINT episode_curation_states_state_check;

UPDATE datasets
SET features = features - 'rrd_path'
WHERE features->>'source' = 'bronze_silver_batch'
  AND features ? 'rrd_path';
"""
