import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from backend.datasets.services.fields_service import (
    get_info_fields,
    update_info_field,
    delete_info_field,
    get_episode_columns,
    add_episode_column,
)


@pytest.fixture
def mock_dataset(tmp_path: Path):
    info = {
        "fps": 30,
        "total_episodes": 3,
        "robot_type": "ur5e",
        "total_tasks": 1,
        "features": {},
        "custom_field_1": "hello",
        "custom_field_2": 42,
    }
    meta = tmp_path / "meta"
    meta.mkdir()
    (meta / "info.json").write_text(json.dumps(info))

    ep_dir = meta / "episodes" / "chunk-000"
    ep_dir.mkdir(parents=True)
    table = pa.table({
        "episode_index": [0, 1, 2],
        "length": [100, 200, 150],
        "task_index": [0, 0, 1],
    })
    pq.write_table(table, str(ep_dir / "file-000.parquet"))

    return tmp_path


def test_get_info_fields(mock_dataset):
    fields = get_info_fields(str(mock_dataset))
    keys = {f["key"] for f in fields}
    assert "fps" in keys
    assert "custom_field_1" in keys


def test_get_info_fields_marks_system(mock_dataset):
    fields = get_info_fields(str(mock_dataset))
    field_map = {f["key"]: f for f in fields}
    assert field_map["fps"]["is_system"] is True
    assert field_map["custom_field_1"]["is_system"] is False


def test_update_info_field(mock_dataset):
    update_info_field(str(mock_dataset), "custom_field_1", "updated")
    info = json.loads((mock_dataset / "meta" / "info.json").read_text())
    assert info["custom_field_1"] == "updated"


def test_update_info_field_adds_new(mock_dataset):
    update_info_field(str(mock_dataset), "new_field", "new_value")
    info = json.loads((mock_dataset / "meta" / "info.json").read_text())
    assert info["new_field"] == "new_value"


def test_update_info_field_rejects_system(mock_dataset):
    with pytest.raises(ValueError, match="system field"):
        update_info_field(str(mock_dataset), "fps", 60)


def test_delete_info_field(mock_dataset):
    delete_info_field(str(mock_dataset), "custom_field_1")
    info = json.loads((mock_dataset / "meta" / "info.json").read_text())
    assert "custom_field_1" not in info


def test_delete_info_field_rejects_system(mock_dataset):
    with pytest.raises(ValueError, match="system field"):
        delete_info_field(str(mock_dataset), "fps")


def test_get_episode_columns(mock_dataset):
    cols = get_episode_columns(str(mock_dataset))
    names = {c["name"] for c in cols}
    assert "episode_index" in names
    assert "length" in names


def test_add_episode_column(mock_dataset):
    add_episode_column(str(mock_dataset), "quality_score", "float64", 0.0)
    cols = get_episode_columns(str(mock_dataset))
    names = {c["name"] for c in cols}
    assert "quality_score" in names


def test_add_episode_column_duplicate(mock_dataset):
    with pytest.raises(ValueError, match="already exists"):
        add_episode_column(str(mock_dataset), "length", "int64", 0)


def test_add_episode_column_persists_values(mock_dataset):
    add_episode_column(str(mock_dataset), "quality_score", "float64", 0.5)
    parquet_files = list((mock_dataset / "meta" / "episodes").rglob("*.parquet"))
    table = pq.read_table(str(parquet_files[0]))
    values = table.column("quality_score").to_pylist()
    assert all(v == 0.5 for v in values)
