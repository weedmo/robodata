"""Regression tests for preserving legacy episode_annotations data after startup."""

import tempfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import pytest_asyncio

from backend.core.db import (
    SCHEMA_V1,
    SCHEMA_V2,
    SCHEMA_V3,
    _reset,
    close_db,
    get_db,
    init_db,
)
from tests.test_episode_annotations_db import _create_mock_dataset


@pytest_asyncio.fixture(autouse=True)
async def tmp_db(monkeypatch):
    _reset()
    tmp = Path(tempfile.mkdtemp()) / "test.db"
    monkeypatch.setattr("backend.core.db._db_path_override", str(tmp))
    yield tmp
    await close_db()
    _reset()
    tmp.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_legacy_episode_annotations_are_migrated_on_dataset_access(tmp_db, tmp_path, monkeypatch):
    from backend.core.config import settings
    from backend.datasets.services.dataset_service import DatasetService
    from backend.datasets.services.episode_service import EpisodeService
    import backend.datasets.services.dataset_service as ds_mod
    import backend.datasets.services.episode_service as ep_mod

    dataset_path = _create_mock_dataset(tmp_path)
    resolved_path = str(dataset_path.resolve())
    parquet_path = dataset_path / "meta" / "episodes" / "chunk-000" / "file-000.parquet"
    table = pq.read_table(parquet_path)
    if "length" not in table.schema.names:
        table = table.append_column("length", pa.array([100, 100, 100], type=pa.int64()))
        pq.write_table(table, parquet_path)

    db = await get_db()
    await db.executescript(SCHEMA_V1)
    await db.executescript(SCHEMA_V2)
    await db.executescript(SCHEMA_V3)
    await db.execute("PRAGMA user_version = 3")
    await db.execute(
        """
        INSERT INTO datasets (
            id, path, name, cell_name, fps, total_episodes, robot_type
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (1, resolved_path, dataset_path.name, "cell000", 30, 3, "test_robot"),
    )
    await db.execute(
        """
        INSERT INTO episode_annotations (dataset_id, episode_index, grade, tags, reason)
        VALUES (?, ?, ?, ?, ?)
        """,
        (1, 1, "bad", '["legacy"]', "legacy reason"),
    )
    await db.commit()

    await init_db()

    original_roots = settings.allowed_dataset_roots
    monkeypatch.setattr(settings, "allowed_dataset_roots", original_roots + [str(dataset_path.parent)])

    ds_mod.dataset_service = DatasetService()
    ds_mod.dataset_service.load_dataset(str(dataset_path))
    ep_mod.dataset_service = ds_mod.dataset_service
    ep_mod.episode_service = EpisodeService()

    episodes = await ep_mod.episode_service.get_episodes()
    migrated = next(ep for ep in episodes if ep["episode_index"] == 1)
    assert migrated["grade"] == "bad"
    assert migrated["tags"] == ["legacy"]
    assert migrated["reason"] == "legacy reason"

    async with db.execute(
        "SELECT grade, tags, reason FROM annotations WHERE serial_number = ?",
        ("MOCK_20260101_000001_000000",),
    ) as cursor:
        row = await cursor.fetchone()
    assert row[0] == "bad"
    assert row[2] == "legacy reason"
