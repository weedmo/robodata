"""Verify the shared mock-dataset helper now includes Serial_number, plus
_rebuild_episode_serials behavior."""

import tempfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import pytest_asyncio

from backend.core.db import get_db, init_db, close_db, _reset


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
    async def test_skips_missing_serial_column(self, tmp_db, tmp_path):
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
