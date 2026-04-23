"""Tests for core/db.py — schema creation, connection lifecycle, version migration."""

import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

from backend.core.db import get_db, init_db, close_db, _reset


@pytest_asyncio.fixture(autouse=True)
async def tmp_db(monkeypatch):
    """Point DB to a temp file for each test."""
    _reset()
    tmp = Path(tempfile.mkdtemp()) / "test.db"
    monkeypatch.setattr("backend.core.db._db_path_override", str(tmp))
    yield tmp
    await close_db()
    _reset()
    tmp.unlink(missing_ok=True)


class TestInitDb:
    @pytest.mark.asyncio
    async def test_creates_tables(self, tmp_db):
        await init_db()
        db = await get_db()
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ) as cursor:
            tables = await cursor.fetchall()
        names = {t[0] for t in tables}
        assert "datasets" in names
        assert "episode_serials" in names
        assert "annotations" in names
        assert "dataset_stats" in names
        # The old v3 table must be gone.
        assert "episode_annotations" not in names

    @pytest.mark.asyncio
    async def test_sets_user_version(self, tmp_db):
        await init_db()
        db = await get_db()
        async with db.execute("PRAGMA user_version") as cursor:
            row = await cursor.fetchone()
        assert row[0] == 4

    @pytest.mark.asyncio
    async def test_idempotent(self, tmp_db):
        await init_db()
        await init_db()
        db = await get_db()
        async with db.execute("PRAGMA user_version") as cursor:
            row = await cursor.fetchone()
        assert row[0] == 4


class TestGetDb:
    @pytest.mark.asyncio
    async def test_returns_same_connection(self, tmp_db):
        await init_db()
        db1 = await get_db()
        db2 = await get_db()
        assert db1 is db2

    @pytest.mark.asyncio
    async def test_close_and_reopen(self, tmp_db):
        await init_db()
        db1 = await get_db()
        await close_db()
        _reset()
        # Re-set the override since _reset clears it
        import backend.core.db
        backend.core.db._db_path_override = str(tmp_db)
        await init_db()
        db2 = await get_db()
        assert db1 is not db2


class TestSchema:
    @pytest.mark.asyncio
    async def test_datasets_table_columns(self, tmp_db):
        await init_db()
        db = await get_db()
        async with db.execute("PRAGMA table_info(datasets)") as cursor:
            rows = await cursor.fetchall()
        col_names = [r[1] for r in rows]
        for col in [
            "id", "path", "name", "cell_name", "fps", "total_episodes",
            "robot_type", "features", "synced_at", "auto_graded_at",
            "info_json_mtime",
        ]:
            assert col in col_names


class TestSchemaV4:
    @pytest.mark.asyncio
    async def test_user_version_is_4(self, tmp_db):
        await init_db()
        db = await get_db()
        async with db.execute("PRAGMA user_version") as cursor:
            row = await cursor.fetchone()
        assert row[0] == 4

    @pytest.mark.asyncio
    async def test_episode_annotations_dropped(self, tmp_db):
        await init_db()
        db = await get_db()
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='episode_annotations'"
        ) as cursor:
            row = await cursor.fetchone()
        assert row is None

    @pytest.mark.asyncio
    async def test_episode_serials_table(self, tmp_db):
        await init_db()
        db = await get_db()
        async with db.execute("PRAGMA table_info(episode_serials)") as cursor:
            rows = await cursor.fetchall()
        cols = {r[1] for r in rows}
        assert cols == {"dataset_id", "episode_index", "serial_number"}

    @pytest.mark.asyncio
    async def test_episode_serials_cascade(self, tmp_db):
        await init_db()
        db = await get_db()
        await db.execute("INSERT INTO datasets (path, name) VALUES ('/tmp/x', 'x')")
        await db.execute(
            "INSERT INTO episode_serials (dataset_id, episode_index, serial_number) VALUES (1, 0, 'S1')"
        )
        await db.commit()
        await db.execute("DELETE FROM datasets WHERE id = 1")
        await db.commit()
        async with db.execute("SELECT COUNT(*) FROM episode_serials") as cursor:
            n = (await cursor.fetchone())[0]
        assert n == 0

    @pytest.mark.asyncio
    async def test_annotations_not_cascaded(self, tmp_db):
        """annotations survive dataset deletion — the whole point of serial-keying."""
        await init_db()
        db = await get_db()
        await db.execute("INSERT INTO datasets (path, name) VALUES ('/tmp/x', 'x')")
        await db.execute(
            "INSERT INTO episode_serials (dataset_id, episode_index, serial_number) VALUES (1, 0, 'S1')"
        )
        await db.execute(
            "INSERT INTO annotations (serial_number, grade) VALUES ('S1', 'good')"
        )
        await db.commit()
        await db.execute("DELETE FROM datasets WHERE id = 1")
        await db.commit()
        async with db.execute("SELECT COUNT(*) FROM annotations") as cursor:
            n = (await cursor.fetchone())[0]
        assert n == 1

    @pytest.mark.asyncio
    async def test_annotations_grade_check(self, tmp_db):
        await init_db()
        db = await get_db()
        with pytest.raises(Exception):
            await db.execute(
                "INSERT INTO annotations (serial_number, grade) VALUES ('S1', 'bogus')"
            )
            await db.commit()

    @pytest.mark.asyncio
    async def test_datasets_has_info_json_mtime(self, tmp_db):
        await init_db()
        db = await get_db()
        async with db.execute("PRAGMA table_info(datasets)") as cursor:
            rows = await cursor.fetchall()
        cols = {r[1] for r in rows}
        assert "info_json_mtime" in cols


class TestSchemaV4Guard:
    @pytest.mark.asyncio
    async def test_rejects_upgrade_when_annotations_present(self, tmp_db):
        """Simulate a v3 DB with user data. v4 upgrade must abort."""
        import backend.core.db as dbmod
        db = await get_db()
        await db.executescript(dbmod.SCHEMA_V1)
        await db.executescript(dbmod.SCHEMA_V2)
        await db.executescript(dbmod.SCHEMA_V3)
        await db.execute("PRAGMA user_version = 3")
        await db.execute("INSERT INTO datasets (path, name) VALUES ('/tmp/x', 'x')")
        await db.execute(
            "INSERT INTO episode_annotations (dataset_id, episode_index, grade) VALUES (1, 0, 'good')"
        )
        await db.commit()

        with pytest.raises(RuntimeError, match="Schema v4 drops episode_annotations"):
            await init_db()

    @pytest.mark.asyncio
    async def test_allows_upgrade_when_empty(self, tmp_db):
        import backend.core.db as dbmod
        db = await get_db()
        await db.executescript(dbmod.SCHEMA_V1)
        await db.executescript(dbmod.SCHEMA_V2)
        await db.executescript(dbmod.SCHEMA_V3)
        await db.execute("PRAGMA user_version = 3")
        await db.commit()

        await init_db()  # should not raise
        async with db.execute("PRAGMA user_version") as cur:
            assert (await cur.fetchone())[0] == 4
