"""Postgres-backed regressions for remaining SQLite-only service SQL fragments."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from backend.core.db import _active_states, _reset, close_db, get_db, init_db

pytestmark = pytest.mark.usefixtures("_point_settings_at_test_db")


@pytest.fixture(autouse=True)
async def reset_postgres_db():
    _reset()
    await init_db()
    db = await get_db()
    await db.execute("DROP TABLE IF EXISTS episode_annotations")
    await db.execute(
        "TRUNCATE TABLE jobs, dataset_stats, episode_serials, datasets, annotations "
        "RESTART IDENTITY CASCADE"
    )
    await db.commit()
    yield
    await close_db()


def _write_cell_dataset(cell_path: Path, name: str, serials: list[str]) -> Path:
    dataset_dir = cell_path / name
    (dataset_dir / "meta" / "episodes" / "chunk-000").mkdir(parents=True)
    (dataset_dir / "meta" / "info.json").write_text(
        json.dumps({
            "fps": 30,
            "total_episodes": len(serials),
            "total_tasks": 1,
            "robot_type": "test_robot",
            "features": {},
        }),
        encoding="utf-8",
    )
    table = pa.table({
        "episode_index": pa.array(list(range(len(serials))), type=pa.int64()),
        "dataset_from_index": pa.array([index * 30 for index in range(len(serials))], type=pa.int64()),
        "dataset_to_index": pa.array([(index + 1) * 30 for index in range(len(serials))], type=pa.int64()),
        "Serial_number": pa.array(serials, type=pa.string()),
    })
    pq.write_table(table, dataset_dir / "meta" / "episodes" / "chunk-000" / "file-000.parquet")
    return dataset_dir


def _create_mock_dataset(root: Path) -> Path:
    dataset_dir = root / "mock_ds"
    (dataset_dir / "meta" / "episodes" / "chunk-000").mkdir(parents=True, exist_ok=True)
    (dataset_dir / "data" / "chunk-000").mkdir(parents=True, exist_ok=True)

    (dataset_dir / "meta" / "info.json").write_text(
        json.dumps({
            "fps": 30,
            "total_episodes": 3,
            "total_tasks": 1,
            "robot_type": "test_robot",
            "features": {},
        }),
        encoding="utf-8",
    )

    episodes = pa.table({
        "episode_index": pa.array([0, 1, 2], type=pa.int64()),
        "task_index": pa.array([0, 0, 0], type=pa.int64()),
        "data/chunk_index": pa.array([0, 0, 0], type=pa.int64()),
        "data/file_index": pa.array([0, 0, 0], type=pa.int64()),
        "dataset_from_index": pa.array([0, 100, 200], type=pa.int64()),
        "dataset_to_index": pa.array([100, 200, 300], type=pa.int64()),
        "Serial_number": pa.array(
            [
                "MOCK_20260101_000000_000000",
                "MOCK_20260101_000001_000000",
                "MOCK_20260101_000002_000000",
            ],
            type=pa.string(),
        ),
    })
    pq.write_table(episodes, dataset_dir / "meta" / "episodes" / "chunk-000" / "file-000.parquet")

    pq.write_table(
        pa.table({
            "episode_index": pa.array([0, 1, 2], type=pa.int64()),
            "timestamp": pa.array([0.0, 0.0, 0.0], type=pa.float32()),
        }),
        dataset_dir / "data" / "chunk-000" / "file-000.parquet",
    )

    return dataset_dir


@pytest.mark.asyncio
async def test_get_datasets_in_cell_upserts_and_updates_rows_on_postgres(tmp_path):
    from backend.datasets.services.cell_service import get_datasets_in_cell

    cell_path = tmp_path / "cell010"
    cell_path.mkdir()
    dataset_dir = _write_cell_dataset(cell_path, "dataset_a", ["SER-0", "SER-1"])

    datasets = await get_datasets_in_cell(str(cell_path))

    assert [dataset.name for dataset in datasets] == ["dataset_a"]

    db = await get_db()
    async with db.execute(
        """
        SELECT d.id, d.cell_name, d.fps, d.total_episodes, st.graded_count
        FROM datasets d
        JOIN dataset_stats st ON st.dataset_id = d.id
        WHERE d.path = ?
        """,
        (str(dataset_dir.resolve()),),
    ) as cursor:
        row = await cursor.fetchone()

    assert row is not None
    assert row["cell_name"] == "cell010"
    assert row["fps"] == 30
    assert row["total_episodes"] == 2
    assert row["graded_count"] == 0

    (dataset_dir / "meta" / "info.json").write_text(
        json.dumps({
            "fps": 60,
            "total_episodes": 4,
            "total_tasks": 1,
            "robot_type": "test_robot",
            "features": {"mode": "updated"},
        }),
        encoding="utf-8",
    )

    datasets = await get_datasets_in_cell(str(cell_path))

    assert [dataset.name for dataset in datasets] == ["dataset_a"]
    assert datasets[0].fps == 60
    assert datasets[0].total_episodes == 4

    async with db.execute(
        """
        SELECT d.id, d.fps, d.total_episodes, COUNT(st.dataset_id) AS stats_rows
        FROM datasets d
        LEFT JOIN dataset_stats st ON st.dataset_id = d.id
        WHERE d.path = ?
        GROUP BY d.id, d.fps, d.total_episodes
        """,
        (str(dataset_dir.resolve()),),
    ) as cursor:
        updated_row = await cursor.fetchone()

    assert updated_row is not None
    assert updated_row["id"] == row["id"]
    assert updated_row["fps"] == 60
    assert updated_row["total_episodes"] == 4
    assert updated_row["stats_rows"] == 1


@pytest.mark.asyncio
async def test_legacy_annotation_migration_is_idempotent_on_postgres(tmp_path):
    from backend.datasets.services.cell_service import _rebuild_episode_serials
    from backend.datasets.services.episode_service import _migrate_legacy_episode_annotations

    dataset_path = _create_mock_dataset(tmp_path)
    db = await get_db()
    await db.execute(
        """
        INSERT INTO datasets (path, name, cell_name, fps, total_episodes, robot_type)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (str(dataset_path.resolve()), dataset_path.name, "cell000", 30, 3, "test_robot"),
    )
    await db.commit()

    async with db.execute("SELECT id FROM datasets WHERE path = ?", (str(dataset_path.resolve()),)) as cursor:
        dataset_id = (await cursor.fetchone())["id"]

    await _rebuild_episode_serials(db, dataset_id, dataset_path)
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS episode_annotations (
            dataset_id INTEGER NOT NULL,
            episode_index INTEGER NOT NULL,
            grade TEXT,
            tags JSONB NOT NULL DEFAULT '[]'::jsonb,
            reason TEXT
        )
        """
    )
    await db.execute(
        """
        INSERT INTO episode_annotations (dataset_id, episode_index, grade, tags, reason)
        VALUES (?, ?, ?, ?, ?)
        """,
        (dataset_id, 1, "bad", json.dumps(["legacy"]), "legacy reason"),
    )
    await db.commit()

    assert await _migrate_legacy_episode_annotations(dataset_id) == 1
    assert not _active_states
    assert await _migrate_legacy_episode_annotations(dataset_id) == 0
    assert not _active_states

    async with db.execute(
        """
        SELECT serial_number, grade, tags, reason
        FROM annotations
        ORDER BY serial_number
        """
    ) as cursor:
        rows = await cursor.fetchall()

    assert len(rows) == 1
    assert rows[0]["serial_number"] == "MOCK_20260101_000001_000000"
    assert rows[0]["grade"] == "bad"
    assert json.loads(rows[0]["tags"]) == ["legacy"]
    assert rows[0]["reason"] == "legacy reason"
