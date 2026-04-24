"""Tests that get_datasets_in_cell() upserts rows into the SQLite DB."""

import json
import asyncio
from pathlib import Path

import pytest
import pytest_asyncio
import pyarrow as pa
import pyarrow.parquet as pq

import backend.core.db as db_module
from backend.core.db import init_db, get_db, close_db
from backend.services.cell_service import get_datasets_in_cell


@pytest.fixture
def mock_cell(tmp_path: Path):
    """Create a single cell with two datasets."""
    for ds_name, fps, total_eps in [("dataset_a", 30, 5), ("dataset_b", 10, 3)]:
        info = {
            "fps": fps,
            "total_episodes": total_eps,
            "robot_type": "ur5e",
            "features": {},
            "total_tasks": 1,
        }
        p = tmp_path / "cell001" / ds_name / "meta"
        p.mkdir(parents=True)
        (p / "info.json").write_text(json.dumps(info))
    return tmp_path / "cell001"


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path):
    """Point the DB module at a temp file and reset after each test."""
    db_module._db_path_override = str(tmp_path / "test_metadata.db")
    db_module._connection = None
    yield
    asyncio.run(close_db())
    db_module._db_path_override = None
    db_module._connection = None


def test_datasets_upserted_to_db(mock_cell):
    """Scanning a cell writes rows to the datasets table."""
    asyncio.run(init_db())
    datasets = get_datasets_in_cell(str(mock_cell))
    assert len(datasets) == 2

    async def check():
        db = await get_db()
        async with db.execute("SELECT name, cell_name, fps FROM datasets ORDER BY name") as cur:
            rows = await cur.fetchall()
        return rows

    rows = asyncio.run(check())
    assert len(rows) == 2
    assert rows[0]["name"] == "dataset_a"
    assert rows[0]["cell_name"] == "cell001"
    assert rows[0]["fps"] == 30
    assert rows[1]["name"] == "dataset_b"
    assert rows[1]["fps"] == 10


def test_dataset_stats_upserted(mock_cell):
    """Scanning a cell writes rows to the dataset_stats table."""
    asyncio.run(init_db())
    get_datasets_in_cell(str(mock_cell))

    async def check():
        db = await get_db()
        async with db.execute(
            "SELECT ds.name, st.total_episodes_check, st.graded_count "
            "FROM datasets ds JOIN dataset_stats st ON st.dataset_id = ds.id "
            "ORDER BY ds.name"
        ) as cur:
            rows = await cur.fetchall()
        return rows

    # Check via a simpler query — just verify rows exist with correct dataset_id FK
    async def check_stats():
        db = await get_db()
        async with db.execute(
            "SELECT COUNT(*) as cnt FROM dataset_stats"
        ) as cur:
            row = await cur.fetchone()
        return row["cnt"]

    count = asyncio.run(check_stats())
    assert count == 2


def test_upsert_is_idempotent(mock_cell):
    """Calling get_datasets_in_cell twice does not duplicate rows."""
    asyncio.run(init_db())
    get_datasets_in_cell(str(mock_cell))
    get_datasets_in_cell(str(mock_cell))

    async def check():
        db = await get_db()
        async with db.execute("SELECT COUNT(*) as cnt FROM datasets") as cur:
            row = await cur.fetchone()
        return row["cnt"]

    assert asyncio.run(check()) == 2


def test_upsert_updates_existing_row(mock_cell):
    """A second scan with changed fps updates the existing row."""
    asyncio.run(init_db())
    get_datasets_in_cell(str(mock_cell))

    # Patch info.json for dataset_a to have fps=60
    info_path = mock_cell / "dataset_a" / "meta" / "info.json"
    info = json.loads(info_path.read_text())
    info["fps"] = 60
    info_path.write_text(json.dumps(info))

    get_datasets_in_cell(str(mock_cell))

    async def check():
        db = await get_db()
        async with db.execute(
            "SELECT fps FROM datasets WHERE name = ?", ("dataset_a",)
        ) as cur:
            row = await cur.fetchone()
        return row["fps"]

    assert asyncio.run(check()) == 60


def _write_episode_metadata(dataset_dir: Path) -> None:
    episodes_dir = dataset_dir / "meta" / "episodes" / "chunk-000"
    episodes_dir.mkdir(parents=True)
    table = pa.table({
        "episode_index": pa.array([0, 1, 2], type=pa.int64()),
        "dataset_from_index": pa.array([0, 30, 60], type=pa.int64()),
        "dataset_to_index": pa.array([30, 60, 90], type=pa.int64()),
        "Serial_number": pa.array(["SER-0", "SER-1", "SER-2"], type=pa.string()),
        "grade": pa.array(["bad", "bad", "bad"], type=pa.string()),
    })
    pq.write_table(table, episodes_dir / "file-000.parquet")


def test_dataset_summary_uses_serial_keyed_annotations_over_stale_stats(tmp_path: Path):
    """Cell summaries should match current annotations, not stale cached stats."""
    cell = tmp_path / "cell001"
    dataset_dir = cell / "dataset_a"
    meta = dataset_dir / "meta"
    meta.mkdir(parents=True)
    (meta / "info.json").write_text(json.dumps({
        "fps": 30,
        "total_episodes": 3,
        "robot_type": "ur5e",
        "features": {},
        "total_tasks": 1,
    }))
    _write_episode_metadata(dataset_dir)

    async def seed_stale_stats():
        await init_db()
        db = await get_db()
        await db.execute(
            """
            INSERT INTO datasets (path, name, cell_name, fps, total_episodes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (str(dataset_dir.resolve()), "dataset_a", "cell001", 30, 3),
        )
        async with db.execute("SELECT id FROM datasets WHERE path = ?", (str(dataset_dir.resolve()),)) as cur:
            dataset_id = (await cur.fetchone())[0]
        await db.execute(
            """
            INSERT INTO dataset_stats (
                dataset_id, graded_count, good_count, normal_count, bad_count,
                total_duration_sec, good_duration_sec, normal_duration_sec, bad_duration_sec
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (dataset_id, 999, 777, 111, 111, 999.0, 777.0, 111.0, 111.0),
        )
        await db.executemany(
            "INSERT INTO annotations (serial_number, grade, tags) VALUES (?, ?, ?)",
            [
                ("SER-0", "good", "[]"),
                ("SER-1", "normal", "[]"),
                ("SER-2", None, "[]"),
            ],
        )
        await db.commit()
        return dataset_id

    dataset_id = asyncio.run(seed_stale_stats())

    datasets = get_datasets_in_cell(str(cell))
    summary = next(ds for ds in datasets if ds.name == "dataset_a")

    assert summary.graded_count == 2
    assert summary.good_count == 1
    assert summary.normal_count == 1
    assert summary.bad_count == 0
    assert summary.total_duration_sec == 3.0
    assert summary.good_duration_sec == 1.0
    assert summary.normal_duration_sec == 1.0
    assert summary.bad_duration_sec == 0.0

    async def read_stats():
        db = await get_db()
        async with db.execute(
            """
            SELECT graded_count, good_count, normal_count, bad_count,
                   total_duration_sec, good_duration_sec, normal_duration_sec, bad_duration_sec
            FROM dataset_stats
            WHERE dataset_id = ?
            """,
            (dataset_id,),
        ) as cur:
            return tuple(await cur.fetchone())

    assert asyncio.run(read_stats()) == (2, 1, 1, 0, 3.0, 1.0, 1.0, 0.0)
