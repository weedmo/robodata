import asyncio
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from backend.core.db import db, init_db
from backend.services.distribution_service import (
    get_available_fields,
    compute_distribution,
)


pytestmark = pytest.mark.usefixtures("_point_settings_at_test_db")


@pytest.fixture(autouse=True)
def _isolate_datasets_table():
    """Reset the datasets table + identity sequence before each test.

    compute_distribution hits the live DB via _ensure_dataset_registered which
    INSERTs into datasets and depends on the SERIAL sequence. Other tests in
    the suite seed datasets with explicit ids (e.g. INSERT ... VALUES (1, ...))
    without advancing the sequence, leaving id=1 occupied while the next
    sequence value still resolves to 1 — that triggers a UniqueViolation when
    this test inserts without an explicit id. TRUNCATE ... RESTART IDENTITY
    pins the world to a clean state regardless of suite ordering.
    """

    async def _reset():
        await init_db()
        await db.execute("TRUNCATE datasets RESTART IDENTITY CASCADE")

    asyncio.run(_reset())
    yield


@pytest.fixture
def mock_dataset(tmp_path: Path):
    info = {
        "fps": 30,
        "total_episodes": 6,
        "robot_type": "ur5e",
        "total_tasks": 2,
        "features": {},
    }
    meta = tmp_path / "meta"
    meta.mkdir()
    (meta / "info.json").write_text(json.dumps(info))

    ep_dir = meta / "episodes" / "chunk-000"
    ep_dir.mkdir(parents=True)
    table = pa.table({
        "episode_index": [0, 1, 2, 3, 4, 5],
        "length": [100, 200, 150, 300, 250, 180],
        "task_index": [0, 0, 1, 1, 0, 1],
        "grade": ["good", "good", "bad", None, "normal", "good"],
        "robot_type": ["ur5e", "ur5e", "ur5e", "ur5e", "ur5e", "ur5e"],
    })
    pq.write_table(table, str(ep_dir / "file-000.parquet"))

    return tmp_path


def test_get_available_fields(mock_dataset):
    fields = get_available_fields(str(mock_dataset))
    names = {f.name for f in fields}
    assert "episode_index" not in names
    assert "length" in names
    assert "grade" in names


def test_get_available_fields_returns_dtype(mock_dataset):
    fields = get_available_fields(str(mock_dataset))
    field_map = {f.name: f for f in fields}
    assert field_map["length"].dtype == "int64"
    assert field_map["grade"].dtype == "string"


def test_compute_distribution_numeric(mock_dataset):
    result = compute_distribution(str(mock_dataset), "length", chart_type="auto")
    assert result.field == "length"
    assert result.chart_type == "histogram"
    assert result.total == 6
    assert sum(b.count for b in result.bins) == 6


def test_compute_distribution_categorical(mock_dataset):
    result = compute_distribution(str(mock_dataset), "grade", chart_type="auto")
    assert result.field == "grade"
    assert result.chart_type == "bar"
    assert result.total == 6
    label_counts = {b.label: b.count for b in result.bins}
    assert label_counts["good"] == 3
    assert label_counts["bad"] == 1


def test_compute_distribution_nonexistent_field(mock_dataset):
    with pytest.raises(ValueError, match="not found"):
        compute_distribution(str(mock_dataset), "nonexistent", chart_type="auto")


def test_compute_distribution_explicit_bar(mock_dataset):
    result = compute_distribution(str(mock_dataset), "task_index", chart_type="bar")
    assert result.chart_type == "bar"
    assert all(isinstance(b.label, str) for b in result.bins)
