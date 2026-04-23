"""Verify lazy sync: parquet rescans only when info.json mtime changes, and
stale dataset rows are cleared from the cell after disk removal.
"""

import json
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

        from backend.datasets.services import cell_service
        with patch.object(
            cell_service, "_rebuild_episode_serials", wraps=cell_service._rebuild_episode_serials
        ) as spy:
            await _browse(cell)
            assert spy.call_count == 1
