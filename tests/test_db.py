"""Smoke tests for the Postgres metadata layer."""

import asyncio
import logging

import asyncpg
import pytest

from backend.core.db import _translate, close_db, get_db, init_db, _reset

pytestmark = pytest.mark.usefixtures("_point_settings_at_test_db")


@pytest.fixture(autouse=True)
async def reset_db():
    _reset()
    await init_db()
    db = await get_db()
    await db.execute(
        "TRUNCATE TABLE episode_curation_states, jobs, dataset_stats, "
        "episode_serials, datasets, annotations "
        "RESTART IDENTITY CASCADE"
    )
    await db.commit()
    yield
    await close_db()


async def test_schema_tables_exist():
    db = await get_db()
    async with db.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' ORDER BY table_name"
    ) as cur:
        names = [row["table_name"] for row in await cur.fetchall()]
    for expected in (
        "annotations",
        "dataset_stats",
        "datasets",
        "episode_curation_states",
        "episode_serials",
        "jobs",
        "schema_versions",
    ):
        assert expected in names, names


async def test_placeholder_substitution():
    db = await get_db()
    async with db.execute("SELECT $1::int + $2::int AS s", (2, 3)) as cur:
        row = await cur.fetchone()
    assert row["s"] == 5


async def test_question_mark_placeholders_translate():
    db = await get_db()
    async with db.execute("SELECT ?::int AS n", (7,)) as cur:
        row = await cur.fetchone()
    assert row["n"] == 7


async def test_insert_and_read_dataset():
    db = await get_db()
    await db.execute(
        "INSERT INTO datasets(path, name) VALUES (?, ?)",
        ("/tmp/x", "x"),
    )
    await db.commit()
    async with db.execute("SELECT name FROM datasets WHERE path=?", ("/tmp/x",)) as cur:
        row = await cur.fetchone()
    assert row["name"] == "x"


async def test_executemany_updates_total_changes():
    db = await get_db()
    before = db.total_changes
    await db.executemany(
        "INSERT INTO datasets(path, name) VALUES (?, ?)",
        [
            ("/tmp/batch-1", "batch-1"),
            ("/tmp/batch-2", "batch-2"),
        ],
    )
    assert db.total_changes == before + 2
    await db.commit()

    fresh = await get_db()
    async with fresh.execute(
        "SELECT path FROM datasets WHERE path LIKE '/tmp/batch-%' ORDER BY path"
    ) as cur:
        rows = await cur.fetchall()
    assert [row["path"] for row in rows] == ["/tmp/batch-1", "/tmp/batch-2"]


async def test_pending_commit_controls_cross_facade_visibility():
    db = await get_db()
    await db.execute(
        "INSERT INTO datasets(path, name) VALUES (?, ?)",
        ("/tmp/pending", "pending"),
    )

    async with db.execute("SELECT name FROM datasets WHERE path = ?", ("/tmp/pending",)) as cur:
        same_facade = await cur.fetchone()
    assert same_facade["name"] == "pending"

    fresh = await get_db()
    async with fresh.execute("SELECT name FROM datasets WHERE path = ?", ("/tmp/pending",)) as cur:
        before_commit = await cur.fetchone()
    assert before_commit is None

    await db.commit()
    await close_db()

    reopened = await get_db()
    async with reopened.execute("SELECT name FROM datasets WHERE path = ?", ("/tmp/pending",)) as cur:
        after_commit = await cur.fetchone()
    assert after_commit["name"] == "pending"


async def test_failed_pending_transaction_rolls_back_and_clears_state():
    db = await get_db()

    with pytest.raises(Exception):
        await db.execute(
            "INSERT INTO datasets(path, name) VALUES (?, ?)",
            ("/tmp/rollback", "rollback"),
        )
        await db.execute(
            "INSERT INTO datasets(path, name) VALUES (?, ?)",
            ("/tmp/rollback", "duplicate"),
        )

    fresh = await get_db()
    async with fresh.execute("SELECT name FROM datasets WHERE path = ?", ("/tmp/rollback",)) as cur:
        row = await cur.fetchone()
    assert row is None

    await db.execute(
        "INSERT INTO datasets(path, name) VALUES (?, ?)",
        ("/tmp/recovered", "recovered"),
    )
    await db.commit()
    async with fresh.execute("SELECT name FROM datasets WHERE path = ?", ("/tmp/recovered",)) as cur:
        recovered = await cur.fetchone()
    assert recovered["name"] == "recovered"


async def test_nested_transaction_uses_savepoint_semantics():
    db = await get_db()

    with pytest.raises(RuntimeError):
        async with db.transaction():
            await db.execute(
                "INSERT INTO datasets(path, name) VALUES (?, ?)",
                ("/tmp/outer", "outer"),
            )
            async with db.transaction():
                await db.execute(
                    "INSERT INTO datasets(path, name) VALUES (?, ?)",
                    ("/tmp/inner", "inner"),
                )
            raise RuntimeError("force outer rollback")

    fresh = await get_db()
    async with fresh.execute(
        "SELECT path FROM datasets WHERE path IN (?, ?) ORDER BY path",
        ("/tmp/inner", "/tmp/outer"),
    ) as cur:
        rows = await cur.fetchall()
    assert rows == []


def test_translate_preserves_comments_and_dollar_quoted_strings():
    sql = """
    -- line comment ?
    SELECT ?, $$dollar ?$$ AS d, /* block ? */ '?' AS s, "col?name"
    FROM demo
    WHERE value = ?
    """

    translated = _translate(sql)

    assert "-- line comment ?" in translated
    assert "$$dollar ?$$" in translated
    assert "/* block ? */" in translated
    assert "'?'" in translated
    assert '"col?name"' in translated
    assert translated.count("$1") == 1
    assert translated.count("$2") == 1
    assert "?::" not in translated


def test_translate_rejects_mixed_placeholder_styles():
    with pytest.raises(ValueError, match="mixed placeholder styles"):
        _translate("SELECT ?::int, $1")


async def test_execute_detects_row_returning_with_leading_comment():
    db = await get_db()
    async with db.execute(
        "/* leading comment */ WITH payload AS (SELECT ?::int AS n) SELECT n FROM payload",
        (9,),
    ) as cur:
        row = await cur.fetchone()
    assert row["n"] == 9


async def test_init_db_logging_redacts_password(caplog):
    caplog.set_level(logging.INFO, logger="backend.core.db")

    await init_db()

    messages = [record.getMessage() for record in caplog.records]
    assert any("Database initialized" in message for message in messages)
    assert all("dev-only-change-me" not in message for message in messages)


async def test_close_db_releases_pending_state():
    db = await get_db()
    await db.execute(
        "INSERT INTO datasets(path, name) VALUES (?, ?)",
        ("/tmp/pending-close", "pending-close"),
    )

    await asyncio.wait_for(close_db(), timeout=1.0)

    reopened = await get_db()
    async with reopened.execute("SELECT name FROM datasets WHERE path = ?", ("/tmp/pending-close",)) as cur:
        row = await cur.fetchone()
    assert row is None


async def test_child_task_does_not_inherit_parent_transaction_state():
    db = await get_db()

    async def child_write():
        await db.execute(
            "INSERT INTO datasets(path, name) VALUES (?, ?)",
            ("/tmp/child", "child"),
        )
        await db.commit()

    with pytest.raises(RuntimeError):
        async with db.transaction():
            await db.execute(
                "INSERT INTO datasets(path, name) VALUES (?, ?)",
                ("/tmp/parent", "parent"),
            )
            task = asyncio.create_task(child_write())
            await task
            raise RuntimeError("rollback parent")

    fresh = await get_db()
    async with fresh.execute(
        "SELECT path FROM datasets WHERE path IN (?, ?) ORDER BY path",
        ("/tmp/child", "/tmp/parent"),
    ) as cur:
        rows = [row["path"] for row in await cur.fetchall()]
    assert rows == ["/tmp/child"]


async def test_data_modifying_cte_persists_only_after_commit():
    db = await get_db()
    await db.execute(
        "INSERT INTO datasets(path, name) VALUES (?, ?)",
        ("/tmp/cte-source", "cte-source"),
    )
    await db.commit()

    async with db.execute(
        """
        WITH deleted AS (
            DELETE FROM datasets
            WHERE path = ?
            RETURNING path
        )
        SELECT path FROM deleted
        """,
        ("/tmp/cte-source",),
    ) as cur:
        row = await cur.fetchone()
    assert row["path"] == "/tmp/cte-source"

    fresh = await get_db()
    async with fresh.execute("SELECT name FROM datasets WHERE path = ?", ("/tmp/cte-source",)) as cur:
        before_commit = await cur.fetchone()
    assert before_commit is not None

    await db.commit()

    async with fresh.execute("SELECT name FROM datasets WHERE path = ?", ("/tmp/cte-source",)) as cur:
        after_commit = await cur.fetchone()
    assert after_commit is None


# Regression: ConnectionDoesNotExistError raised by pool.release() must not
# poison successful query results. Found by /qa on 2026-04-27 — first
# /api/episodes call after a stale Postgres connection died returned 500
# (21B body) because db._run_without_state's finally block called
# pool.release(conn) without guarding the dead-connection case. asyncpg's
# release internally fires RESET ALL, which fails on a closed conn.
# See docker logs trace: ConnectionDoesNotExistError "connection was closed
# in the middle of operation" inside backend/core/db.py:491 release.

class _FlakyPool:
    """Delegating asyncpg pool wrapper whose first release() raises once.

    Used to simulate the production trace where Postgres has closed the
    connection (idle timeout, network blip) but the pool has not yet
    detected it; pool.release() internally fires RESET ALL, and that RESET
    raises ConnectionDoesNotExistError. The bug under test is that this
    error escapes db._run_without_state's finally block and replaces the
    successful query result with a 500.
    """

    def __init__(self, real):
        self._real = real
        self._fired = False

    async def acquire(self, *args, **kwargs):
        return await self._real.acquire(*args, **kwargs)

    async def release(self, conn, *args, **kwargs):
        if not self._fired:
            self._fired = True
            raise asyncpg.exceptions.ConnectionDoesNotExistError(
                "connection was closed in the middle of operation"
            )
        return await self._real.release(conn, *args, **kwargs)


async def test_execute_select_swallows_dead_connection_release_error(monkeypatch):
    db = await get_db()
    flaky = _FlakyPool(db._pool)
    monkeypatch.setattr(db, "_pool", flaky)

    async with db.execute("SELECT 1 AS one") as cur:
        rows = await cur.fetchall()

    assert flaky._fired, "pool.release was not exercised"
    assert rows[0]["one"] == 1


async def test_executescript_swallows_dead_connection_release_error(monkeypatch):
    db = await get_db()
    flaky = _FlakyPool(db._pool)
    monkeypatch.setattr(db, "_pool", flaky)

    # executescript hits the second unguarded release site at db.py:543
    await db.executescript("SELECT 1")

    assert flaky._fired, "pool.release was not exercised"
