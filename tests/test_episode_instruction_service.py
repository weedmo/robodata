"""Regression tests for transactional episode instruction edits."""
from __future__ import annotations

import json

import pyarrow as pa
import pyarrow.parquet as pq

from backend.datasets.services import episode_instruction_service as service


def _dataset(tmp_path):
    root = tmp_path / "dataset"
    (root / "meta/episodes/chunk-000").mkdir(parents=True)
    (root / "data/chunk-000").mkdir(parents=True)
    (root / "meta").joinpath("info.json").write_text(json.dumps({"total_tasks": 1}))
    pq.write_table(pa.table({"task_index": [0], "task": ["pick"]}), root / "meta/tasks.parquet")
    pq.write_table(pa.table({"episode_index": [0, 1], "task_index": [0, 0]}), root / "meta/episodes/chunk-000/file-000.parquet")
    pq.write_table(pa.table({"episode_index": [0, 0, 1], "task_index": [0, 0, 0]}), root / "data/chunk-000/file-000.parquet")
    return root


def test_episode_edit_creates_task_and_updates_frames(tmp_path):
    root = _dataset(tmp_path)
    preview = service.preview(root, 0, "place", "episode")
    service.commit(root, 0, "place", "episode", preview["fingerprint"], False)
    assert pq.read_table(root / "meta/tasks.parquet").column("task").to_pylist() == ["pick", "place"]
    assert pq.read_table(root / "meta/episodes/chunk-000/file-000.parquet").column("task_index").to_pylist() == [1, 0]
    assert pq.read_table(root / "data/chunk-000/file-000.parquet").column("task_index").to_pylist() == [1, 1, 0]


def test_stale_fingerprint_does_not_mutate(tmp_path):
    root = _dataset(tmp_path)
    preview = service.preview(root, 0, "place", "episode")
    try:
        service.commit(root, 0, "place", "episode", "stale", False)
    except service.InstructionConflictError as exc:
        assert exc.code == "instruction_preview_stale"
    else:
        raise AssertionError("expected stale preview conflict")
    assert pq.read_table(root / "meta/tasks.parquet").num_rows == 1


def test_shared_edit_renames_matching_task_inside_multi_task_lists(tmp_path):
    root = _dataset(tmp_path)
    pq.write_table(pa.table({"task_index": [0, 1], "task": ["pick", "place"]}), root / "meta/tasks.parquet")
    pq.write_table(pa.table({"episode_index": [0, 1], "tasks": [["pick", "place"], ["pick"]]}), root / "meta/episodes/chunk-000/file-000.parquet")
    preview = service.preview(root, 0, "grasp", "shared")
    assert preview["affected_episode_count"] == 2
    service.commit(root, 0, "grasp", "shared", preview["fingerprint"], True)
    assert pq.read_table(root / "meta/episodes/chunk-000/file-000.parquet").column("tasks").to_pylist() == [["grasp", "place"], ["grasp"]]


def test_recovery_restores_backup_when_source_is_missing(tmp_path):
    root = _dataset(tmp_path)
    backup = root.parent / ".dataset.instruction-backup-test"
    staging = root.parent / ".dataset.instruction-staging-test"
    root.rename(backup)
    service._manifest_path(root).write_text(json.dumps({"backup": str(backup), "staging": str(staging)}))
    service.recover(root)
    assert root.exists()
    assert not service._manifest_path(root).exists()


def test_recovery_preserves_invalid_missing_backup_manifest(tmp_path):
    root = _dataset(tmp_path)
    manifest = service._manifest_path(root)
    manifest.write_text(json.dumps({"backup": str(root.parent / "missing"), "staging": str(root.parent / "staging")}))
    try:
        service.recover(root)
    except service.InstructionConflictError as exc:
        assert exc.code == "instruction_recovery_required"
    else:
        raise AssertionError("expected recovery conflict")
    assert manifest.exists()


def test_recovery_preserves_installed_dataset_after_promotion_crash(tmp_path):
    root = _dataset(tmp_path)
    backup = root.parent / ".dataset.instruction-backup-test"
    staging = root.parent / ".dataset.instruction-staging-test"
    backup.mkdir()
    (backup / "old").write_text("old")
    (root / "new").write_text("new")
    service._manifest_path(root).write_text(json.dumps({"backup": str(backup), "staging": str(staging), "phase": "moved"}))
    service.recover(root)
    assert (root / "new").read_text() == "new"
    assert not backup.exists()
    assert not service._manifest_path(root).exists()


def test_episode_edit_updates_referenced_rows_in_multiple_data_chunks(tmp_path):
    root = _dataset(tmp_path)
    (root / "data/chunk-001").mkdir()
    pq.write_table(pa.table({"episode_index": [0, 1], "task_index": [0, 0]}), root / "data/chunk-001/file-000.parquet")
    preview = service.preview(root, 0, "place", "episode")
    service.commit(root, 0, "place", "episode", preview["fingerprint"], False)
    assert pq.read_table(root / "data/chunk-001/file-000.parquet").column("task_index").to_pylist() == [1, 0]


def test_last_reference_reassignment_removes_orphan_task_and_keeps_indexes_dense(tmp_path):
    root = _dataset(tmp_path)
    pq.write_table(pa.table({"episode_index": [0], "task_index": [0]}), root / "meta/episodes/chunk-000/file-000.parquet")
    pq.write_table(pa.table({"episode_index": [0], "task_index": [0]}), root / "data/chunk-000/file-000.parquet")
    preview = service.preview(root, 0, "place", "episode")
    service.commit(root, 0, "place", "episode", preview["fingerprint"], False)
    assert pq.read_table(root / "meta/tasks.parquet").column("task_index").to_pylist() == [0]
    assert pq.read_table(root / "meta/tasks.parquet").column("task").to_pylist() == ["place"]
    assert pq.read_table(root / "meta/episodes/chunk-000/file-000.parquet").column("task_index").to_pylist() == [0]


def test_list_task_reassignment_compacts_orphan_and_remaps_frames(tmp_path):
    root = _dataset(tmp_path)
    pq.write_table(pa.table({"task_index": [0, 1], "task": ["pick", "place"]}), root / "meta/tasks.parquet")
    pq.write_table(pa.table({"episode_index": [0, 1], "tasks": [["pick"], ["place"]]}), root / "meta/episodes/chunk-000/file-000.parquet")
    pq.write_table(pa.table({"episode_index": [0, 1], "task_index": [0, 1]}), root / "data/chunk-000/file-000.parquet")
    preview = service.preview(root, 0, "place", "episode")
    service.commit(root, 0, "place", "episode", preview["fingerprint"], False)
    assert pq.read_table(root / "meta/tasks.parquet").column("task").to_pylist() == ["place"]
    assert pq.read_table(root / "data/chunk-000/file-000.parquet").column("task_index").to_pylist() == [0, 0]


def test_mutation_failure_restores_original_dataset(tmp_path, monkeypatch):
    root = _dataset(tmp_path)
    original_tasks = pq.read_table(root / "meta/tasks.parquet").to_pydict()
    preview = service.preview(root, 0, "place", "episode")

    def fail_mutation(_root, _info):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(service, "_mutate", fail_mutation)
    try:
        service.commit(root, 0, "place", "episode", preview["fingerprint"], False)
    except OSError:
        pass
    else:
        raise AssertionError("expected mutation failure")
    assert pq.read_table(root / "meta/tasks.parquet").to_pydict() == original_tasks
    assert not service._manifest_path(root).exists()
