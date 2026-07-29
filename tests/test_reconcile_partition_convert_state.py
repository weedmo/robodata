from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import scripts.reconcile_partition_convert_state as reconcile_module
from scripts.partition_raw_by_contract import apply_partition
from scripts.reconcile_partition_convert_state import (
    _install_bytes,
    _state_lock,
    _write_new_regular,
    reconcile_partition_state,
    rollback_partition_state,
)


KEEP_CONTRACT = {"canonical_schema": {"version": 1}, "fixture": "keep"}
MOVE_CONTRACT = {"canonical_schema": {"version": 1}, "fixture": "move"}


def _digest(contract: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            contract,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


KEEP_DIGEST = _digest(KEEP_CONTRACT)
MOVE_DIGEST = _digest(MOVE_CONTRACT)


def _recording(task: Path, serial: str) -> None:
    recording = task / serial
    recording.mkdir(parents=True)
    (recording / "metacard.json").write_text(
        json.dumps({"serial": serial}),
        encoding="utf-8",
    )
    (recording / f"{serial}_0.mcap").write_bytes(serial.encode())


def _manifest(path: Path) -> Path:
    payload = {
        "version": 1,
        "contract_version": 1,
        "digest_algorithm": "sha256",
        "task": "task",
        "target_fps": 24,
        "invalid": [],
        "invariants": {
            "partition_intersections_empty": True,
            "raw_mutation_performed": False,
            "recorded_exactly_once": True,
            "resolved_invalid_intersection_empty": True,
        },
        "partitions": [
            {
                "contract": KEEP_CONTRACT,
                "digest": KEEP_DIGEST,
                "serials": ["keep-a"],
            },
            {
                "contract": MOVE_CONTRACT,
                "digest": MOVE_DIGEST,
                "serials": ["move-b"],
            },
        ],
        "recordings": [
            {"digest": KEEP_DIGEST, "serial": "keep-a", "status": "resolved"},
            {"digest": MOVE_DIGEST, "serial": "move-b", "status": "resolved"},
        ],
        "summary": {
            "invalid": 0,
            "partition_count": 2,
            "resolved": 2,
            "total": 2,
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    return path


def _setup(tmp_path: Path) -> tuple[Path, Path, Path, str, str]:
    raw_root = tmp_path / "raw"
    source_task = "cell004/task"
    destination_task = "cell004/task__move"
    source = raw_root / source_task
    _recording(source, "keep-a")
    _recording(source, "move-b")
    private = tmp_path / "private"
    private.mkdir()
    private.chmod(0o700)
    journal = private / "partition.json"
    apply_partition(
        source,
        _manifest(tmp_path / "manifest.json"),
        journal,
        KEEP_DIGEST,
        {MOVE_DIGEST: raw_root / destination_task},
    )
    lerobot_root = tmp_path / "lerobot"
    lerobot_root.mkdir()
    state = {
        source_task: {
            "converted_count": 1,
            "failed_serials": ["move-b", "keep-failure"],
            "last_serial": "keep-a",
            "last_updated": "before",
            "transient_failed": {
                "move-b": {"last_error": "schema mismatch"},
                "keep-retry": {"last_error": "stale handle"},
            },
        },
        destination_task: {
            "converted_count": 99,
            "failed_serials": ["move-b"],
            "last_serial": "move-b",
            "last_updated": "bad-copy",
            "transient_failed": {
                "move-b": {"last_error": "schema mismatch"},
            },
        },
        "cell999/unrelated": {
            "converted_count": 7,
            "failed_serials": ["other"],
        },
    }
    state_path = lerobot_root / "convert_state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    state_path.chmod(0o640)
    return raw_root, lerobot_root, journal, source_task, destination_task


def _apply(
    raw_root: Path,
    lerobot_root: Path,
    journal: Path,
    source_task: str,
    destination_task: str,
) -> dict:
    return reconcile_partition_state(
        raw_root=raw_root,
        lerobot_root=lerobot_root,
        journal_path=journal,
        source_task=source_task,
        destination_tasks={MOVE_DIGEST: destination_task},
    )


def _add_named_partition(
    tmp_path: Path,
    raw_root: Path,
    name: str,
) -> tuple[Path, str, str, str, str]:
    source_task = f"cell004/{name}"
    destination_task = f"cell004/{name}__move"
    keep_serial = f"{name}-keep"
    move_serial = f"{name}-move"
    source = raw_root / source_task
    _recording(source, keep_serial)
    _recording(source, move_serial)
    manifest = _manifest(tmp_path / f"{name}-manifest.json")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["task"] = source_task
    payload["partitions"][0]["serials"] = [keep_serial]
    payload["partitions"][1]["serials"] = [move_serial]
    payload["recordings"] = [
        {
            "digest": KEEP_DIGEST,
            "serial": keep_serial,
            "status": "resolved",
        },
        {
            "digest": MOVE_DIGEST,
            "serial": move_serial,
            "status": "resolved",
        },
    ]
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    manifest.chmod(0o600)
    private = tmp_path / "composable-private"
    private.mkdir(exist_ok=True)
    private.chmod(0o700)
    journal = private / f"{name}-partition.json"
    apply_partition(
        source,
        manifest,
        journal,
        KEEP_DIGEST,
        {MOVE_DIGEST: raw_root / destination_task},
    )
    return (
        journal,
        source_task,
        destination_task,
        keep_serial,
        move_serial,
    )


def _composable_setup(tmp_path: Path):
    raw_root = tmp_path / "raw"
    lerobot_root = tmp_path / "lerobot"
    lerobot_root.mkdir()
    partitions = {
        name: _add_named_partition(tmp_path, raw_root, name)
        for name in ("task_a", "task_b")
    }
    state: dict[str, dict] = {
        "cell999/unrelated": {
            "converted_count": 7,
            "failed_serials": ["unrelated-failure"],
            "nested": {"preserve": ["exact", 1]},
        }
    }
    for journal, source, destination, keep_serial, move_serial in (
        partitions.values()
    ):
        del journal
        state[source] = {
            "converted_count": 128,
            "failed_serials": [move_serial, f"{keep_serial}-failure"],
            "last_serial": keep_serial,
            "last_updated": "stale-pre-transform",
        }
        state[destination] = {
            "converted_count": 0,
            "failed_serials": [move_serial],
            "last_serial": "",
            "last_updated": "destination-pre-transform",
        }
    state_path = lerobot_root / "convert_state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    state_path.chmod(0o640)
    return raw_root, lerobot_root, partitions, state


def _apply_named(
    raw_root: Path,
    lerobot_root: Path,
    partition: tuple[Path, str, str, str, str],
):
    journal, source, destination, _keep, _move = partition
    return reconcile_partition_state(
        raw_root=raw_root,
        lerobot_root=lerobot_root,
        journal_path=journal,
        source_task=source,
        destination_tasks={MOVE_DIGEST: destination},
    )


def test_reconcile_committed_partition_clears_moved_failures_and_backs_up(
    tmp_path: Path,
):
    raw_root, lerobot_root, journal, source_task, destination_task = _setup(
        tmp_path
    )
    state_path = lerobot_root / "convert_state.json"
    original = state_path.read_bytes()
    original_info = state_path.stat()

    result = _apply(
        raw_root,
        lerobot_root,
        journal,
        source_task,
        destination_task,
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert result["status"] == "reconciled"
    assert state[source_task]["failed_serials"] == ["keep-failure"]
    assert set(state[source_task]["transient_failed"]) == {"keep-retry"}
    assert state[source_task]["converted_count"] == 1
    assert state[destination_task]["failed_serials"] == ["move-b"]
    assert set(state[destination_task]["transient_failed"]) == {"move-b"}
    assert state[destination_task]["converted_count"] == 0
    assert state[destination_task]["last_serial"] == ""
    assert state["cell999/unrelated"] == {
        "converted_count": 7,
        "failed_serials": ["other"],
    }
    backup = Path(result["backup"])
    assert backup.read_bytes() == original
    assert stat.S_IMODE(backup.stat().st_mode) == stat.S_IMODE(
        original_info.st_mode
    )
    assert (backup.stat().st_uid, backup.stat().st_gid) == (
        original_info.st_uid,
        original_info.st_gid,
    )
    assert stat.S_IMODE(state_path.stat().st_mode) == stat.S_IMODE(
        original_info.st_mode
    )


def test_reconcile_uses_canonical_state_owner_for_journal_authority(
    tmp_path: Path,
    monkeypatch,
):
    values = _setup(tmp_path)
    state_info = (values[1] / "convert_state.json").stat()
    original_read_journal = reconcile_module._read_journal
    captured: list[tuple[int, int]] = []

    def capture_journal_owner(path, *, expected_uid, expected_gid):
        captured.append((expected_uid, expected_gid))
        return original_read_journal(
            path,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
        )

    monkeypatch.setattr(
        reconcile_module,
        "_read_journal",
        capture_journal_owner,
    )

    result = _apply(*values)

    assert result["status"] == "reconciled"
    assert captured == [(state_info.st_uid, state_info.st_gid)]


def test_reconcile_rejects_journal_owner_that_differs_from_state_authority(
    tmp_path: Path,
    monkeypatch,
):
    values = _setup(tmp_path)
    state_path = values[1] / "convert_state.json"
    original_state = state_path.read_bytes()
    original_read_regular = reconcile_module._read_regular_at

    def present_foreign_state_owner(parent_fd: int, name: str):
        payload, info = original_read_regular(parent_fd, name)
        if name != "convert_state.json":
            return payload, info
        fields = list(info)
        fields[4] = info.st_uid + 1
        return payload, os.stat_result(fields)

    monkeypatch.setattr(
        reconcile_module,
        "_read_regular_at",
        present_foreign_state_owner,
    )

    with pytest.raises(PermissionError, match="owner"):
        _apply(*values)

    assert state_path.read_bytes() == original_state
    assert list(values[1].glob("*.bak")) == []


@pytest.mark.parametrize(
    ("first_name", "second_name"),
    [("task_a", "task_b"), ("task_b", "task_a")],
)
def test_reconcile_replay_repairs_only_stale_scope_after_other_partition(
    tmp_path: Path,
    first_name: str,
    second_name: str,
):
    raw_root, lerobot_root, partitions, initial = _composable_setup(tmp_path)
    state_path = lerobot_root / "convert_state.json"
    _apply_named(raw_root, lerobot_root, partitions[first_name])
    _apply_named(raw_root, lerobot_root, partitions[second_name])
    fully_reconciled = json.loads(state_path.read_text(encoding="utf-8"))

    first_source = partitions[first_name][1]
    first_destination = partitions[first_name][2]
    second_source = partitions[second_name][1]
    second_destination = partitions[second_name][2]
    stale = copy.deepcopy(fully_reconciled)
    hybrid_source = copy.deepcopy(fully_reconciled[first_source])
    hybrid_source["failed_serials"] = copy.deepcopy(
        initial[first_source]["failed_serials"]
    )
    assert (
        hybrid_source["last_updated"]
        == fully_reconciled[first_source]["last_updated"]
    )
    stale[first_source] = hybrid_source
    stale["cell999/unrelated"]["nested"]["preserve"].append("later-change")
    state_path.write_text(json.dumps(stale), encoding="utf-8")
    state_path.chmod(0o640)
    second_scope_before = {
        second_source: copy.deepcopy(stale[second_source]),
        second_destination: copy.deepcopy(stale[second_destination]),
    }
    unrelated_before = copy.deepcopy(stale["cell999/unrelated"])

    result = _apply_named(
        raw_root,
        lerobot_root,
        partitions[first_name],
    )

    repaired = json.loads(state_path.read_text(encoding="utf-8"))
    assert result["status"] == "reconciled"
    assert repaired[first_source] == fully_reconciled[first_source]
    assert repaired[first_destination] == fully_reconciled[first_destination]
    assert repaired[second_source] == second_scope_before[second_source]
    assert (
        repaired[second_destination]
        == second_scope_before[second_destination]
    )
    assert repaired["cell999/unrelated"] == unrelated_before


def test_reconcile_replay_rejects_foreign_in_scope_conflict(
    tmp_path: Path,
):
    raw_root, lerobot_root, partitions, _initial = _composable_setup(tmp_path)
    state_path = lerobot_root / "convert_state.json"
    _apply_named(raw_root, lerobot_root, partitions["task_a"])
    _apply_named(raw_root, lerobot_root, partitions["task_b"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    source = partitions["task_a"][1]
    state[source]["converted_count"] = 9999
    state[source]["last_updated"] = "foreign-conflict"
    state[source]["foreign_field"] = "not-authoritative"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    state_path.chmod(0o640)
    before = state_path.read_bytes()

    with pytest.raises(RuntimeError, match="in-scope conflict"):
        _apply_named(raw_root, lerobot_root, partitions["task_a"])

    assert state_path.read_bytes() == before


def test_scoped_rollback_preserves_later_unrelated_partition(
    tmp_path: Path,
):
    raw_root, lerobot_root, partitions, initial = _composable_setup(tmp_path)
    state_path = lerobot_root / "convert_state.json"
    _apply_named(raw_root, lerobot_root, partitions["task_a"])
    _apply_named(raw_root, lerobot_root, partitions["task_b"])
    before = json.loads(state_path.read_text(encoding="utf-8"))
    b_source = partitions["task_b"][1]
    b_destination = partitions["task_b"][2]
    journal, a_source, a_destination, _keep, _move = partitions["task_a"]

    result = rollback_partition_state(
        raw_root=raw_root,
        lerobot_root=lerobot_root,
        journal_path=journal,
        source_task=a_source,
        destination_tasks={MOVE_DIGEST: a_destination},
    )

    rolled_back = json.loads(state_path.read_text(encoding="utf-8"))
    assert result["status"] == "rolled_back"
    assert rolled_back[a_source] == initial[a_source]
    assert rolled_back[a_destination] == initial[a_destination]
    assert rolled_back[b_source] == before[b_source]
    assert rolled_back[b_destination] == before[b_destination]
    assert rolled_back["cell999/unrelated"] == before["cell999/unrelated"]


def test_reconcile_is_idempotent_and_does_not_replace_backup(tmp_path: Path):
    values = _setup(tmp_path)
    first = _apply(*values)
    state_path = values[1] / "convert_state.json"
    backup = Path(first["backup"])
    first_state = state_path.read_bytes()
    backup_identity = (backup.stat().st_dev, backup.stat().st_ino)

    second = _apply(*values)

    assert second["status"] == "already_reconciled"
    assert state_path.read_bytes() == first_state
    assert (backup.stat().st_dev, backup.stat().st_ino) == backup_identity


def test_reconcile_grounds_destination_count_in_existing_output(tmp_path: Path):
    raw_root, lerobot_root, journal, source_task, destination_task = _setup(
        tmp_path
    )
    episodes = (
        lerobot_root
        / destination_task
        / "meta"
        / "episodes"
        / "chunk-000"
    )
    episodes.mkdir(parents=True)
    pq.write_table(
        pa.table({"Serial_number": ["move-b"]}),
        episodes / "file-000.parquet",
    )

    result = _apply(
        raw_root,
        lerobot_root,
        journal,
        source_task,
        destination_task,
    )

    state = json.loads(
        (lerobot_root / "convert_state.json").read_text(encoding="utf-8")
    )
    assert result["destination_counts"] == {destination_task: 1}
    assert state[destination_task]["converted_count"] == 1
    assert state[destination_task]["last_serial"] == "move-b"
    assert state[destination_task]["failed_serials"] == []
    assert "transient_failed" not in state[destination_task]


def test_reconcile_grounds_source_count_in_existing_output(tmp_path: Path):
    raw_root, lerobot_root, journal, source_task, destination_task = _setup(
        tmp_path
    )
    episodes = (
        lerobot_root / source_task / "meta" / "episodes" / "chunk-000"
    )
    episodes.mkdir(parents=True)
    pq.write_table(
        pa.table({"Serial_number": ["keep-a"]}),
        episodes / "file-000.parquet",
    )
    state_path = lerobot_root / "convert_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state[source_task]["converted_count"] = 99
    state[source_task]["last_serial"] = "foreign"
    state[source_task]["failed_serials"].append("keep-a")
    state[source_task]["transient_failed"]["keep-a"] = {
        "last_error": "stale state"
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    state_path.chmod(0o640)

    result = _apply(
        raw_root,
        lerobot_root,
        journal,
        source_task,
        destination_task,
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert result["source_count"] == 1
    assert state[source_task]["converted_count"] == 1
    assert state[source_task]["last_serial"] == "keep-a"
    assert state[source_task]["failed_serials"] == ["keep-failure"]
    assert set(state[source_task]["transient_failed"]) == {"keep-retry"}


def test_rollback_restores_backup_and_is_idempotent(tmp_path: Path):
    raw_root, lerobot_root, journal, source_task, destination_task = _setup(
        tmp_path
    )
    original = (lerobot_root / "convert_state.json").read_bytes()
    applied = _apply(
        raw_root,
        lerobot_root,
        journal,
        source_task,
        destination_task,
    )

    first = rollback_partition_state(
        raw_root=raw_root,
        lerobot_root=lerobot_root,
        journal_path=journal,
        source_task=source_task,
        destination_tasks={MOVE_DIGEST: destination_task},
    )
    second = rollback_partition_state(
        raw_root=raw_root,
        lerobot_root=lerobot_root,
        journal_path=journal,
        source_task=source_task,
        destination_tasks={MOVE_DIGEST: destination_task},
    )

    assert first["status"] == "rolled_back"
    assert second["status"] == "already_rolled_back"
    assert (lerobot_root / "convert_state.json").read_bytes() == original
    assert Path(applied["backup"]).read_bytes() == original


def test_reconcile_rejects_wrong_destination_without_state_mutation(
    tmp_path: Path,
):
    raw_root, lerobot_root, journal, source_task, destination_task = _setup(
        tmp_path
    )
    state_path = lerobot_root / "convert_state.json"
    before = state_path.read_bytes()

    with pytest.raises(RuntimeError, match="exact source/destinations"):
        reconcile_partition_state(
            raw_root=raw_root,
            lerobot_root=lerobot_root,
            journal_path=journal,
            source_task=source_task,
            destination_tasks={
                MOVE_DIGEST: f"{destination_task}-foreign",
            },
        )

    assert state_path.read_bytes() == before
    assert list(lerobot_root.glob("*.bak")) == []


def test_reconcile_rejects_symlink_state_without_following(
    tmp_path: Path,
):
    raw_root, lerobot_root, journal, source_task, destination_task = _setup(
        tmp_path
    )
    state_path = lerobot_root / "convert_state.json"
    outside = tmp_path / "outside-state.json"
    state_path.rename(outside)
    state_path.symlink_to(outside)
    before = outside.read_bytes()

    with pytest.raises(OSError):
        _apply(
            raw_root,
            lerobot_root,
            journal,
            source_task,
            destination_task,
        )

    assert state_path.is_symlink()
    assert outside.read_bytes() == before
    assert list(lerobot_root.glob("*.bak")) == []


def test_reconcile_rejects_uncommitted_journal_without_state_mutation(
    tmp_path: Path,
):
    raw_root, lerobot_root, journal, source_task, destination_task = _setup(
        tmp_path
    )
    state_path = lerobot_root / "convert_state.json"
    before = state_path.read_bytes()
    state_log = journal.with_name(f".{journal.name}.state-log")
    log_lines = state_log.read_bytes().splitlines(keepends=True)
    state_log.write_bytes(b"".join(log_lines[:-1]))

    with pytest.raises(RuntimeError):
        _apply(
            raw_root,
            lerobot_root,
            journal,
            source_task,
            destination_task,
        )

    assert state_path.read_bytes() == before
    assert list(lerobot_root.glob("*.bak")) == []


def test_reconcile_rejects_foreign_destination_failure(tmp_path: Path):
    raw_root, lerobot_root, journal, source_task, destination_task = _setup(
        tmp_path
    )
    state_path = lerobot_root / "convert_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state[destination_task]["failed_serials"].append("foreign")
    state_path.write_text(json.dumps(state), encoding="utf-8")
    state_path.chmod(0o640)
    before = state_path.read_bytes()

    with pytest.raises(ValueError, match="outside its raw partition"):
        _apply(
            raw_root,
            lerobot_root,
            journal,
            source_task,
            destination_task,
        )

    assert state_path.read_bytes() == before
    assert list(lerobot_root.glob("*.bak")) == []


def test_write_new_regular_cleans_only_artifact_created_by_current_call(
    tmp_path: Path,
    monkeypatch,
):
    template = tmp_path / "template"
    template.write_bytes(b"template")
    template.chmod(0o640)
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    original_write = reconcile_module.os.write
    original_fstat = reconcile_module.os.fstat

    def fail_after_partial_write(descriptor: int, payload: bytes) -> int:
        original_write(descriptor, payload[:1])
        raise OSError("injected write failure")

    def fail_fstat(_descriptor: int):
        raise OSError("injected fstat failure")

    monkeypatch.setattr(reconcile_module.os, "write", fail_after_partial_write)
    monkeypatch.setattr(reconcile_module.os, "fstat", fail_fstat)
    try:
        with pytest.raises(OSError, match="injected write failure"):
            _write_new_regular(
                parent_fd,
                "new-state",
                b"replacement",
                template=template.stat(),
            )
        assert not (tmp_path / "new-state").exists()

        authority = tmp_path / "authority"
        authority.write_bytes(b"do-not-delete")
        with pytest.raises(FileExistsError):
            _write_new_regular(
                parent_fd,
                "authority",
                b"replacement",
                template=template.stat(),
            )
        assert authority.read_bytes() == b"do-not-delete"
    finally:
        os.close(parent_fd)
        monkeypatch.setattr(reconcile_module.os, "fstat", original_fstat)


def test_install_bytes_cleans_temporary_after_replace_failure(
    tmp_path: Path,
    monkeypatch,
):
    state = tmp_path / "convert_state.json"
    state.write_bytes(b"authority")
    state.chmod(0o640)
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)

    def fail_replace(*args, **kwargs):
        del args, kwargs
        raise OSError("injected replace failure")

    monkeypatch.setattr(reconcile_module.os, "replace", fail_replace)
    try:
        with pytest.raises(OSError, match="injected replace failure"):
            _install_bytes(
                parent_fd,
                b"replacement",
                template=state.stat(),
                token="test",
            )
    finally:
        os.close(parent_fd)

    assert state.read_bytes() == b"authority"
    assert list(tmp_path.glob(".convert_state.json.*.tmp")) == []

    monkeypatch.setattr(reconcile_module.os, "getpid", lambda: 123)
    monkeypatch.setattr(
        reconcile_module.secrets,
        "token_hex",
        lambda _size: "deadbeef",
    )
    foreign_temporary = (
        tmp_path / ".convert_state.json.test.123.deadbeef.tmp"
    )
    foreign_temporary.write_bytes(b"foreign-authority")
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(FileExistsError):
            _install_bytes(
                parent_fd,
                b"replacement",
                template=state.stat(),
                token="test",
            )
    finally:
        os.close(parent_fd)
    assert foreign_temporary.read_bytes() == b"foreign-authority"


def test_backup_parent_fsync_failure_leaves_cleanly_replayable_state(
    tmp_path: Path,
    monkeypatch,
):
    values = _setup(tmp_path)
    lerobot_root = values[1]
    state_path = lerobot_root / "convert_state.json"
    original_state = state_path.read_bytes()
    original_fsync = reconcile_module.os.fsync
    injected = False

    def fail_first_lerobot_directory_fsync(descriptor: int) -> None:
        nonlocal injected
        target = os.readlink(f"/proc/self/fd/{descriptor}")
        if target == str(lerobot_root) and not injected:
            injected = True
            raise OSError("injected parent fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(
        reconcile_module.os,
        "fsync",
        fail_first_lerobot_directory_fsync,
    )
    with pytest.raises(OSError, match="injected parent fsync failure"):
        _apply(*values)

    assert injected is True
    assert state_path.read_bytes() == original_state
    assert list(lerobot_root.glob("*.bak")) == []
    assert list(lerobot_root.glob(".convert_state.json.*.tmp")) == []

    monkeypatch.setattr(reconcile_module.os, "fsync", original_fsync)
    replay = _apply(*values)

    assert replay["status"] == "reconciled"
    assert Path(replay["backup"]).read_bytes() == original_state


def test_state_lock_corrects_nfs_created_wrong_initial_mode(
    tmp_path: Path,
    monkeypatch,
):
    state = tmp_path / "convert_state.json"
    state.write_text("{}", encoding="utf-8")
    state.chmod(0o640)
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    original_open = reconcile_module.os.open
    lock_name = ".convert_state.json.partition-reconcile.lock"

    def create_with_nfs_mode(name, flags, mode=0o777, *, dir_fd=None):
        descriptor = original_open(
            name,
            flags,
            mode,
            dir_fd=dir_fd,
        )
        if name == lock_name and flags & os.O_EXCL:
            os.fchmod(descriptor, 0o777)
        return descriptor

    monkeypatch.setattr(reconcile_module.os, "open", create_with_nfs_mode)
    try:
        with _state_lock(parent_fd, state.stat()):
            assert stat.S_IMODE((tmp_path / lock_name).stat().st_mode) == 0o600
    finally:
        os.close(parent_fd)

    assert stat.S_IMODE((tmp_path / lock_name).stat().st_mode) == 0o600


def test_state_lock_fchmod_failure_cleans_created_inode(
    tmp_path: Path,
    monkeypatch,
):
    state = tmp_path / "convert_state.json"
    state.write_text("{}", encoding="utf-8")
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    lock = tmp_path / ".convert_state.json.partition-reconcile.lock"

    def fail_fchmod(_descriptor: int, _mode: int) -> None:
        raise OSError("injected lock fchmod failure")

    monkeypatch.setattr(reconcile_module.os, "fchmod", fail_fchmod)
    try:
        with pytest.raises(OSError, match="injected lock fchmod failure"):
            with _state_lock(parent_fd, state.stat()):
                pass
    finally:
        os.close(parent_fd)

    assert not lock.exists()


def test_state_lock_failure_does_not_unlink_replacement(
    tmp_path: Path,
    monkeypatch,
):
    state = tmp_path / "convert_state.json"
    state.write_text("{}", encoding="utf-8")
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    lock = tmp_path / ".convert_state.json.partition-reconcile.lock"
    displaced = tmp_path / ".created-lock-displaced"

    def replace_then_fail(_descriptor: int, _mode: int) -> None:
        lock.rename(displaced)
        lock.write_bytes(b"foreign-authority")
        raise OSError("injected lock replacement race")

    monkeypatch.setattr(
        reconcile_module.os,
        "fchmod",
        replace_then_fail,
    )
    try:
        with pytest.raises(OSError, match="injected lock replacement race"):
            with _state_lock(parent_fd, state.stat()):
                pass
    finally:
        os.close(parent_fd)

    assert lock.read_bytes() == b"foreign-authority"
    assert displaced.exists()


def test_state_lock_rejects_unsafe_preexisting_mode_without_chmod(
    tmp_path: Path,
):
    state = tmp_path / "convert_state.json"
    state.write_text("{}", encoding="utf-8")
    lock = tmp_path / ".convert_state.json.partition-reconcile.lock"
    lock.write_bytes(b"")
    lock.chmod(0o777)
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(PermissionError, match="mode is unsafe"):
            with _state_lock(parent_fd, state.stat()):
                pass
    finally:
        os.close(parent_fd)

    assert stat.S_IMODE(lock.stat().st_mode) == 0o777
