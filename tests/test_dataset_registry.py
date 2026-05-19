"""Tests for path-keyed dataset registry behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest


def _make_min_dataset(
    root: Path,
    name: str,
    *,
    length: int = 10,
    total_episodes: int = 1,
) -> Path:
    ds = root / name
    (ds / "meta" / "episodes" / "chunk-000").mkdir(parents=True, exist_ok=True)
    (ds / "data" / "chunk-000").mkdir(parents=True, exist_ok=True)
    (ds / "meta" / "info.json").write_text(json.dumps({
        "fps": 30,
        "total_episodes": total_episodes,
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


def _rewrite_episode_parquet(ds: Path, episode_indexes: list[int], *, length: int = 10) -> None:
    n = len(episode_indexes)
    pq.write_table(
        pa.table({
            "episode_index": pa.array(episode_indexes, type=pa.int64()),
            "length": pa.array([length] * n, type=pa.int64()),
            "task_index": pa.array([0] * n, type=pa.int64()),
            "data/chunk_index": pa.array([0] * n, type=pa.int64()),
            "data/file_index": pa.array([0] * n, type=pa.int64()),
            "dataset_from_index": pa.array(
                [i * length for i in range(n)], type=pa.int64()
            ),
            "dataset_to_index": pa.array(
                [(i + 1) * length for i in range(n)], type=pa.int64()
            ),
            "Serial_number": pa.array([f"{ds.name}-serial"] * n, type=pa.string()),
        }),
        ds / "meta" / "episodes" / "chunk-000" / "file-000.parquet",
    )


def _bump_mtime(path: Path, delta_seconds: float = 2.0) -> None:
    """Force a future mtime to ensure fingerprint diff on filesystems with low resolution."""
    st = path.stat()
    import os
    os.utime(path, (st.st_atime + delta_seconds, st.st_mtime + delta_seconds))


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


# ---------------------------------------------------------------------------
# Metadata fingerprint auto-reload regression tests (PRD: cache refresh).
# Each test creates a one-episode dataset, rewrites the relevant metadata file,
# and asserts the registry returns a fresh DatasetContext that reflects disk
# state without invalidate() or process restart.
# ---------------------------------------------------------------------------


def test_registry_reloads_when_info_json_changes(tmp_path, monkeypatch):
    """Cached context must be replaced when meta/info.json is rewritten."""
    from backend.core.config import settings
    from backend.datasets.services.dataset_registry import DatasetRegistry

    ds = _make_min_dataset(tmp_path, "cell_info", length=10, total_episodes=1)
    monkeypatch.setattr(settings, "allowed_dataset_roots", [str(tmp_path)])

    reg = DatasetRegistry(max_size=4)
    ctx_old = reg.get(ds)
    assert ctx_old.get_info()["total_episodes"] == 1

    # Rewrite info.json + episode parquet so both fingerprint inputs change.
    (ds / "meta" / "info.json").write_text(json.dumps({
        "fps": 30,
        "total_episodes": 2,
        "total_tasks": 1,
        "robot_type": "test_robot",
        "features": {},
    }))
    _bump_mtime(ds / "meta" / "info.json")
    _rewrite_episode_parquet(ds, [0, 1], length=10)
    _bump_mtime(ds / "meta" / "episodes" / "chunk-000" / "file-000.parquet")

    ctx_new = reg.get(ds)
    assert ctx_new is not ctx_old, "registry must replace stale context after info.json rewrite"
    assert ctx_new.get_info()["total_episodes"] == 2
    assert [int(e["episode_index"]) for e in ctx_new.get_episodes()] == [0, 1]


def test_registry_reloads_when_tasks_parquet_changes(tmp_path, monkeypatch):
    """Cached context must be replaced when meta/tasks.parquet is rewritten."""
    from backend.core.config import settings
    from backend.datasets.services.dataset_registry import DatasetRegistry

    ds = _make_min_dataset(tmp_path, "cell_tasks", length=10)
    monkeypatch.setattr(settings, "allowed_dataset_roots", [str(tmp_path)])

    reg = DatasetRegistry(max_size=4)
    ctx_old = reg.get(ds)
    old_task_count = len(ctx_old.get_tasks())

    pq.write_table(
        pa.table({
            "task_index": pa.array([0, 1], type=pa.int64()),
            "task": pa.array(["task a", "task b"], type=pa.string()),
        }),
        ds / "meta" / "tasks.parquet",
    )
    _bump_mtime(ds / "meta" / "tasks.parquet")

    ctx_new = reg.get(ds)
    assert ctx_new is not ctx_old
    assert len(ctx_new.get_tasks()) == 2
    assert len(ctx_new.get_tasks()) != old_task_count


def test_registry_reloads_when_episode_parquet_file_count_changes(tmp_path, monkeypatch):
    """Adding a new chunk parquet file must trigger context reload."""
    from backend.core.config import settings
    from backend.datasets.services.dataset_registry import DatasetRegistry

    ds = _make_min_dataset(tmp_path, "cell_chunks", length=5)
    monkeypatch.setattr(settings, "allowed_dataset_roots", [str(tmp_path)])

    reg = DatasetRegistry(max_size=4)
    ctx_old = reg.get(ds)
    assert len(ctx_old.iter_episode_parquet_files()) == 1

    # Add a second chunk-001 file with another episode.
    (ds / "meta" / "episodes" / "chunk-001").mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({
            "episode_index": pa.array([1], type=pa.int64()),
            "length": pa.array([5], type=pa.int64()),
            "task_index": pa.array([0], type=pa.int64()),
            "data/chunk_index": pa.array([1], type=pa.int64()),
            "data/file_index": pa.array([0], type=pa.int64()),
            "dataset_from_index": pa.array([5], type=pa.int64()),
            "dataset_to_index": pa.array([10], type=pa.int64()),
            "Serial_number": pa.array(["cell_chunks-serial"], type=pa.string()),
        }),
        ds / "meta" / "episodes" / "chunk-001" / "file-000.parquet",
    )

    ctx_new = reg.get(ds)
    assert ctx_new is not ctx_old
    assert len(ctx_new.iter_episode_parquet_files()) == 2
    assert [int(e["episode_index"]) for e in ctx_new.get_episodes()] == [0, 1]


def test_registry_returns_same_context_when_metadata_unchanged(tmp_path, monkeypatch):
    """Cache hits must keep returning the same context when metadata is stable."""
    from backend.core.config import settings
    from backend.datasets.services.dataset_registry import DatasetRegistry

    ds = _make_min_dataset(tmp_path, "cell_stable", length=10)
    monkeypatch.setattr(settings, "allowed_dataset_roots", [str(tmp_path)])

    reg = DatasetRegistry(max_size=4)
    ctx_a = reg.get(ds)
    ctx_b = reg.get(ds)
    assert ctx_a is ctx_b


def test_registry_preserves_file_lock_identity_across_reload(tmp_path, monkeypatch):
    """File locks taken before reload must survive automatic context replacement."""
    from backend.core.config import settings
    from backend.datasets.services.dataset_registry import DatasetRegistry

    ds = _make_min_dataset(tmp_path, "cell_locks", length=10)
    monkeypatch.setattr(settings, "allowed_dataset_roots", [str(tmp_path)])

    reg = DatasetRegistry(max_size=4)
    ctx_old = reg.get(ds)
    old_tasks_lock = ctx_old.get_file_lock("meta/tasks.parquet")
    old_default_lock = ctx_old.file_lock

    # Trigger reload by rewriting info.json.
    (ds / "meta" / "info.json").write_text(json.dumps({
        "fps": 30,
        "total_episodes": 1,
        "total_tasks": 1,
        "robot_type": "test_robot_v2",
        "features": {},
    }))
    _bump_mtime(ds / "meta" / "info.json")

    ctx_new = reg.get(ds)
    assert ctx_new is not ctx_old, "reload expected when info.json changes"
    assert ctx_new.get_file_lock("meta/tasks.parquet") is old_tasks_lock, (
        "per-path file lock identity must be preserved across reload"
    )
    assert ctx_new.file_lock is old_default_lock, (
        "the default tasks parquet lock must be preserved across reload"
    )


def test_registry_dataset_key_still_resolves_after_reload(tmp_path, monkeypatch):
    """get_by_key must still resolve the dataset path after an automatic reload."""
    from backend.core.config import settings
    from backend.datasets.services.dataset_registry import (
        DatasetRegistry,
        dataset_key_for,
    )

    ds = _make_min_dataset(tmp_path, "cell_key", length=10)
    monkeypatch.setattr(settings, "allowed_dataset_roots", [str(tmp_path)])

    reg = DatasetRegistry(max_size=4)
    key = dataset_key_for(ds)
    ctx_old = reg.get(ds)
    assert reg.get_by_key(key) is ctx_old

    (ds / "meta" / "info.json").write_text(json.dumps({
        "fps": 30,
        "total_episodes": 3,
        "total_tasks": 1,
        "robot_type": "test_robot",
        "features": {},
    }))
    _bump_mtime(ds / "meta" / "info.json")
    _rewrite_episode_parquet(ds, [0, 1, 2], length=10)
    _bump_mtime(ds / "meta" / "episodes" / "chunk-000" / "file-000.parquet")

    ctx_new = reg.get_by_key(key)
    assert ctx_new is not ctx_old
    assert ctx_new.get_info()["total_episodes"] == 3
    assert ctx_new.dataset_path == ds.resolve()


def test_registry_stable_load_retries_when_fingerprint_changes_during_load(tmp_path, monkeypatch):
    """Stable-load must retry once when before/after fingerprint differs."""
    from backend.core.config import settings
    from backend.datasets.services import dataset_registry as registry_mod

    ds = _make_min_dataset(tmp_path, "cell_retry", length=10)
    monkeypatch.setattr(settings, "allowed_dataset_roots", [str(tmp_path)])

    reg = registry_mod.DatasetRegistry(max_size=4)

    real_fingerprint = registry_mod._fingerprint_dataset
    sequence: list[object] = [
        ("v0",),  # first before
        ("v1",),  # first after — different => retry
    ]
    # Subsequent calls should return the real, stable fingerprint
    def fake_fingerprint(root):
        if sequence:
            return sequence.pop(0)
        return real_fingerprint(root)

    monkeypatch.setattr(registry_mod, "_fingerprint_dataset", fake_fingerprint)
    ctx = reg.get(ds)
    assert ctx.get_info()["total_episodes"] == 1
    # Two calls (before+after retry, then before+after stable) consumed the
    # priming sequence and then the real helper returned a steady fingerprint.
    assert sequence == []


def test_registry_stable_load_raises_runtime_error_on_persistent_change(tmp_path, monkeypatch):
    """Stable-load must raise RuntimeError when the fingerprint never stabilizes."""
    from backend.core.config import settings
    from backend.datasets.services import dataset_registry as registry_mod

    ds = _make_min_dataset(tmp_path, "cell_unstable", length=10)
    monkeypatch.setattr(settings, "allowed_dataset_roots", [str(tmp_path)])

    reg = registry_mod.DatasetRegistry(max_size=4)

    counter = {"n": 0}

    def always_changing(_root):
        counter["n"] += 1
        return (f"v{counter['n']}",)

    monkeypatch.setattr(registry_mod, "_fingerprint_dataset", always_changing)
    with pytest.raises(RuntimeError) as excinfo:
        reg.get(ds)
    assert "metadata changed while loading" in str(excinfo.value).lower()
