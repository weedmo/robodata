"""End-to-end: delete a dataset, re-convert with the same Serial_numbers,
and verify the user-entered grade follows the recording automatically.
"""

import json
import tempfile
import time
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
    from backend.datasets.services.episode_service import _save_annotation_to_db

    cell = _mk_cell(tmp_path, "cell000", "task_a", ["S-A", "S-B"])
    await get_datasets_in_cell(str(cell))

    # Resolve dataset_id and write a user grade for episode 1 (serial S-B)
    db = await get_db()
    ds_path = str((cell / "task_a").resolve())
    async with db.execute("SELECT id FROM datasets WHERE path = ?", (ds_path,)) as cur:
        ds_id = (await cur.fetchone())[0]
    await _save_annotation_to_db(ds_id, 1, grade="good", tags=["keep"], reason=None)

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
    from backend.datasets.services.episode_service import _save_annotation_to_db

    cell = _mk_cell(tmp_path, "cell001", "task_b", ["S-X", "S-Y"])
    await get_datasets_in_cell(str(cell))

    db = await get_db()
    ds_path = str((cell / "task_b").resolve())
    async with db.execute("SELECT id FROM datasets WHERE path = ?", (ds_path,)) as cur:
        ds_id = (await cur.fetchone())[0]
    # Grade S-Y (episode 1) as bad
    await _save_annotation_to_db(ds_id, 1, grade="bad", tags=[], reason="shaky")

    # Rewrite parquet with reversed order: S-Y is now episode 0
    pf = cell / "task_b" / "meta" / "episodes" / "chunk-000" / "file-000.parquet"
    t = pa.table({
        "episode_index": pa.array([0, 1], type=pa.int64()),
        "Serial_number": pa.array(["S-Y", "S-X"], type=pa.string()),
    })
    pq.write_table(t, pf)
    # Bump info.json mtime so the lazy sync fires a rebuild
    info = cell / "task_b" / "meta" / "info.json"
    time.sleep(0.05)
    info.write_text(info.read_text())

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
