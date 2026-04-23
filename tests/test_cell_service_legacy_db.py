"""Regression tests for cell dataset listing against legacy metadata DB schemas."""

import json

import pytest

import backend.core.db as db_module
from backend.core.db import SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, close_db, get_db
from backend.datasets.services.cell_service import get_datasets_in_cell


@pytest.fixture(autouse=True)
async def isolated_db(tmp_path, monkeypatch):
    """Point the DB module at an isolated temp file for each test."""
    db_module._db_path_override = str(tmp_path / "legacy_metadata.db")
    db_module._connection = None
    yield
    await close_db()
    db_module._db_path_override = None
    db_module._connection = None


@pytest.fixture
def legacy_dataset_root(tmp_path):
    """Create a dataset root with one valid LeRobot dataset."""
    info = {
        "fps": 30,
        "total_episodes": 5,
        "robot_type": "ur5e",
        "features": {},
        "total_tasks": 1,
    }
    dataset_meta = tmp_path / "lerobot_test" / "dataset_a" / "meta"
    dataset_meta.mkdir(parents=True)
    (dataset_meta / "info.json").write_text(json.dumps(info), encoding="utf-8")
    return tmp_path / "lerobot_test"


@pytest.mark.asyncio
async def test_get_datasets_in_cell_supports_v3_db_schema(legacy_dataset_root):
    """Listing datasets should still work when the metadata DB has not been upgraded to v4."""
    db = await get_db()
    await db.executescript(SCHEMA_V1)
    await db.executescript(SCHEMA_V2)
    await db.executescript(SCHEMA_V3)
    await db.execute("PRAGMA user_version = 3")
    await db.commit()

    datasets = await get_datasets_in_cell(str(legacy_dataset_root))

    assert [dataset.name for dataset in datasets] == ["dataset_a"]

    async with db.execute(
        "SELECT name, cell_name, fps, total_episodes FROM datasets ORDER BY name"
    ) as cursor:
        rows = await cursor.fetchall()

    assert len(rows) == 1
    assert rows[0]["name"] == "dataset_a"
    assert rows[0]["cell_name"] == "lerobot_test"
    assert rows[0]["fps"] == 30
    assert rows[0]["total_episodes"] == 5
