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
    assert grade == "normal"  # pre-existing annotation wins; migration is skipped


@pytest.mark.asyncio
async def test_skip_when_already_annotated(tmp_db, tmp_path, monkeypatch):
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
