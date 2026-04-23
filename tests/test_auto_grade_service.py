"""Integration tests for `ensure_auto_graded` idempotency + grade preservation.

These cover three paths of `backend.datasets.services.auto_grade_service`:

1. If `datasets.auto_graded_at` is already set, the function exits before any
   work (idempotency).
2. When it short-circuits due to missing features, it does NOT stamp
   `auto_graded_at` — so a later full load can still run the pass (retry path).
3. When it runs successfully (even over an empty set of parquet files), it
   preserves pre-existing user grades AND stamps `auto_graded_at`.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

from backend.core.db import get_db, init_db, close_db, _reset
from backend.datasets.services import auto_grade_service


@pytest_asyncio.fixture(autouse=True)
async def tmp_db(monkeypatch):
    """Point DB to a temp file for each test, matching test_db.py conventions."""
    _reset()
    tmp = Path(tempfile.mkdtemp()) / "test.db"
    monkeypatch.setattr("backend.core.db._db_path_override", str(tmp))
    await init_db()
    yield tmp
    await close_db()
    _reset()
    tmp.unlink(missing_ok=True)


async def _insert_dataset(path: str) -> int:
    """Insert a fresh dataset row (auto_graded_at = NULL) and return its id."""
    db = await get_db()
    await db.execute(
        "INSERT INTO datasets (path, name, auto_graded_at) VALUES (?, ?, NULL)",
        (path, "t"),
    )
    await db.commit()
    async with db.execute("SELECT id FROM datasets WHERE path = ?", (path,)) as cur:
        row = await cur.fetchone()
    return row[0]


class _StubService:
    """Minimal stand-in for the `dataset_service` singleton."""

    def __init__(self, features: dict):
        self._features = features
        self.distribution_cache: dict = {}

    def get_features(self):
        return self._features

    def iter_episode_parquet_files(self):
        return iter([])  # no parquet files → loop body never executes


async def test_ensure_auto_graded_skips_if_stamped(tmp_path, monkeypatch):
    """Already-stamped datasets must short-circuit without any further work."""
    ds_id = await _insert_dataset(str(tmp_path / "ds1"))
    db = await get_db()
    await db.execute(
        "UPDATE datasets SET auto_graded_at = '2026-04-18T00:00:00Z' WHERE id = ?",
        (ds_id,),
    )
    await db.commit()

    # Install a stub that would blow up if `get_features` were reached.
    class _Boom:
        distribution_cache: dict = {}

        def get_features(self):  # pragma: no cover — must not be called
            raise AssertionError("ensure_auto_graded must not inspect features when already stamped")

        def iter_episode_parquet_files(self):  # pragma: no cover
            raise AssertionError("ensure_auto_graded must not iterate parquet files when already stamped")

    # NOTE: `ensure_auto_graded` re-imports `dataset_service` from its home module
    # each call, so we must patch the attribute on that module — not on
    # `auto_grade_service`.
    monkeypatch.setattr(
        "backend.datasets.services.dataset_service.dataset_service",
        _Boom(),
        raising=False,
    )

    await auto_grade_service.ensure_auto_graded(ds_id, tmp_path / "ds1")

    async with db.execute(
        "SELECT auto_graded_at FROM datasets WHERE id = ?", (ds_id,)
    ) as cur:
        row = await cur.fetchone()
    # Stamp untouched (not overwritten by a later `strftime('now')`).
    assert row[0] == "2026-04-18T00:00:00Z"


async def test_ensure_auto_graded_does_not_stamp_when_features_empty(tmp_path, monkeypatch):
    """Empty features ⇒ retry path: no stamp written."""
    ds_id = await _insert_dataset(str(tmp_path / "ds2"))

    monkeypatch.setattr(
        "backend.datasets.services.dataset_service.dataset_service",
        _StubService(features={}),
        raising=False,
    )

    await auto_grade_service.ensure_auto_graded(ds_id, tmp_path / "ds2")

    db = await get_db()
    async with db.execute(
        "SELECT auto_graded_at FROM datasets WHERE id = ?", (ds_id,)
    ) as cur:
        row = await cur.fetchone()
    assert row[0] is None  # retry path preserved


async def test_ensure_auto_graded_preserves_existing_grades(tmp_path, monkeypatch):
    """User grades are NEVER overwritten, and the dataset IS stamped on success."""
    ds_id = await _insert_dataset(str(tmp_path / "ds3"))

    # Pre-existing user grade on episode 0 — seed via serial-keyed tables.
    db = await get_db()
    await db.execute(
        "INSERT INTO episode_serials (dataset_id, episode_index, serial_number) VALUES (?, 0, ?)",
        (ds_id, "S-0"),
    )
    await db.execute(
        "INSERT INTO annotations (serial_number, grade, tags, reason) "
        "VALUES (?, 'good', '[]', 'user chose good')",
        ("S-0",),
    )
    await db.commit()

    monkeypatch.setattr(
        "backend.datasets.services.dataset_service.dataset_service",
        _StubService(features={"observation.state": {"dtype": "float32"}}),
        raising=False,
    )

    await auto_grade_service.ensure_auto_graded(ds_id, tmp_path / "ds3")

    async with db.execute(
        """SELECT a.grade, a.reason
           FROM episode_serials es
           JOIN annotations a ON a.serial_number = es.serial_number
           WHERE es.dataset_id = ? AND es.episode_index = 0""",
        (ds_id,),
    ) as cur:
        row = await cur.fetchone()
    assert row["grade"] == "good"
    assert row["reason"] == "user chose good"

    async with db.execute(
        "SELECT auto_graded_at FROM datasets WHERE id = ?", (ds_id,)
    ) as cur:
        row = await cur.fetchone()
    assert row[0] is not None
