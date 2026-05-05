"""Tests for path-keyed dataset registry behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest


def _make_min_dataset(root: Path, name: str, *, length: int = 10) -> Path:
    ds = root / name
    (ds / "meta" / "episodes" / "chunk-000").mkdir(parents=True, exist_ok=True)
    (ds / "data" / "chunk-000").mkdir(parents=True, exist_ok=True)
    (ds / "meta" / "info.json").write_text(json.dumps({
        "fps": 30,
        "total_episodes": 1,
        "total_tasks": 1,
        "robot_type": "test_robot",
        "features": {},
    }))
    pq.write_table(
        pa.table({
            "task_index": pa.array([0], type=pa.int64()),
            "task": pa.array([f"task {name}"], type=pa.string()),
        }),
        ds / "meta" / "tasks.parquet",
    )
    pq.write_table(
        pa.table({
            "episode_index": pa.array([0], type=pa.int64()),
            "length": pa.array([length], type=pa.int64()),
            "task_index": pa.array([0], type=pa.int64()),
            "data/chunk_index": pa.array([0], type=pa.int64()),
            "data/file_index": pa.array([0], type=pa.int64()),
            "dataset_from_index": pa.array([0], type=pa.int64()),
            "dataset_to_index": pa.array([length], type=pa.int64()),
            "Serial_number": pa.array([f"{name}-serial"], type=pa.string()),
        }),
        ds / "meta" / "episodes" / "chunk-000" / "file-000.parquet",
    )
    return ds


@pytest.fixture
def two_datasets(tmp_path, monkeypatch):
    from backend.core.config import settings

    a = _make_min_dataset(tmp_path, "cell005_ds", length=10)
    b = _make_min_dataset(tmp_path, "cell002_ds", length=20)
    monkeypatch.setattr(settings, "allowed_dataset_roots", [str(tmp_path)])
    return a, b


def test_registry_get_returns_path_specific_context(two_datasets):
    from backend.datasets.services.dataset_registry import DatasetRegistry

    a, b = two_datasets
    reg = DatasetRegistry(max_size=4)
    ctx_a = reg.get(a)
    ctx_b = reg.get(b)
    assert ctx_a is not ctx_b
    assert ctx_a.dataset_path == a.resolve()
    assert ctx_b.dataset_path == b.resolve()
    assert ctx_a.get_episodes()[0]["length"] == 10
    assert ctx_b.get_episodes()[0]["length"] == 20


def test_registry_uses_replaced_core_settings_for_allowed_roots(tmp_path, monkeypatch):
    from backend.core import config as config_mod
    from backend.datasets.services import dataset_registry as registry_mod

    ds = _make_min_dataset(tmp_path, "cell009_ds", length=9)
    replacement = config_mod.Settings(allowed_dataset_roots=[str(tmp_path)])
    monkeypatch.setattr(config_mod, "settings", replacement)

    reg = registry_mod.DatasetRegistry(max_size=2)

    assert reg.get(ds).dataset_path == ds.resolve()


def test_registry_caches_same_path(two_datasets):
    from backend.datasets.services.dataset_registry import DatasetRegistry

    a, _ = two_datasets
    reg = DatasetRegistry(max_size=4)
    assert reg.get(a) is reg.get(str(a))


def test_registry_evicts_lru_when_full(two_datasets, tmp_path):
    from backend.datasets.services.dataset_registry import DatasetRegistry

    a, b = two_datasets
    c = _make_min_dataset(tmp_path, "cell007_ds", length=30)
    reg = DatasetRegistry(max_size=2)
    ctx_a = reg.get(a)
    reg.get(b)
    reg.get(c)
    assert reg.get(a) is not ctx_a


def test_registry_dataset_key_maps_to_loaded_context(two_datasets):
    from backend.datasets.services.dataset_registry import DatasetRegistry, dataset_key_for

    a, _ = two_datasets
    reg = DatasetRegistry(max_size=4)
    ctx = reg.get(a)
    assert reg.get_by_key(dataset_key_for(a)) is ctx


def test_registry_dataset_key_survives_context_eviction(two_datasets, tmp_path):
    from backend.datasets.services.dataset_registry import DatasetRegistry, dataset_key_for

    a, b = two_datasets
    c = _make_min_dataset(tmp_path, "cell007_ds", length=30)
    reg = DatasetRegistry(max_size=1)
    key_a = dataset_key_for(a)
    ctx_a = reg.get(a)
    reg.get(b)
    reg.get(c)
    reloaded = reg.get_by_key(key_a)
    assert reloaded.dataset_path == ctx_a.dataset_path
    assert reloaded is not ctx_a


def test_registry_rejects_path_outside_allowed_roots(tmp_path, monkeypatch):
    from backend.core.config import settings
    from backend.datasets.services.dataset_registry import DatasetRegistry

    monkeypatch.setattr(settings, "allowed_dataset_roots", [str(tmp_path / "allowed")])
    with pytest.raises(ValueError):
        DatasetRegistry(max_size=2).get(tmp_path)
