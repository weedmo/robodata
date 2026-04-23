import json
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from backend.core.config import settings
from backend.datasets.services.dataset_service import DatasetService
import backend.datasets.services.dataset_service as dataset_service_module
import backend.datasets.services.task_service as task_service


def _write_official_style_dataset(root: Path) -> Path:
    meta_dir = root / "meta"
    episodes_dir = meta_dir / "episodes" / "chunk-000"
    meta_dir.mkdir(parents=True, exist_ok=True)
    episodes_dir.mkdir(parents=True, exist_ok=True)

    (meta_dir / "info.json").write_text(
        json.dumps(
            {
                "codebase_version": "v3.0",
                "robot_type": "test_robot",
                "total_episodes": 1,
                "total_tasks": 1,
                "fps": 30,
                "features": {},
            }
        ),
        encoding="utf-8",
    )

    pq.write_table(
        pa.table(
            {
                "episode_index": pa.array([0], type=pa.int64()),
                "task_index": pa.array([0], type=pa.int64()),
                "length": pa.array([10], type=pa.int64()),
                "data/chunk_index": pa.array([0], type=pa.int64()),
                "data/file_index": pa.array([0], type=pa.int64()),
                "dataset_from_index": pa.array([0], type=pa.int64()),
                "dataset_to_index": pa.array([10], type=pa.int64()),
            }
        ),
        episodes_dir / "file-000.parquet",
    )

    pq.write_table(
        pa.table(
            {
                "task_index": pa.array([0], type=pa.int64()),
                "__index_level_0__": pa.array(["pick the cube"], type=pa.string()),
            }
        ),
        meta_dir / "tasks.parquet",
    )

    return root


def _write_named_index_dataset(root: Path) -> Path:
    dataset_root = _write_official_style_dataset(root)
    tasks_path = dataset_root / "meta" / "tasks.parquet"
    df = pd.DataFrame(
        {"task_index": [0]},
        index=pd.Index(["pick the cube"], name="task_name"),
    )
    df.to_parquet(tasks_path)
    return dataset_root


@pytest.fixture
def official_style_dataset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    dataset_path = _write_official_style_dataset(tmp_path / "official_style_dataset")
    monkeypatch.setattr(
        settings,
        "allowed_dataset_roots",
        settings.allowed_dataset_roots + [str(tmp_path)],
    )
    return dataset_path


@pytest.fixture
def named_index_dataset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    dataset_path = _write_named_index_dataset(tmp_path / "named_index_dataset")
    monkeypatch.setattr(
        settings,
        "allowed_dataset_roots",
        settings.allowed_dataset_roots + [str(tmp_path)],
    )
    return dataset_path


class TestOfficialTasksParquetCompatibility:
    def test_dataset_service_normalizes_official_task_schema(
        self,
        official_style_dataset: Path,
    ):
        dataset_service = DatasetService()

        dataset_service.load_dataset(official_style_dataset)

        assert dataset_service.get_tasks() == [
            {
                "task_index": 0,
                "task": "pick the cube",
            }
        ]

    @pytest.mark.asyncio
    async def test_task_service_updates_official_task_schema_in_place(
        self,
        official_style_dataset: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        dataset_service = DatasetService()
        dataset_service.load_dataset(official_style_dataset)
        monkeypatch.setattr(dataset_service_module, "dataset_service", dataset_service)
        monkeypatch.setattr(task_service, "dataset_service", dataset_service)

        result = await task_service.update_task(0, "place the cube")

        assert result == {"task_index": 0, "task_instruction": "place the cube"}

        tasks_table = pq.read_table(official_style_dataset / "meta" / "tasks.parquet")
        assert tasks_table.column("__index_level_0__").to_pylist() == ["place the cube"]
        assert task_service.get_tasks() == [
            {
                "task_index": 0,
                "task_instruction": "place the cube",
            }
        ]

    def test_dataset_service_normalizes_named_index_task_schema(
        self,
        named_index_dataset: Path,
    ):
        dataset_service = DatasetService()

        dataset_service.load_dataset(named_index_dataset)

        assert dataset_service.get_tasks() == [
            {
                "task_index": 0,
                "task": "pick the cube",
            }
        ]

    @pytest.mark.asyncio
    async def test_task_service_updates_named_index_task_schema_in_place(
        self,
        named_index_dataset: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        dataset_service = DatasetService()
        dataset_service.load_dataset(named_index_dataset)
        monkeypatch.setattr(dataset_service_module, "dataset_service", dataset_service)
        monkeypatch.setattr(task_service, "dataset_service", dataset_service)

        result = await task_service.update_task(0, "place the cube")

        assert result == {"task_index": 0, "task_instruction": "place the cube"}

        tasks_table = pq.read_table(named_index_dataset / "meta" / "tasks.parquet")
        assert tasks_table.column("task_name").to_pylist() == ["place the cube"]
