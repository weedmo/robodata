import asyncio
import json
from pathlib import Path
from typing import Sequence

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from backend.core.db import db, get_db, init_db
from backend.datasets.services.distribution_service import (
    get_available_fields,
    compute_distribution,
)


pytestmark = pytest.mark.usefixtures("_point_settings_at_test_db")


@pytest.fixture(autouse=True)
def _isolate_datasets_table(monkeypatch):
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
        await db.execute("TRUNCATE datasets, annotations RESTART IDENTITY CASCADE")

    asyncio.run(_reset())
    from backend.datasets.services.dataset_registry import dataset_registry, settings as registry_settings

    if "/tmp" not in registry_settings.allowed_dataset_roots:
        monkeypatch.setattr(
            registry_settings,
            "allowed_dataset_roots",
            registry_settings.allowed_dataset_roots + ["/tmp"],
        )
    dataset_registry._items.clear()
    dataset_registry._key_to_path.clear()
    yield
    dataset_registry._items.clear()
    dataset_registry._key_to_path.clear()


def _write_distribution_dataset(root: Path, grades: Sequence[str | None]) -> Path:
    meta = root / "meta"
    meta.mkdir(parents=True)
    (meta / "info.json").write_text(json.dumps({
        "fps": 30,
        "total_episodes": len(grades),
        "robot_type": "ur5e",
        "total_tasks": 1,
        "features": {},
    }))

    ep_dir = meta / "episodes" / "chunk-000"
    ep_dir.mkdir(parents=True)
    indices = list(range(len(grades)))
    table = pa.table({
        "episode_index": indices,
        "length": [100 + i for i in indices],
        "task_index": [0] * len(grades),
        "grade": list(grades),
        "Serial_number": [f"{root.name}_20260101_{i:06d}_000000" for i in indices],
    })
    pq.write_table(table, str(ep_dir / "file-000.parquet"))
    return root


async def _write_db_annotation(dataset_path: Path, episode_index: int, grade: str | None) -> None:
    from backend.datasets.services.episode_service import (
        _ensure_dataset_registered,
        _ensure_migrated,
    )

    dataset_id = await _ensure_dataset_registered(dataset_path)
    await _ensure_migrated(dataset_id, dataset_path)
    database = await get_db()
    async with database.execute(
        "SELECT serial_number FROM episode_serials WHERE dataset_id = ? AND episode_index = ?",
        (dataset_id, episode_index),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    await database.execute(
        """INSERT INTO annotations (serial_number, grade, tags, reason)
           VALUES (?, ?, ?, NULL)
           ON CONFLICT (serial_number) DO UPDATE SET
             grade=excluded.grade,
             tags=excluded.tags,
             reason=excluded.reason""",
        (row[0], grade, "[]"),
    )
    await database.commit()


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
        "Serial_number": [
            "SERIAL_20260101_000000_000000",
            "SERIAL_20260101_000001_000000",
            "SERIAL_20260101_000002_000000",
            "SERIAL_20260101_000003_000000",
            "SERIAL_20260101_000004_000000",
            "SERIAL_20260101_000005_000000",
        ],
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


def test_compute_distribution_db_null_grade_clears_parquet_grade(mock_dataset):
    asyncio.run(_write_db_annotation(mock_dataset, episode_index=0, grade=None))

    result = compute_distribution(str(mock_dataset), "grade", chart_type="auto")
    label_counts = {b.label: b.count for b in result.bins}
    assert label_counts["good"] == 2
    assert label_counts["bad"] == 1
    assert label_counts["normal"] == 1
    assert label_counts["(ungraded)"] == 2


def test_compute_distribution_cache_is_dataset_scoped(tmp_path):
    first = _write_distribution_dataset(tmp_path / "first", ["good"])
    second = _write_distribution_dataset(tmp_path / "second", ["bad", "bad"])

    first_result = compute_distribution(str(first), "grade", chart_type="auto")
    second_result = compute_distribution(str(second), "grade", chart_type="auto")

    assert {b.label: b.count for b in first_result.bins} == {"good": 1}
    assert {b.label: b.count for b in second_result.bins} == {"bad": 2}


def test_compute_distribution_nonexistent_field(mock_dataset):
    with pytest.raises(ValueError, match="not found"):
        compute_distribution(str(mock_dataset), "nonexistent", chart_type="auto")


def test_compute_distribution_explicit_bar(mock_dataset):
    result = compute_distribution(str(mock_dataset), "task_index", chart_type="bar")
    assert result.chart_type == "bar"
    assert all(isinstance(b.label, str) for b in result.bins)
