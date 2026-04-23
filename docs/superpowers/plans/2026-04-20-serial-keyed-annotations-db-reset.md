# Serial-keyed annotations + DB reset — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reset the metadata DB and rekey user-entered episode grades on the recording's `Serial_number` so re-converting a dataset no longer reattaches old grades to new episodes.

**Architecture:** SQLite schema v4 introduces `episode_serials(dataset_id, episode_index, serial_number)` as a parquet-backed cache and `annotations(serial_number, grade, tags, reason)` as the source of truth. Cell browsing rebuilds `episode_serials` only when `meta/info.json` mtime changes. All annotation CRUD goes through `(dataset_id, episode_index) → serial_number → annotations`.

**Tech Stack:** Python 3.13, FastAPI, aiosqlite, pyarrow, pytest-asyncio.

Spec: `docs/superpowers/specs/2026-04-20-serial-keyed-annotations-db-reset-design.md`.

---

## File Structure

**Create:**
- `scripts/reset_db.py` — backup-and-init helper
- `scripts/__init__.py` — package marker (if missing)
- `tests/test_reset_db_script.py`
- `tests/test_episode_serials_sync.py`
- `tests/test_lazy_sync_mtime.py`
- `tests/test_reconversion_scenario.py`
- `tests/test_sidecar_migration_v4.py`

**Modify:**
- `backend/core/db.py` — SCHEMA_V4, migration branch, safety guard
- `backend/datasets/services/cell_service.py` — stale path cleanup, mtime gate, `_rebuild_episode_serials`
- `backend/datasets/services/episode_service.py` — `_get_serial` helper, JOIN-based reads, serial-resolved writes, `_ensure_migrated` redesign
- `backend/datasets/services/auto_grade_service.py` — two query updates with serial resolution
- `backend/datasets/services/dataset_service.py` — serial-rebuild hook at load time (scope in Task 11)
- `tests/test_db.py` — v4 assertions, guard test
- `tests/test_episode_annotations_db.py` — rewrite against new schema
- `tests/test_auto_grade_service.py` — fixtures on episode_serials + annotations
- `tests/test_grade_reason.py` — reason column lives in `annotations`
- `tests/test_mockup.py` — verify sidecar still loads end-to-end
- `tests/create_mock_dataset.py` (or equivalent fixture helper) — add `Serial_number` column

---

## Task 1: Schema v4 with serial-keyed tables + safety guard

**Files:**
- Modify: `backend/core/db.py`
- Modify: `tests/test_db.py`

- [ ] **Step 1: Write failing tests for v4 schema**

Append to `tests/test_db.py` (before the trailing blank line):

```python
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
```

Also update the existing `test_sets_user_version` and `test_idempotent` to expect `4`, and delete `test_episode_annotations_table_columns`, `test_grade_check_constraint`, `test_cascade_delete` (they cover the dropped table; the new tests above replace them).

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_db.py -v`
Expected: new `TestSchemaV4` tests fail (user_version is 3, episode_serials/annotations absent).

- [ ] **Step 3: Add SCHEMA_V4 and migration branch**

Modify `backend/core/db.py`. After `SCHEMA_V3`:

```python
SCHEMA_V4 = """
ALTER TABLE datasets ADD COLUMN info_json_mtime REAL;

DROP TABLE IF EXISTS episode_annotations;

CREATE TABLE episode_serials (
    dataset_id      INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    episode_index   INTEGER NOT NULL,
    serial_number   TEXT NOT NULL,
    PRIMARY KEY (dataset_id, episode_index)
);
CREATE INDEX idx_episode_serials_serial ON episode_serials(serial_number);

CREATE TABLE annotations (
    serial_number   TEXT PRIMARY KEY,
    grade           TEXT CHECK(grade IN ('good','normal','bad')),
    tags            TEXT DEFAULT '[]',
    reason          TEXT,
    updated_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
"""
```

Inside `init_db()`, after the v3 block, add:

```python
    if version < 4:
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='episode_annotations'"
        ) as cursor:
            table_exists = await cursor.fetchone() is not None
        if table_exists:
            async with db.execute("SELECT COUNT(*) FROM episode_annotations") as cursor:
                leftover = (await cursor.fetchone())[0]
            if leftover > 0:
                raise RuntimeError(
                    f"Schema v4 drops episode_annotations but found {leftover} rows. "
                    "Run `python -m scripts.reset_db` first (it backs up and wipes the DB). "
                    "Existing grades are not automatically preserved; the intended flow "
                    "is annotate-fresh after reset."
                )
        await db.executescript(SCHEMA_V4)
        await db.execute("PRAGMA user_version = 4")
        await db.commit()
        logger.info("Database upgraded to v4 (serial-keyed annotations) at %s", _get_db_path())
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_db.py -v`
Expected: all tests pass, including the new `TestSchemaV4` class.

- [ ] **Step 5: Write failing test for safety guard**

Append to `tests/test_db.py`:

```python
class TestSchemaV4Guard:
    @pytest.mark.asyncio
    async def test_rejects_upgrade_when_annotations_present(self, tmp_db, monkeypatch):
        """Simulate a v3 DB with user data. v4 upgrade must abort."""
        # Bring the DB up to v3 only
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
```

- [ ] **Step 6: Run guard tests to verify pass**

Run: `pytest tests/test_db.py::TestSchemaV4Guard -v`
Expected: both tests pass (the guard is already implemented in Step 3).

- [ ] **Step 7: Commit**

```bash
git add backend/core/db.py tests/test_db.py
git commit -m "feat(db): add schema v4 with serial-keyed annotations"
```

---

## Task 2: `scripts/reset_db.py` backup-and-init helper

**Files:**
- Create: `scripts/__init__.py` (if missing)
- Create: `scripts/reset_db.py`
- Create: `tests/test_reset_db_script.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_reset_db_script.py`:

```python
"""Tests for scripts/reset_db.py — backup and init flow."""

import asyncio
import tempfile
from pathlib import Path

import pytest


def test_dry_run_does_not_touch_files(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "metadata.db"
    db_path.write_bytes(b"dummy")
    monkeypatch.setattr("backend.core.config.settings.db_path", str(db_path))

    from scripts.reset_db import run
    run(dry_run=True, assume_yes=True)

    assert db_path.exists()
    assert db_path.read_bytes() == b"dummy"
    out = capsys.readouterr().out
    assert str(db_path) in out


def test_backup_and_init_creates_empty_v4_db(tmp_path, monkeypatch):
    db_path = tmp_path / "metadata.db"
    db_path.write_bytes(b"old")
    (tmp_path / "metadata.db-wal").write_bytes(b"wal")
    monkeypatch.setattr("backend.core.config.settings.db_path", str(db_path))
    # Reset the db singleton so the override takes effect
    from backend.core import db as dbmod
    dbmod._reset()
    dbmod._db_path_override = str(db_path)

    from scripts.reset_db import run
    run(dry_run=False, assume_yes=True)

    # Originals gone
    assert not db_path.exists() or db_path.stat().st_size != 3  # old bytes absent
    # At least one backup created
    backups = sorted(tmp_path.glob("metadata.db.bak-*"))
    assert backups, f"no backup created in {list(tmp_path.iterdir())}"
    assert backups[0].read_bytes() == b"old"
    wal_backups = sorted(tmp_path.glob("metadata.db-wal.bak-*"))
    assert wal_backups
    assert wal_backups[0].read_bytes() == b"wal"

    # New DB is v4
    async def _check():
        conn = await dbmod.get_db()
        async with conn.execute("PRAGMA user_version") as cur:
            return (await cur.fetchone())[0]

    version = asyncio.run(_check())
    assert version == 4

    asyncio.run(dbmod.close_db())
    dbmod._reset()


def test_backup_name_collision_suffix(tmp_path, monkeypatch):
    """Two resets in the same second must not clobber the first backup."""
    db_path = tmp_path / "metadata.db"
    db_path.write_bytes(b"one")
    monkeypatch.setattr("backend.core.config.settings.db_path", str(db_path))
    from backend.core import db as dbmod
    dbmod._reset()
    dbmod._db_path_override = str(db_path)

    from scripts.reset_db import run
    run(dry_run=False, assume_yes=True)
    # Write fresh content and reset again at the same ISO second
    db_path.write_bytes(b"two")

    import scripts.reset_db as resetmod
    # Force a stable timestamp by monkeypatching
    monkeypatch.setattr(resetmod, "_utc_timestamp", lambda: "20260420T000000Z")
    # First run will produce metadata.db.bak-20260420T000000Z
    # Second run must produce .bak-20260420T000000Z.1
    # Clean prior real-timestamped backups for a deterministic assertion
    for b in tmp_path.glob("metadata.db.bak-*"):
        b.unlink()
    db_path.write_bytes(b"alpha")
    resetmod.run(dry_run=False, assume_yes=True)
    db_path.write_bytes(b"beta")
    asyncio.run(dbmod.close_db())
    dbmod._reset()
    dbmod._db_path_override = str(db_path)
    resetmod.run(dry_run=False, assume_yes=True)

    names = sorted(p.name for p in tmp_path.glob("metadata.db.bak-*"))
    assert "metadata.db.bak-20260420T000000Z" in names
    assert any(n.endswith(".1") for n in names), names

    asyncio.run(dbmod.close_db())
    dbmod._reset()
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_reset_db_script.py -v`
Expected: FAIL (module `scripts.reset_db` not found).

- [ ] **Step 3: Create the script**

Create `scripts/__init__.py` as an empty file if it doesn't exist.

Create `scripts/reset_db.py`:

```python
"""Backup and reinitialize the curation-tools metadata DB.

Usage:
    python -m scripts.reset_db --dry-run          # preview
    python -m scripts.reset_db                    # interactive
    python -m scripts.reset_db --yes              # non-interactive
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import shutil
import sys
from pathlib import Path


def _utc_timestamp() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _unique_backup(target: Path) -> Path:
    candidate = target
    n = 1
    while candidate.exists():
        candidate = target.with_name(f"{target.name}.{n}")
        n += 1
    return candidate


def _resolve_db_path() -> Path:
    from backend.core.config import settings
    if settings.db_path:
        return Path(settings.db_path)
    return Path.home() / ".local" / "share" / "curation-tools" / "metadata.db"


def run(*, dry_run: bool, assume_yes: bool) -> None:
    db_path = _resolve_db_path()
    wal = db_path.with_name(db_path.name + "-wal")
    shm = db_path.with_name(db_path.name + "-shm")
    ts = _utc_timestamp()

    targets: list[Path] = [p for p in (db_path, wal, shm) if p.exists()]
    backups: list[tuple[Path, Path]] = []
    for src in targets:
        desired = src.with_name(f"{src.name}.bak-{ts}")
        backups.append((src, _unique_backup(desired)))

    print(f"[reset_db] DB path: {db_path}")
    if not targets:
        print("[reset_db] no existing DB files; will create fresh v4 DB")
    else:
        for src, dst in backups:
            print(f"[reset_db] backup  {src}  ->  {dst}")

    if dry_run:
        print("[reset_db] dry-run; no files modified")
        return

    if not assume_yes:
        resp = input("Proceed? [y/N] ").strip().lower()
        if resp != "y":
            print("[reset_db] aborted")
            sys.exit(1)

    for src, dst in backups:
        shutil.copy2(src, dst)
        src.unlink()

    # Reset the singleton in case the test harness (or a prior call) left one
    from backend.core import db as dbmod
    asyncio.run(dbmod.close_db())
    dbmod._reset()
    dbmod._db_path_override = str(db_path)

    asyncio.run(dbmod.init_db())
    print(f"[reset_db] fresh DB initialized at {db_path} (schema v4)")
    for _src, dst in backups:
        print(f"[reset_db] backup retained: {dst}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset curation-tools metadata DB.")
    parser.add_argument("--dry-run", action="store_true", help="show actions without modifying files")
    parser.add_argument("--yes", action="store_true", help="skip confirmation prompt")
    args = parser.parse_args()
    run(dry_run=args.dry_run, assume_yes=args.yes)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_reset_db_script.py -v`
Expected: all 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/__init__.py scripts/reset_db.py tests/test_reset_db_script.py
git commit -m "feat(scripts): add reset_db backup-and-init script"
```

---

## Task 3: Mock-dataset fixture gains `Serial_number` column

**Files:**
- Modify: `tests/test_episode_annotations_db.py` (update `_create_mock_dataset`)
- Create helper shared by later tests: keep the helper local, but ensure Serial_number is always present.

- [ ] **Step 1: Write a failing test that depends on Serial_number**

Create `tests/test_episode_serials_sync.py` with a smoke check that uses a helper we'll add in the next task. For this task, only add a single test that asserts the fixture helper produces the column, then we'll consume it in Task 4.

```python
"""Verify the shared mock-dataset helper now includes Serial_number."""

from pathlib import Path

import pyarrow.parquet as pq


def test_mock_dataset_has_serial_number(tmp_path: Path):
    from tests.test_episode_annotations_db import _create_mock_dataset
    ds = _create_mock_dataset(tmp_path)
    pf = ds / "meta" / "episodes" / "chunk-000" / "file-000.parquet"
    schema = pq.read_schema(pf)
    assert "Serial_number" in schema.names
    t = pq.read_table(pf, columns=["episode_index", "Serial_number"])
    serials = t.column("Serial_number").to_pylist()
    assert all(s and s.startswith("MOCK_") for s in serials)
    assert len(set(serials)) == len(serials), "serials must be unique per episode"
```

- [ ] **Step 2: Run test to verify failure**

Run: `pytest tests/test_episode_serials_sync.py::test_mock_dataset_has_serial_number -v`
Expected: FAIL (column absent).

- [ ] **Step 3: Update the fixture helper**

Modify `tests/test_episode_annotations_db.py` — replace the `ep_table` block in `_create_mock_dataset` to include `Serial_number`:

```python
    ep_table = pa.table({
        "episode_index": pa.array([0, 1, 2], type=pa.int64()),
        "task_index": pa.array([0, 0, 0], type=pa.int64()),
        "data/chunk_index": pa.array([0, 0, 0], type=pa.int64()),
        "data/file_index": pa.array([0, 0, 0], type=pa.int64()),
        "dataset_from_index": pa.array([0, 100, 200], type=pa.int64()),
        "dataset_to_index": pa.array([100, 200, 300], type=pa.int64()),
        "Serial_number": pa.array(
            ["MOCK_20260101_000000_000000", "MOCK_20260101_000001_000000", "MOCK_20260101_000002_000000"],
            type=pa.string(),
        ),
    })
```

- [ ] **Step 4: Run test to verify pass**

Run: `pytest tests/test_episode_serials_sync.py::test_mock_dataset_has_serial_number -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_episode_annotations_db.py tests/test_episode_serials_sync.py
git commit -m "test: add Serial_number to mock dataset fixture"
```

---

## Task 4: `_rebuild_episode_serials` helper

**Files:**
- Modify: `backend/datasets/services/cell_service.py`
- Modify: `tests/test_episode_serials_sync.py`

- [ ] **Step 1: Add failing tests for rebuild behavior**

Append to `tests/test_episode_serials_sync.py`:

```python
import asyncio
import json
import tempfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import pytest_asyncio

from backend.core.db import get_db, init_db, close_db, _reset


@pytest_asyncio.fixture
async def tmp_db(monkeypatch):
    _reset()
    tmp = Path(tempfile.mkdtemp()) / "test.db"
    monkeypatch.setattr("backend.core.db._db_path_override", str(tmp))
    await init_db()
    yield tmp
    await close_db()
    _reset()
    tmp.unlink(missing_ok=True)


async def _insert_dataset(db, path: str, name: str = "ds") -> int:
    await db.execute("INSERT INTO datasets (path, name) VALUES (?, ?)", (path, name))
    await db.commit()
    async with db.execute("SELECT id FROM datasets WHERE path = ?", (path,)) as cur:
        return (await cur.fetchone())[0]


def _write_episodes_parquet(
    dataset_dir: Path, rows: list[tuple[int, str]], chunk: int = 0, file: int = 0
) -> Path:
    out = dataset_dir / "meta" / "episodes" / f"chunk-{chunk:03d}"
    out.mkdir(parents=True, exist_ok=True)
    pf = out / f"file-{file:03d}.parquet"
    t = pa.table({
        "episode_index": pa.array([r[0] for r in rows], type=pa.int64()),
        "Serial_number": pa.array([r[1] for r in rows], type=pa.string()),
    })
    pq.write_table(t, pf)
    return pf


class TestRebuildEpisodeSerials:
    @pytest.mark.asyncio
    async def test_populates_from_parquet(self, tmp_db, tmp_path):
        from backend.datasets.services.cell_service import _rebuild_episode_serials
        db = await get_db()
        dataset_dir = tmp_path / "ds_a"
        dataset_dir.mkdir()
        _write_episodes_parquet(dataset_dir, [(0, "S-A"), (1, "S-B"), (2, "S-C")])
        dataset_id = await _insert_dataset(db, str(dataset_dir.resolve()))

        await _rebuild_episode_serials(db, dataset_id, dataset_dir)
        await db.commit()

        async with db.execute(
            "SELECT episode_index, serial_number FROM episode_serials WHERE dataset_id = ? ORDER BY episode_index",
            (dataset_id,),
        ) as cur:
            rows = await cur.fetchall()
        assert [tuple(r) for r in rows] == [(0, "S-A"), (1, "S-B"), (2, "S-C")]

    @pytest.mark.asyncio
    async def test_drops_stale_rows(self, tmp_db, tmp_path):
        from backend.datasets.services.cell_service import _rebuild_episode_serials
        db = await get_db()
        dataset_dir = tmp_path / "ds_b"
        dataset_dir.mkdir()
        _write_episodes_parquet(dataset_dir, [(0, "S-A"), (1, "S-B"), (2, "S-C")])
        dataset_id = await _insert_dataset(db, str(dataset_dir.resolve()))
        await _rebuild_episode_serials(db, dataset_id, dataset_dir)
        await db.commit()

        # Simulate re-conversion with fewer episodes
        _write_episodes_parquet(dataset_dir, [(0, "S-A"), (1, "S-B")])
        await _rebuild_episode_serials(db, dataset_id, dataset_dir)
        await db.commit()

        async with db.execute(
            "SELECT episode_index FROM episode_serials WHERE dataset_id = ? ORDER BY episode_index",
            (dataset_id,),
        ) as cur:
            rows = [r[0] for r in await cur.fetchall()]
        assert rows == [0, 1]

    @pytest.mark.asyncio
    async def test_skips_missing_serial_column(self, tmp_db, tmp_path, caplog):
        from backend.datasets.services.cell_service import _rebuild_episode_serials
        db = await get_db()
        dataset_dir = tmp_path / "ds_c"
        (dataset_dir / "meta" / "episodes" / "chunk-000").mkdir(parents=True)
        t = pa.table({"episode_index": pa.array([0, 1], type=pa.int64())})
        pq.write_table(t, dataset_dir / "meta" / "episodes" / "chunk-000" / "file-000.parquet")
        dataset_id = await _insert_dataset(db, str(dataset_dir.resolve()))

        await _rebuild_episode_serials(db, dataset_id, dataset_dir)
        await db.commit()

        async with db.execute(
            "SELECT COUNT(*) FROM episode_serials WHERE dataset_id = ?", (dataset_id,)
        ) as cur:
            assert (await cur.fetchone())[0] == 0

    @pytest.mark.asyncio
    async def test_skips_empty_or_none_serial(self, tmp_db, tmp_path):
        from backend.datasets.services.cell_service import _rebuild_episode_serials
        db = await get_db()
        dataset_dir = tmp_path / "ds_d"
        dataset_dir.mkdir()
        _write_episodes_parquet(dataset_dir, [(0, "S-A"), (1, ""), (2, "S-C")])
        dataset_id = await _insert_dataset(db, str(dataset_dir.resolve()))

        await _rebuild_episode_serials(db, dataset_id, dataset_dir)
        await db.commit()

        async with db.execute(
            "SELECT episode_index FROM episode_serials WHERE dataset_id = ? ORDER BY episode_index",
            (dataset_id,),
        ) as cur:
            rows = [r[0] for r in await cur.fetchall()]
        assert rows == [0, 2]
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_episode_serials_sync.py::TestRebuildEpisodeSerials -v`
Expected: FAIL — `_rebuild_episode_serials` not defined.

- [ ] **Step 3: Implement `_rebuild_episode_serials`**

Append to `backend/datasets/services/cell_service.py` (after `_upsert_datasets_to_db`):

```python
async def _rebuild_episode_serials(db, dataset_id: int, dataset_dir: Path) -> None:
    """Replace all rows in episode_serials for dataset_id using current parquet.

    Only reads the `episode_index` and `Serial_number` columns. Episodes with
    missing or empty Serial_number are skipped with a warning; the Serial_number
    column being absent from a parquet file is also a warning (the whole file
    is skipped).
    """
    from glob import glob

    import pyarrow.parquet as pq

    pattern = str(dataset_dir / "meta" / "episodes" / "chunk-*" / "file-*.parquet")
    collected: list[tuple[int, int, str]] = []
    for parquet_path in sorted(glob(pattern)):
        schema = pq.read_schema(parquet_path)
        if "Serial_number" not in schema.names:
            logger.warning("parquet %s missing Serial_number; skipping", parquet_path)
            continue
        table = pq.read_table(parquet_path, columns=["episode_index", "Serial_number"])
        indices = table.column("episode_index").to_pylist()
        serials = table.column("Serial_number").to_pylist()
        for idx, serial in zip(indices, serials):
            if serial is None or serial == "":
                logger.warning(
                    "episode %s in %s has empty Serial_number; skipping",
                    idx, dataset_dir,
                )
                continue
            collected.append((dataset_id, int(idx), str(serial)))

    await db.execute("DELETE FROM episode_serials WHERE dataset_id = ?", (dataset_id,))
    if collected:
        await db.executemany(
            "INSERT INTO episode_serials (dataset_id, episode_index, serial_number) "
            "VALUES (?, ?, ?)",
            collected,
        )
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_episode_serials_sync.py -v`
Expected: all 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add backend/datasets/services/cell_service.py tests/test_episode_serials_sync.py
git commit -m "feat(cell_service): add _rebuild_episode_serials helper"
```

---

## Task 5: Stale-path cleanup + mtime-gated sync in `_upsert_datasets_to_db`

**Files:**
- Modify: `backend/datasets/services/cell_service.py`
- Create: `tests/test_lazy_sync_mtime.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_lazy_sync_mtime.py`:

```python
"""Verify lazy sync: parquet rescans only when info.json mtime changes, and
stale dataset rows are cleared from the cell after disk removal.
"""

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import pytest_asyncio

from backend.core.db import get_db, init_db, close_db, _reset


@pytest_asyncio.fixture
async def tmp_db(monkeypatch):
    _reset()
    tmp = Path(tempfile.mkdtemp()) / "test.db"
    monkeypatch.setattr("backend.core.db._db_path_override", str(tmp))
    await init_db()
    yield tmp
    await close_db()
    _reset()
    tmp.unlink(missing_ok=True)


def _make_dataset(parent: Path, name: str, serials: list[str]) -> Path:
    d = parent / name
    (d / "meta" / "episodes" / "chunk-000").mkdir(parents=True)
    (d / "meta" / "info.json").write_text(json.dumps({
        "fps": 30, "total_episodes": len(serials), "total_tasks": 1,
        "robot_type": "test", "features": {},
    }))
    t = pa.table({
        "episode_index": pa.array(list(range(len(serials))), type=pa.int64()),
        "Serial_number": pa.array(serials, type=pa.string()),
    })
    pq.write_table(t, d / "meta" / "episodes" / "chunk-000" / "file-000.parquet")
    return d


async def _browse(cell_path: Path):
    from backend.datasets.services.cell_service import get_datasets_in_cell
    return await get_datasets_in_cell(str(cell_path))


class TestStalePathCleanup:
    @pytest.mark.asyncio
    async def test_removes_vanished_datasets(self, tmp_db, tmp_path):
        cell = tmp_path / "cell000"
        cell.mkdir()
        ds_a = _make_dataset(cell, "a", ["S-A1", "S-A2"])
        ds_b = _make_dataset(cell, "b", ["S-B1"])

        await _browse(cell)

        db = await get_db()
        async with db.execute("SELECT path FROM datasets ORDER BY path") as cur:
            assert {r[0] for r in await cur.fetchall()} == {
                str(ds_a.resolve()), str(ds_b.resolve()),
            }

        # Delete ds_b from disk, re-browse
        import shutil
        shutil.rmtree(ds_b)
        await _browse(cell)

        async with db.execute("SELECT path FROM datasets") as cur:
            assert {r[0] for r in await cur.fetchall()} == {str(ds_a.resolve())}


class TestLazyMtime:
    @pytest.mark.asyncio
    async def test_skips_rebuild_when_mtime_unchanged(self, tmp_db, tmp_path):
        cell = tmp_path / "cell001"
        cell.mkdir()
        _make_dataset(cell, "a", ["S-A1", "S-A2"])

        await _browse(cell)  # first browse populates

        from backend.datasets.services import cell_service
        with patch.object(cell_service, "_rebuild_episode_serials") as mock_rebuild:
            await _browse(cell)
            mock_rebuild.assert_not_called()

    @pytest.mark.asyncio
    async def test_rebuilds_when_info_json_changes(self, tmp_db, tmp_path):
        cell = tmp_path / "cell002"
        cell.mkdir()
        ds = _make_dataset(cell, "a", ["S-A1", "S-A2"])

        await _browse(cell)

        # Overwrite info.json with a new mtime
        time.sleep(0.05)
        info = json.loads((ds / "meta" / "info.json").read_text())
        info["total_episodes"] = 3
        (ds / "meta" / "info.json").write_text(json.dumps(info))
        # Ensure the mtime actually advanced
        new_mtime = (ds / "meta" / "info.json").stat().st_mtime

        from backend.datasets.services import cell_service
        with patch.object(
            cell_service, "_rebuild_episode_serials", wraps=cell_service._rebuild_episode_serials
        ) as spy:
            await _browse(cell)
            assert spy.call_count == 1
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_lazy_sync_mtime.py -v`
Expected: FAIL — stale rows persist, mtime gate absent.

- [ ] **Step 3: Rewrite `_upsert_datasets_to_db`**

Replace the body of `_upsert_datasets_to_db` in `backend/datasets/services/cell_service.py` with:

```python
async def _upsert_datasets_to_db(cell_name: str, datasets: list[DatasetSummary]) -> None:
    """Upsert dataset summaries, prune vanished datasets, and rebuild
    episode_serials lazily (only when meta/info.json mtime changes).
    """
    from backend.core.db import get_db

    db = await get_db()
    live_paths = sorted({ds.path for ds in datasets})

    # (a) Remove datasets in this cell that no longer exist on disk.
    if live_paths:
        placeholders = ",".join("?" * len(live_paths))
        await db.execute(
            f"DELETE FROM datasets WHERE cell_name = ? AND path NOT IN ({placeholders})",
            (cell_name, *live_paths),
        )
    else:
        await db.execute("DELETE FROM datasets WHERE cell_name = ?", (cell_name,))

    for ds in datasets:
        info_json = Path(ds.path) / "meta" / "info.json"
        try:
            info_mtime = info_json.stat().st_mtime
        except OSError:
            logger.warning("cannot stat %s; skipping dataset", info_json)
            continue

        async with db.execute(
            "SELECT id, info_json_mtime FROM datasets WHERE path = ?", (ds.path,)
        ) as cursor:
            row = await cursor.fetchone()
        cached_mtime = row[1] if row else None

        await db.execute(
            """
            INSERT INTO datasets (
                path, name, cell_name, fps, total_episodes, robot_type,
                info_json_mtime, synced_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            ON CONFLICT(path) DO UPDATE SET
              name=excluded.name, cell_name=excluded.cell_name,
              fps=excluded.fps, total_episodes=excluded.total_episodes,
              robot_type=excluded.robot_type,
              info_json_mtime=excluded.info_json_mtime,
              synced_at=excluded.synced_at
            """,
            (ds.path, ds.name, cell_name, ds.fps, ds.total_episodes,
             ds.robot_type, info_mtime),
        )

        async with db.execute("SELECT id FROM datasets WHERE path = ?", (ds.path,)) as cursor:
            dataset_id = (await cursor.fetchone())[0]

        if cached_mtime is None or cached_mtime != info_mtime:
            await _rebuild_episode_serials(db, dataset_id, Path(ds.path))

        await db.execute(
            """
            INSERT INTO dataset_stats (
                dataset_id, graded_count, good_count, normal_count, bad_count,
                total_duration_sec, good_duration_sec, normal_duration_sec, bad_duration_sec,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            ON CONFLICT(dataset_id) DO UPDATE SET
              graded_count=excluded.graded_count, good_count=excluded.good_count,
              normal_count=excluded.normal_count, bad_count=excluded.bad_count,
              total_duration_sec=excluded.total_duration_sec,
              good_duration_sec=excluded.good_duration_sec,
              normal_duration_sec=excluded.normal_duration_sec,
              bad_duration_sec=excluded.bad_duration_sec,
              updated_at=excluded.updated_at
            """,
            (
                dataset_id, ds.graded_count, ds.good_count, ds.normal_count, ds.bad_count,
                ds.total_duration_sec, ds.good_duration_sec, ds.normal_duration_sec,
                ds.bad_duration_sec,
            ),
        )
    await db.commit()
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_lazy_sync_mtime.py tests/test_episode_serials_sync.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add backend/datasets/services/cell_service.py tests/test_lazy_sync_mtime.py
git commit -m "refactor(cell_service): populate episode_serials via lazy mtime-based sync"
```

---

## Task 6: `_get_serial` helper + JOIN-based read

**Files:**
- Modify: `backend/datasets/services/episode_service.py`
- Modify: `tests/test_episode_annotations_db.py`

- [ ] **Step 1: Update existing tests for new schema**

In `tests/test_episode_annotations_db.py`:

Replace every `episode_annotations` reference in SELECTs/inserts with the JOIN pattern. Concretely:

Inside `test_writes_grade_and_tags_to_db` (around line 111), replace:

```python
        async with db.execute(
            "SELECT grade, tags FROM episode_annotations WHERE episode_index = 0"
        ) as cursor:
```

with:

```python
        async with db.execute(
            """SELECT a.grade, a.tags
               FROM annotations a
               JOIN episode_serials es ON es.serial_number = a.serial_number
               WHERE es.episode_index = 0"""
        ) as cursor:
```

Inside `test_writes_to_db` (around line 160), replace:

```python
        async with db.execute(
            "SELECT episode_index, grade, tags FROM episode_annotations ORDER BY episode_index"
        ) as cursor:
```

with:

```python
        async with db.execute(
            """SELECT es.episode_index, a.grade, a.tags
               FROM annotations a
               JOIN episode_serials es ON es.serial_number = a.serial_number
               ORDER BY es.episode_index"""
        ) as cursor:
```

Inside `test_migrates_sidecar_to_db` (around line 225), replace:

```python
        async with db.execute("SELECT COUNT(*) FROM episode_annotations") as cursor:
```

with:

```python
        async with db.execute("SELECT COUNT(*) FROM annotations") as cursor:
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_episode_annotations_db.py -v`
Expected: FAIL — `episode_annotations` is dropped, and `_load_annotations_from_db`/`_upsert_annotation` still hit the old table internally.

- [ ] **Step 3: Add `_get_serial` and rewrite `_load_annotations_from_db`**

In `backend/datasets/services/episode_service.py`, add after the other DB helpers (near line 150):

```python
async def _get_serial(db, dataset_id: int, episode_index: int) -> str | None:
    async with db.execute(
        "SELECT serial_number FROM episode_serials WHERE dataset_id = ? AND episode_index = ?",
        (dataset_id, episode_index),
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row else None
```

Replace `_load_annotations_from_db` body:

```python
async def _load_annotations_from_db(dataset_id: int) -> dict[int, dict]:
    db = await get_db()
    async with db.execute(
        """SELECT es.episode_index, a.grade, a.tags, a.reason
           FROM episode_serials es
           LEFT JOIN annotations a ON a.serial_number = es.serial_number
           WHERE es.dataset_id = ?""",
        (dataset_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return {
        row[0]: {
            "grade": row[1],
            "tags": _json.loads(row[2]) if row[2] else [],
            "reason": row[3],
        }
        for row in rows
    }
```

- [ ] **Step 4: Run tests to verify partial pass**

Run: `pytest tests/test_episode_annotations_db.py::TestGetEpisodes -v`
Expected: PASS — read path works for already-populated data.

Other tests still FAIL because writes don't go through serial yet — addressed in Task 7.

- [ ] **Step 5: Commit**

```bash
git add backend/datasets/services/episode_service.py tests/test_episode_annotations_db.py
git commit -m "refactor(episode_service): add _get_serial helper and JOIN-based read"
```

---

## Task 7: Serial-resolved writes + stats refresh

**Files:**
- Modify: `backend/datasets/services/episode_service.py`

- [ ] **Step 1: Rewrite `_upsert_annotation`**

Replace the body of `_upsert_annotation` (currently the function that writes to `episode_annotations`) in `backend/datasets/services/episode_service.py`:

```python
async def _upsert_annotation(
    dataset_id: int,
    episode_index: int,
    grade: str | None,
    tags: list[str],
    reason: str | None,
) -> None:
    db = await get_db()
    serial = await _get_serial(db, dataset_id, episode_index)
    if serial is None:
        raise ValueError(
            f"no serial_number for dataset_id={dataset_id} episode={episode_index}; "
            "run cell browse first to populate episode_serials"
        )
    await db.execute(
        """INSERT INTO annotations (serial_number, grade, tags, reason, updated_at)
           VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
           ON CONFLICT(serial_number) DO UPDATE SET
             grade=excluded.grade, tags=excluded.tags, reason=excluded.reason,
             updated_at=excluded.updated_at""",
        (serial, grade, _json.dumps(tags), reason),
    )
    await db.commit()
```

- [ ] **Step 2: Rewrite `_refresh_dataset_stats` aggregation**

Find the `_refresh_dataset_stats` function (around line 210) and replace the grade-count query block:

```python
    async with db.execute(
        """SELECT
             COUNT(a.grade),
             SUM(CASE WHEN a.grade='good' THEN 1 ELSE 0 END),
             SUM(CASE WHEN a.grade='normal' THEN 1 ELSE 0 END),
             SUM(CASE WHEN a.grade='bad' THEN 1 ELSE 0 END)
           FROM episode_serials es
           LEFT JOIN annotations a ON a.serial_number = es.serial_number
           WHERE es.dataset_id = ?""",
        (dataset_id,),
    ) as cursor:
        row = await cursor.fetchone()
```

Everything after remains unchanged (the row unpacking still works).

- [ ] **Step 3: Seed `episode_serials` in `_ensure_dataset_registered`**

When an isolated test inserts a dataset via `_ensure_dataset_registered`, `_upsert_annotation` will fail on missing serial. Add a fallback that, when `episode_serials` is empty for that dataset, rebuilds from parquet:

Modify `_ensure_dataset_registered` (around line 140):

```python
async def _ensure_dataset_registered(dataset_path: Path) -> int:
    db = await get_db()
    resolved = str(dataset_path.resolve())
    async with db.execute("SELECT id FROM datasets WHERE path = ?", (resolved,)) as cursor:
        row = await cursor.fetchone()
    if row:
        dataset_id = row[0]
    else:
        await db.execute("INSERT INTO datasets (path, name) VALUES (?, ?)", (resolved, dataset_path.name))
        await db.commit()
        async with db.execute("SELECT id FROM datasets WHERE path = ?", (resolved,)) as cursor:
            dataset_id = (await cursor.fetchone())[0]

    # Ensure episode_serials is populated so annotation writes can resolve serials
    async with db.execute(
        "SELECT COUNT(*) FROM episode_serials WHERE dataset_id = ?", (dataset_id,)
    ) as cursor:
        n = (await cursor.fetchone())[0]
    if n == 0:
        from backend.datasets.services.cell_service import _rebuild_episode_serials
        await _rebuild_episode_serials(db, dataset_id, dataset_path)
        await db.commit()

    return dataset_id
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_episode_annotations_db.py -v`
Expected: `TestUpdateEpisode`, `TestBulkGrade`, `TestGetEpisodes` pass. `TestSidecarMigration` still fails — Task 8.

- [ ] **Step 5: Commit**

```bash
git add backend/datasets/services/episode_service.py
git commit -m "refactor(episode_service): route annotation writes through serial_number"
```

---

## Task 8: `_ensure_migrated` with serial resolution + `INSERT OR IGNORE`

**Files:**
- Modify: `backend/datasets/services/episode_service.py`
- Create: `tests/test_sidecar_migration_v4.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_sidecar_migration_v4.py`:

```python
"""v4 sidecar migration: grades get keyed by serial, OR IGNORE protects
existing annotations from stale sidecar overwrites.
"""

import json
import tempfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import pytest_asyncio

from backend.core.db import get_db, init_db, close_db, _reset


@pytest_asyncio.fixture
async def tmp_db(monkeypatch):
    _reset()
    tmp = Path(tempfile.mkdtemp()) / "test.db"
    monkeypatch.setattr("backend.core.db._db_path_override", str(tmp))
    await init_db()
    yield tmp
    await close_db()
    _reset()
    tmp.unlink(missing_ok=True)


def _mk_ds(root: Path, name: str, serials: list[str]) -> Path:
    d = root / name
    (d / "meta" / "episodes" / "chunk-000").mkdir(parents=True)
    (d / "meta" / "info.json").write_text(json.dumps({
        "fps": 30, "total_episodes": len(serials), "total_tasks": 1,
        "robot_type": "t", "features": {},
    }))
    t = pa.table({
        "episode_index": pa.array(list(range(len(serials))), type=pa.int64()),
        "Serial_number": pa.array(serials, type=pa.string()),
    })
    pq.write_table(t, d / "meta" / "episodes" / "chunk-000" / "file-000.parquet")
    return d


@pytest.mark.asyncio
async def test_sidecar_migrates_by_serial(tmp_db, tmp_path, monkeypatch):
    from backend.datasets.services.episode_service import (
        _sidecar_file, _ensure_dataset_registered, _ensure_migrated,
    )
    monkeypatch.setattr(
        "backend.core.config.settings.annotations_path",
        str(tmp_path / "annotations"),
    )
    ds_dir = _mk_ds(tmp_path, "d1", ["S-A", "S-B", "S-C"])

    # Legacy sidecar file
    sidecar_path = _sidecar_file(ds_dir)
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(json.dumps({
        "0": {"grade": "good", "tags": ["x"]},
        "2": {"grade": "bad", "tags": []},
    }))

    dataset_id = await _ensure_dataset_registered(ds_dir)
    await _ensure_migrated(dataset_id, ds_dir)

    db = await get_db()
    async with db.execute("SELECT serial_number, grade FROM annotations ORDER BY serial_number") as cur:
        rows = [tuple(r) for r in await cur.fetchall()]
    assert rows == [("S-A", "good"), ("S-C", "bad")]


@pytest.mark.asyncio
async def test_existing_annotation_not_clobbered(tmp_db, tmp_path, monkeypatch):
    from backend.datasets.services.episode_service import (
        _sidecar_file, _ensure_dataset_registered, _ensure_migrated,
    )
    monkeypatch.setattr(
        "backend.core.config.settings.annotations_path",
        str(tmp_path / "annotations"),
    )
    ds_dir = _mk_ds(tmp_path, "d2", ["S-A"])
    db = await get_db()

    # Pre-seed an annotation for S-A
    dataset_id = await _ensure_dataset_registered(ds_dir)
    await db.execute(
        "INSERT INTO annotations (serial_number, grade) VALUES ('S-A', 'normal')"
    )
    await db.commit()

    sidecar_path = _sidecar_file(ds_dir)
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(json.dumps({"0": {"grade": "bad", "tags": []}}))

    await _ensure_migrated(dataset_id, ds_dir)

    async with db.execute("SELECT grade FROM annotations WHERE serial_number='S-A'") as cur:
        grade = (await cur.fetchone())[0]
    assert grade == "normal"  # OR IGNORE protected it


@pytest.mark.asyncio
async def test_skip_when_already_annotated(tmp_db, tmp_path, monkeypatch, caplog):
    """If any annotation reachable from this dataset exists, migration is skipped."""
    from backend.datasets.services.episode_service import (
        _sidecar_file, _ensure_dataset_registered, _ensure_migrated,
    )
    monkeypatch.setattr(
        "backend.core.config.settings.annotations_path",
        str(tmp_path / "annotations"),
    )
    ds_dir = _mk_ds(tmp_path, "d3", ["S-A", "S-B"])
    db = await get_db()

    dataset_id = await _ensure_dataset_registered(ds_dir)
    await db.execute(
        "INSERT INTO annotations (serial_number, grade) VALUES ('S-A', 'good')"
    )
    await db.commit()

    sidecar_path = _sidecar_file(ds_dir)
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(json.dumps({
        "0": {"grade": "bad", "tags": []},
        "1": {"grade": "bad", "tags": []},
    }))

    await _ensure_migrated(dataset_id, ds_dir)

    async with db.execute("SELECT COUNT(*) FROM annotations") as cur:
        assert (await cur.fetchone())[0] == 1  # nothing added from sidecar
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_sidecar_migration_v4.py -v`
Expected: FAIL — `_ensure_migrated` still references `episode_annotations`.

- [ ] **Step 3: Rewrite `_ensure_migrated`**

Replace the function body in `backend/datasets/services/episode_service.py`:

```python
async def _ensure_migrated(dataset_id: int, dataset_path: Path) -> None:
    db = await get_db()
    async with db.execute(
        """SELECT COUNT(*)
           FROM episode_serials es
           JOIN annotations a ON a.serial_number = es.serial_number
           WHERE es.dataset_id = ?""",
        (dataset_id,),
    ) as cursor:
        count_row = await cursor.fetchone()
    if count_row[0] > 0:
        return

    sidecar = _load_sidecar_json(dataset_path)
    if not sidecar:
        return

    migrated = 0
    for idx_str, ann in sidecar.items():
        serial = await _get_serial(db, dataset_id, int(idx_str))
        if serial is None:
            logger.warning(
                "sidecar migration: no serial for ep %s in %s; skipping",
                idx_str, dataset_path,
            )
            continue
        await db.execute(
            """INSERT OR IGNORE INTO annotations (serial_number, grade, tags, reason)
               VALUES (?, ?, ?, NULL)""",
            (serial, ann.get("grade"), _json.dumps(ann.get("tags", []))),
        )
        migrated += 1
    await db.commit()
    await _refresh_dataset_stats(dataset_id)
    logger.info(
        "Migrated %d annotations from sidecar for %s (of %d entries)",
        migrated, dataset_path.name, len(sidecar),
    )
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_sidecar_migration_v4.py tests/test_episode_annotations_db.py::TestSidecarMigration -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add backend/datasets/services/episode_service.py tests/test_sidecar_migration_v4.py
git commit -m "refactor(episode_service): migrate sidecar annotations via serial with OR IGNORE"
```

---

## Task 9: Auto-grade service — serial-resolved reads and writes

**Files:**
- Modify: `backend/datasets/services/auto_grade_service.py`
- Modify: `tests/test_auto_grade_service.py`
- Modify: `tests/test_grade_reason.py`

- [ ] **Step 1: Inspect the existing auto-grade tests**

Run: `pytest tests/test_auto_grade_service.py tests/test_grade_reason.py -v`
Expected: both suites FAIL at fixture level or on queries against the dropped `episode_annotations` table.

- [ ] **Step 2a: Ensure serials are populated before the auto-grade pass**

Auto-grade can be invoked from the dataset-service load path, independent of cell browsing. If `episode_serials` is empty at that moment, every annotation write silently skips and `auto_graded_at` gets stamped — the auto-grade pass would be permanently disabled for that dataset. Guard against this at the top of `ensure_auto_graded`, right after the `auto_graded_at` early-return:

```python
    async with db.execute(
        "SELECT COUNT(*) FROM episode_serials WHERE dataset_id = ?", (dataset_id,)
    ) as cursor:
        serial_count = (await cursor.fetchone())[0]
    if serial_count == 0:
        from backend.datasets.services.cell_service import _rebuild_episode_serials
        await _rebuild_episode_serials(db, dataset_id, dataset_path)
        await db.commit()
```

- [ ] **Step 2b: Rewrite the "already graded" query**

In `backend/datasets/services/auto_grade_service.py`, replace the block around lines 274-279:

```python
    async with db.execute(
        """SELECT es.episode_index
           FROM episode_serials es
           JOIN annotations a ON a.serial_number = es.serial_number
           WHERE es.dataset_id = ? AND a.grade IS NOT NULL""",
        (dataset_id,),
    ) as cursor:
        graded_rows = await cursor.fetchall()
    already_graded: set[int] = {r[0] for r in graded_rows}
```

- [ ] **Step 3: Rewrite the auto-grade write loop**

Replace the block around lines 306-320:

```python
    for ep_idx, reason in auto_updates:
        async with db.execute(
            "SELECT serial_number FROM episode_serials WHERE dataset_id = ? AND episode_index = ?",
            (dataset_id, ep_idx),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            logger.warning(
                "auto_grade: skip ep %d in dataset_id=%s (no serial_number)",
                ep_idx, dataset_id,
            )
            continue
        serial = row[0]
        await db.execute(
            """INSERT INTO annotations (serial_number, grade, tags, reason, updated_at)
               VALUES (?, 'normal', '[]', ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
               ON CONFLICT(serial_number) DO UPDATE SET
                   grade  = CASE WHEN annotations.grade IS NULL
                                THEN excluded.grade ELSE annotations.grade END,
                   reason = CASE WHEN annotations.grade IS NULL
                                THEN excluded.reason ELSE annotations.reason END,
                   updated_at = excluded.updated_at""",
            (serial, reason),
        )
```

- [ ] **Step 4: Update auto-grade tests**

In `tests/test_auto_grade_service.py`:
- Change any direct `episode_annotations` INSERT in fixtures to insert into `episode_serials` (mapping) and `annotations` (grade data).
- Change any SELECT to the JOIN pattern from Task 6, Step 1.

Example pattern for fixtures:

```python
await db.execute(
    "INSERT INTO episode_serials (dataset_id, episode_index, serial_number) VALUES (?, ?, ?)",
    (dataset_id, ep_idx, f"S-{ep_idx}"),
)
await db.execute(
    "INSERT INTO annotations (serial_number, grade, reason) VALUES (?, ?, ?)",
    (f"S-{ep_idx}", "good", None),
)
```

Do the same sweep on `tests/test_grade_reason.py`.

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_auto_grade_service.py tests/test_grade_reason.py -v`
Expected: all pass. If any pre-existing test encodes "auto overwrites user grade" behavior, its expectation is wrong and must flip — the invariant is user-entered grades are preserved.

- [ ] **Step 6: Commit**

```bash
git add backend/datasets/services/auto_grade_service.py tests/test_auto_grade_service.py tests/test_grade_reason.py
git commit -m "refactor(auto_grade): route through serial-keyed annotations"
```

---

## Task 10: Re-conversion scenario integration test

**Files:**
- Create: `tests/test_reconversion_scenario.py`

- [ ] **Step 1: Write the test**

Create `tests/test_reconversion_scenario.py`:

```python
"""End-to-end: delete a dataset, re-convert with the same Serial_numbers,
and verify the user-entered grade follows the recording automatically.
"""

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import pytest_asyncio

from backend.core.db import get_db, init_db, close_db, _reset


@pytest_asyncio.fixture
async def tmp_db(monkeypatch):
    _reset()
    tmp = Path(tempfile.mkdtemp()) / "test.db"
    monkeypatch.setattr("backend.core.db._db_path_override", str(tmp))
    await init_db()
    yield tmp
    await close_db()
    _reset()
    tmp.unlink(missing_ok=True)


def _mk_cell(parent: Path, cell_name: str, ds_name: str, serials: list[str]) -> Path:
    cell = parent / cell_name
    d = cell / ds_name
    (d / "meta" / "episodes" / "chunk-000").mkdir(parents=True)
    (d / "meta" / "info.json").write_text(json.dumps({
        "fps": 30, "total_episodes": len(serials), "total_tasks": 1,
        "robot_type": "t", "features": {},
    }))
    t = pa.table({
        "episode_index": pa.array(list(range(len(serials))), type=pa.int64()),
        "Serial_number": pa.array(serials, type=pa.string()),
    })
    pq.write_table(t, d / "meta" / "episodes" / "chunk-000" / "file-000.parquet")
    return cell


@pytest.mark.asyncio
async def test_grade_follows_serial_across_reconversion(tmp_db, tmp_path):
    from backend.datasets.services.cell_service import get_datasets_in_cell
    from backend.datasets.services.episode_service import _upsert_annotation

    cell = _mk_cell(tmp_path, "cell000", "task_a", ["S-A", "S-B"])
    await get_datasets_in_cell(str(cell))

    # Resolve dataset_id and write a user grade for episode 1 (serial S-B)
    db = await get_db()
    ds_path = str((cell / "task_a").resolve())
    async with db.execute("SELECT id FROM datasets WHERE path = ?", (ds_path,)) as cur:
        ds_id = (await cur.fetchone())[0]
    await _upsert_annotation(ds_id, 1, grade="good", tags=["keep"], reason=None)

    # Simulate a re-conversion: wipe the dataset row (user deleted + reconverted)
    await db.execute("DELETE FROM datasets WHERE id = ?", (ds_id,))
    await db.commit()
    # episode_serials for old dataset_id is gone (cascade). annotations remain.
    async with db.execute("SELECT COUNT(*) FROM annotations WHERE serial_number='S-B'") as cur:
        assert (await cur.fetchone())[0] == 1

    # Rebuild: same parquet, same Serial_numbers, new dataset_id
    await get_datasets_in_cell(str(cell))

    async with db.execute("SELECT id FROM datasets WHERE path = ?", (ds_path,)) as cur:
        new_ds_id = (await cur.fetchone())[0]
    assert new_ds_id != ds_id

    # Join back: grade is still "good" for the new (dataset_id, episode_index=1)
    async with db.execute(
        """SELECT a.grade, a.tags
           FROM episode_serials es
           JOIN annotations a ON a.serial_number = es.serial_number
           WHERE es.dataset_id = ? AND es.episode_index = 1""",
        (new_ds_id,),
    ) as cur:
        row = await cur.fetchone()
    assert row[0] == "good"
    assert json.loads(row[1]) == ["keep"]


@pytest.mark.asyncio
async def test_reindexing_still_follows_serial(tmp_db, tmp_path):
    """Re-conversion swaps episode order; grade follows the serial, not the index."""
    from backend.datasets.services.cell_service import get_datasets_in_cell
    from backend.datasets.services.episode_service import _upsert_annotation

    cell = _mk_cell(tmp_path, "cell001", "task_b", ["S-X", "S-Y"])
    await get_datasets_in_cell(str(cell))

    db = await get_db()
    ds_path = str((cell / "task_b").resolve())
    async with db.execute("SELECT id FROM datasets WHERE path = ?", (ds_path,)) as cur:
        ds_id = (await cur.fetchone())[0]
    # Grade S-Y (episode 1) as bad
    await _upsert_annotation(ds_id, 1, grade="bad", tags=[], reason="shaky")

    # Rewrite parquet with reversed order: S-Y is now episode 0
    import pyarrow as pa, pyarrow.parquet as pq
    pf = cell / "task_b" / "meta" / "episodes" / "chunk-000" / "file-000.parquet"
    t = pa.table({
        "episode_index": pa.array([0, 1], type=pa.int64()),
        "Serial_number": pa.array(["S-Y", "S-X"], type=pa.string()),
    })
    pq.write_table(t, pf)
    # Touch info.json so mtime-gated sync fires
    info = cell / "task_b" / "meta" / "info.json"
    info.write_text(info.read_text())  # rewrite, advancing mtime
    import os, time
    time.sleep(0.05)
    os.utime(info, None)

    await get_datasets_in_cell(str(cell))

    async with db.execute(
        """SELECT a.grade, a.reason
           FROM episode_serials es
           JOIN annotations a ON a.serial_number = es.serial_number
           WHERE es.dataset_id = ? AND es.episode_index = 0""",
        (ds_id,),
    ) as cur:
        row = await cur.fetchone()
    assert row[0] == "bad"
    assert row[1] == "shaky"
```

- [ ] **Step 2: Run test to verify pass**

Run: `pytest tests/test_reconversion_scenario.py -v`
Expected: both tests pass (all machinery from prior tasks is in place).

- [ ] **Step 3: Commit**

```bash
git add tests/test_reconversion_scenario.py
git commit -m "test: cover reconversion and episode reindex scenarios"
```

---

## Task 11: Sweep residual `episode_annotations` references

**Files:**
- Search: all of `backend/` and `tests/`
- Modify: any remaining callers (expected: small set, most caught already)

- [ ] **Step 1: Grep for remaining references**

Run: `grep -rn "episode_annotations" backend/ tests/ scripts/`
Expected: no matches other than `SCHEMA_V4` (which still says `DROP TABLE IF EXISTS episode_annotations`) and the guard check.

If other matches appear, fix each one:
- SELECT/INSERT/UPDATE against `episode_annotations` → rewrite with the `episode_serials JOIN annotations` pattern.
- Tests that import helpers whose signatures changed → update.

- [ ] **Step 2: Run the full test suite**

Run: `pytest -x -q`
Expected: all pass.

If `tests/test_mockup.py` still exercises the sidecar loader and dataset load flow, verify it still uses `_load_sidecar_json` (kept as a back-compat alias) — no structural changes required.

- [ ] **Step 3: Commit (only if there are diffs)**

```bash
git status
# If any files changed:
git add -u
git commit -m "chore: sweep residual episode_annotations references"
```

If there are no diffs, skip the commit.

---

## Task 12: Staging & live reset

> **NOTE:** These steps are manual. They do not produce a commit. Perform them in the operator's workstation exactly once.

- [ ] **Step 1: Stop the FastAPI server**

Kill the dev server process (e.g., `Ctrl-C` in the shell running `uvicorn`, or `pkill -f uvicorn`). Confirm port 8001 is free:

```bash
lsof -i :8001 || echo "port free"
```

- [ ] **Step 2: Dry-run the reset**

Run: `python -m scripts.reset_db --dry-run`
Expected: prints the DB path and the backup plan, leaves files intact.

- [ ] **Step 3: Execute the reset**

Run: `python -m scripts.reset_db --yes`
Expected: prints `[reset_db] fresh DB initialized at <path> (schema v4)` and one or more `backup retained:` lines.

Verify:

```bash
ls -la ~/.local/share/curation-tools/ | grep metadata
```

- [ ] **Step 4: Restart the server**

Start the dev server as usual (e.g., `uvicorn backend.main:app --host 127.0.0.1 --port 8001`).

- [ ] **Step 5: Browse `cell002` through the UI**

Open the frontend, navigate to a cell, wait for the dataset list to render. Then from a separate shell:

```bash
sqlite3 ~/.local/share/curation-tools/metadata.db \
  "SELECT d.path, COUNT(es.episode_index) FROM datasets d LEFT JOIN episode_serials es ON es.dataset_id=d.id GROUP BY d.id;"
```

Expected: each dataset has a non-zero serial count matching `total_episodes`.

- [ ] **Step 6: Grade one episode and verify**

In the UI, set an episode's grade to `good`. Then:

```bash
sqlite3 ~/.local/share/curation-tools/metadata.db "SELECT * FROM annotations;"
```

Expected: one row with the serial you just graded.

- [ ] **Step 7 (optional): Reconversion sanity check**

Delete one dataset directory and regenerate it through the converter with the same `Serial_number`s. Re-browse and confirm the grade persists.

---

## Self-review checklist (done inline before handoff)

- Spec sections mapped to tasks:
  - Schema v4 + guard → Task 1
  - Backup script → Task 2
  - Lazy mtime sync → Tasks 4-5
  - Serial CRUD → Tasks 6-7
  - Sidecar migration → Task 8
  - Auto-grade → Task 9
  - Reconversion scenario → Task 10
  - Rollout (manual) → Task 12
- No placeholders: every code block is complete, every command is literal.
- Type/name consistency: `_get_serial`, `_rebuild_episode_serials`, `annotations`, `episode_serials`, `info_json_mtime` — single spelling across all tasks.
- Follow-up parity: `_load_annotations_from_db` return shape unchanged, existing callers untouched.
