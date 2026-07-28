"""Behavioral tests for preservation-first conversion recovery."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import stat
import threading
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import backend.converter.recovery_service as recovery_service
from backend.converter.recovery_service import (
    RecoveryError,
    RecoveryService,
    recovery_blockers,
)


CELL_TASK = "cell001/task_a"
RAW_SERIALS = ["20260727_010101", "20260727_010102"]


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _serial_digest(serials: list[str]) -> str:
    payload = "".join(f"{serial}\n" for serial in sorted(serials)).encode()
    return hashlib.sha256(payload).hexdigest()


def _write_dataset(
    path: Path,
    *,
    serials: list[str],
    validation_status: str,
    identity: str,
) -> None:
    path.mkdir(parents=True)
    _write_json(
        path / "validation-result.json",
        {"status": validation_status, "identity": identity},
    )
    if validation_status == "passed":
        episode_path = path / "meta" / "episodes" / "chunk-000" / "file-000.parquet"
        episode_path.parent.mkdir(parents=True)
        pq.write_table(
            pa.table({"Serial_number": pa.array(serials, type=pa.string())}),
            episode_path,
        )


def _validation_runner(path: Path) -> dict:
    return json.loads((path / "validation-result.json").read_text(encoding="utf-8"))


@pytest.fixture
def recovery_layout(tmp_path: Path) -> dict[str, Path]:
    raw_root = tmp_path / "raw"
    lerobot_root = tmp_path / "lerobot"
    raw_task = raw_root / CELL_TASK
    output_parent = lerobot_root / "cell001"
    output = output_parent / "task_a"
    for serial in RAW_SERIALS:
        (raw_task / serial).mkdir(parents=True)
    output_parent.mkdir(parents=True)
    _write_json(
        lerobot_root / "convert_state.json",
        {
            CELL_TASK: {
                "converted_count": 99,
                "failed_serials": list(RAW_SERIALS),
                "transient_failed": {serial: {"attempts": 1} for serial in RAW_SERIALS},
            },
            "cell999/untouched": {"converted_count": 7, "sentinel": True},
        },
    )
    return {
        "raw_root": raw_root,
        "lerobot_root": lerobot_root,
        "raw_task": raw_task,
        "output_parent": output_parent,
        "output": output,
        "state": lerobot_root / "convert_state.json",
    }


def _service(layout: dict[str, Path], **kwargs) -> RecoveryService:
    return RecoveryService(
        layout["raw_root"],
        layout["lerobot_root"],
        validation_runner=_validation_runner,
        **kwargs,
    )


def _finalization_marker(layout: dict[str, Path], *, phase: str = "armed") -> Path:
    marker = layout["output_parent"] / ".task_a.finalization-pending.json"
    _write_json(
        marker,
        {
            "version": 1,
            "cell_task": CELL_TASK,
            "output_root": str(layout["output"]),
            "phase": phase,
            "rebuild_token": "token-1",
            "build_fingerprint": "build-1",
            "raw_snapshot_before": _serial_digest(RAW_SERIALS),
        },
    )
    return marker


def _rebuild_marker(
    layout: dict[str, Path],
    archive: Path,
    *,
    phase: str,
) -> Path:
    marker = layout["output_parent"] / ".task_a.rebuild-journal.json"
    _write_json(
        marker,
        {
            "version": 1,
            "cell_task": CELL_TASK,
            "output_root": str(layout["output"]),
            "archive_path": str(archive),
            "phase": phase,
            "rebuild_token": "token-1",
            "build_fingerprint": "build-1",
            "expected_snapshot_sha256": _serial_digest(RAW_SERIALS),
        },
    )
    return marker


def _compatible_prepared_marker(layout: dict[str, Path]) -> tuple[Path, Path]:
    marker = layout["output_parent"] / ".task_a.rebuild-journal.json"
    audit = (
        layout["output_parent"]
        / ".task_a.rebuild-build-adoption-compatible.json"
    )
    journal_base = {
        "version": 1,
        "cell_task": CELL_TASK,
        "output_root": str(layout["output"]),
        "archive_path": None,
        "phase": "prepared",
        "rebuild_token": "token-1",
        "build_fingerprint": "build-1",
        "expected_snapshot_sha256": _serial_digest(RAW_SERIALS),
    }
    audit_payload = {
        "version": 1,
        "kind": "compatible-partial-rebuild-build-adoption",
        "cell_task": CELL_TASK,
        "new_build_fingerprint": "build-1",
        "raw_snapshot_sha256": _serial_digest(RAW_SERIALS),
        "durable_count": len(RAW_SERIALS),
        "durable_serials_sha256": _serial_digest(RAW_SERIALS),
        "previous_journal": dict(journal_base),
    }
    _write_json(audit, audit_payload)
    canonical_audit = json.dumps(
        audit_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    journal = {
        **journal_base,
        "compatible_build_adoption": {
            "version": 1,
            "audit_path": str(audit),
            "audit_sha256": hashlib.sha256(canonical_audit).hexdigest(),
        },
    }
    _write_json(marker, journal)
    return marker, audit


def _read_state(layout: dict[str, Path]) -> dict:
    return json.loads(layout["state"].read_text(encoding="utf-8"))


def _active_artifacts(layout: dict[str, Path]) -> list[Path]:
    return [
        layout["output_parent"] / ".task_a.finalization-pending.json",
        layout["output_parent"] / ".task_a.rebuild-journal.json",
        layout["output_parent"] / ".task_a.recovery-intent.json",
    ]


def _regular_fingerprint(path: Path) -> dict:
    parent_fd = recovery_service._open_directory_chain_nofollow(path.parent)
    try:
        _, fingerprint = recovery_service._read_regular_bytes_at(
            parent_fd,
            path.name,
        )
        return fingerprint
    finally:
        os.close(parent_fd)


def _directory_fingerprint(path: Path) -> dict:
    parent_fd = recovery_service._open_directory_chain_nofollow(path.parent)
    try:
        fingerprint = recovery_service._fingerprint_directory_at(
            parent_fd,
            path.name,
        )
        assert fingerprint is not None
        return fingerprint
    finally:
        os.close(parent_fd)


def _compact_json_digest(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _contract_manifest(
    tmp_path: Path,
    *,
    serials: list[str] | None = None,
    contract: object | None = None,
) -> tuple[Path, str]:
    resolved_serials = list(RAW_SERIALS if serials is None else serials)
    contract_payload = (
        {"fixture": "contract-v1"}
        if contract is None
        else contract.to_dict()
    )
    digest = "9" * 64 if contract is None else contract.digest
    target_fps = (
        24 if contract is None else contract.conversion_schema.fps
    )
    payload = {
        "version": 1,
        "contract_version": 1,
        "digest_algorithm": "sha256",
        "task": CELL_TASK,
        "target_fps": target_fps,
        "invalid": [],
        "invariants": {
            "partition_intersections_empty": True,
            "raw_mutation_performed": False,
            "recorded_exactly_once": True,
            "resolved_invalid_intersection_empty": True,
        },
        "partitions": [
            {
                "digest": digest,
                "contract": contract_payload,
                "serials": resolved_serials,
            }
        ],
        "recordings": [
            {"digest": digest, "serial": serial, "status": "resolved"}
            for serial in resolved_serials
        ],
        "summary": {
            "invalid": 0,
            "partition_count": 1,
            "resolved": len(resolved_serials),
            "total": len(resolved_serials),
        },
    }
    path = tmp_path / "contract-manifest.json"
    _write_json(path, payload)
    path.chmod(0o600)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _rewrite_contract_manifest(path: Path, mutate) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    _write_json(path, payload)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mock_matching_raw_contract(
    monkeypatch: pytest.MonkeyPatch,
    manifest: Path,
) -> None:
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    monkeypatch.setattr(
        recovery_service,
        "_current_raw_contract_manifest",
        lambda **_kwargs: json.loads(json.dumps(payload)),
    )


def _canonical_contract():
    contract_class = recovery_service._resolved_contract_class()
    config = SimpleNamespace(
        fps=24,
        robot_type="fixture_robot",
        camera_names=[],
        action_order=[],
        joint_order={"obs": [], "action": {}},
        observation_modality=None,
        action_modalities=(),
    )
    return contract_class.resolve(config, {}, source_fps=24)


def _fake_contract_class(
    *,
    compatible: bool = True,
    programming_error: bool = False,
):
    class Contract:
        digest = "9" * 64
        conversion_schema = SimpleNamespace(fps=24)

        def assert_dataset_info_compatible(self, info, *, context):
            if programming_error:
                raise RuntimeError("contract validator bug")
            if not compatible:
                mismatch_class = (
                    recovery_service._conversion_schema_mismatch_class()
                )
                raise mismatch_class(
                    context=context,
                    differences={
                        "action": {"expected": 19, "actual": 16},
                    },
                )

    class ContractClass:
        @staticmethod
        def from_dict(payload):
            assert payload == {"fixture": "contract-v1"}
            return Contract()

    return ContractClass


def _prepare_rollback_intent(
    layout: dict[str, Path],
) -> tuple[Path, Path, Path, dict]:
    _write_dataset(
        layout["output"],
        serials=[],
        validation_status="failed",
        identity="incomplete-output",
    )
    archive = layout["output_parent"] / ".task_a.rebuild-output-1"
    _write_dataset(
        archive,
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="valid-archive",
    )
    _finalization_marker(layout)
    marker = _rebuild_marker(layout, archive, phase="prepared")

    def crash_after_intent(window: str) -> None:
        if window == "after_intent_write":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        _service(layout, crash_hook=crash_after_intent).recover(
            CELL_TASK,
            "rollback",
        )
    intent_path = layout["output_parent"] / ".task_a.recovery-intent.json"
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    return archive, marker, intent_path, intent


def _write_complete_output_skeleton(path: Path) -> None:
    episodes = path / "meta" / "episodes" / "chunk-000"
    data = path / "data" / "chunk-000"
    episodes.mkdir(parents=True)
    data.mkdir(parents=True)
    _write_json(
        path / "meta" / "info.json",
        {
            "codebase_version": "v3.0",
            "features": {},
            "fps": 30,
            "total_episodes": 1,
            "total_frames": 1,
        },
    )
    pq.write_table(
        pa.table({"task_index": [0], "task": ["task_a"]}),
        path / "meta" / "tasks.parquet",
    )
    pq.write_table(
        pa.table(
            {
                "episode_index": [0],
                "task_index": [0],
                "data/chunk_index": [0],
                "data/file_index": [0],
                "dataset_from_index": [0],
                "dataset_to_index": [1],
            }
        ),
        episodes / "file-000.parquet",
    )
    pq.write_table(
        pa.table(
            {
                "episode_index": [0],
                "frame_index": [0],
                "index": [0],
                "task_index": [0],
                "timestamp": [0.0],
            }
        ),
        data / "file-000.parquet",
    )


class _UnsupportedRenameAt2:
    argtypes = None
    restype = None

    def __call__(self, *_args) -> int:
        ctypes.set_errno(errno.EINVAL)
        return -1


class _UnsupportedRenameLibc:
    renameat2 = _UnsupportedRenameAt2()


def test_nfs_rename_fallback_requires_isolation_and_preserves_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source_parent = tmp_path / "source"
    destination_parent = tmp_path / "destination"
    source_parent.mkdir()
    destination_parent.mkdir()
    (source_parent / "payload").mkdir()
    (destination_parent / "occupied").mkdir()
    src_fd = os.open(source_parent, os.O_RDONLY | os.O_DIRECTORY)
    dst_fd = os.open(destination_parent, os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.setattr(
        recovery_service.ctypes,
        "CDLL",
        lambda *_args, **_kwargs: _UnsupportedRenameLibc(),
    )
    monkeypatch.setattr(
        recovery_service,
        "_filesystem_type",
        lambda _fd: recovery_service._NFS_SUPER_MAGIC,
    )
    try:
        with pytest.raises(RecoveryError) as exc_info:
            recovery_service._rename_noreplace(
                src_fd,
                "payload",
                dst_fd,
                "moved",
            )
        assert exc_info.value.code == "nfs_isolation_required"
        assert (source_parent / "payload").is_dir()

        monkeypatch.setenv("CURATION_RECOVERY_ISOLATED", "true")
        with pytest.raises(FileExistsError):
            recovery_service._rename_noreplace(
                src_fd,
                "payload",
                dst_fd,
                "occupied",
            )
        assert (source_parent / "payload").is_dir()
        assert (destination_parent / "occupied").is_dir()

        recovery_service._rename_noreplace(
            src_fd,
            "payload",
            dst_fd,
            "moved",
        )
        assert not (source_parent / "payload").exists()
        assert (destination_parent / "moved").is_dir()
    finally:
        os.close(dst_fd)
        os.close(src_fd)


def test_inspect_recommends_rollback_for_prepared_rebuild_with_valid_archive(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=[],
        validation_status="failed",
        identity="incomplete-output",
    )
    archive = layout["output_parent"] / ".task_a.rebuild-output-1"
    _write_dataset(
        archive,
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="valid-archive",
    )
    _finalization_marker(layout)
    _rebuild_marker(layout, archive, phase="prepared")

    inspection = _service(layout).inspect(CELL_TASK)

    assert inspection["recommended_modes"][0] == "rollback"


def test_inspect_recommends_adoption_for_armed_finalization_with_valid_output(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="complete-output",
    )
    _finalization_marker(layout)

    inspection = _service(layout).inspect(CELL_TASK)

    assert inspection["recommended_modes"] == ["adopt-finalization"]


def test_inspect_recommends_quarantine_for_freshly_failed_output(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=[],
        validation_status="failed",
        identity="broken-output",
    )

    inspection = _service(layout).inspect(CELL_TASK)

    assert inspection["recommended_modes"] == ["quarantine-restart"]


def test_contract_manifest_authorization_must_be_paired(
    recovery_layout: dict[str, Path],
):
    with pytest.raises(RecoveryError) as exc_info:
        RecoveryService(
            recovery_layout["raw_root"],
            recovery_layout["lerobot_root"],
            contract_manifest_path=Path("/tmp/manifest.json"),
        )

    assert exc_info.value.code == "invalid_authorization"


def test_matching_canonical_contract_manifest_preserves_passed_validation(
    recovery_layout: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    layout = recovery_layout
    contract = _canonical_contract()
    _write_dataset(
        layout["output"],
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="matching-output",
    )
    _write_json(
        layout["output"] / "meta" / "info.json",
        {
            "robot_type": "fixture_robot",
            "fps": 24,
            "conversion_schema": contract.conversion_schema.to_dict(),
            "features": {
                "observation.state": {
                    "dtype": "float32",
                    "shape": [0],
                    "names": [],
                },
                "action": {
                    "dtype": "float32",
                    "shape": [0],
                    "names": [],
                },
            },
            contract.INFO_KEY: contract.to_dict(),
            contract.DIGEST_KEY: contract.digest,
        },
    )
    manifest, digest = _contract_manifest(tmp_path, contract=contract)
    _mock_matching_raw_contract(monkeypatch, manifest)

    inspection = RecoveryService(
        layout["raw_root"],
        layout["lerobot_root"],
        validation_runner=_validation_runner,
        contract_manifest_path=manifest,
        authorized_contract_manifest_sha256=digest,
    ).inspect(CELL_TASK)

    assert inspection["output"]["validation"] == {
        "status": "passed",
        "identity": "matching-output",
    }


def test_contract_manifest_rejects_wrong_full_file_sha256(
    recovery_layout: dict[str, Path],
    tmp_path: Path,
):
    manifest, _ = _contract_manifest(tmp_path)

    with pytest.raises(RecoveryError) as exc_info:
        RecoveryService(
            recovery_layout["raw_root"],
            recovery_layout["lerobot_root"],
            contract_manifest_path=manifest,
            authorized_contract_manifest_sha256="0" * 64,
        )

    assert exc_info.value.code == "manifest_tampered"


@pytest.mark.parametrize(
    "malformation",
    [
        pytest.param("wrong-task", id="wrong-task"),
        pytest.param("target-fps-mismatch", id="target-fps-mismatch"),
        pytest.param("partition-digest-mismatch", id="partition-digest-mismatch"),
        pytest.param("zero-partitions", id="zero-partitions"),
        pytest.param("multiple-partitions", id="multiple-partitions"),
        pytest.param("invalid-set", id="invalid-set"),
        pytest.param("raw-serial-mismatch", id="raw-serial-mismatch"),
        pytest.param("unknown-top-level-field", id="unknown-top-level-field"),
        pytest.param("unknown-partition-field", id="unknown-partition-field"),
        pytest.param("boolean-version", id="boolean-version"),
        pytest.param("boolean-contract-version", id="boolean-contract-version"),
        pytest.param("boolean-summary", id="boolean-summary"),
    ],
)
def test_contract_manifest_rejects_noncanonical_authorized_content(
    recovery_layout: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    malformation: str,
):
    manifest, _ = _contract_manifest(tmp_path)

    def mutate(payload: dict) -> None:
        if malformation == "wrong-task":
            payload["task"] = "cell001/task_b"
        elif malformation == "target-fps-mismatch":
            payload["target_fps"] = 30
        elif malformation == "partition-digest-mismatch":
            payload["partitions"][0]["digest"] = "8" * 64
            for recording in payload["recordings"]:
                recording["digest"] = "8" * 64
        elif malformation == "zero-partitions":
            payload["partitions"] = []
        elif malformation == "multiple-partitions":
            payload["partitions"].append(dict(payload["partitions"][0]))
        elif malformation == "invalid-set":
            payload["invalid"] = [{"serial": RAW_SERIALS[0]}]
        elif malformation == "raw-serial-mismatch":
            payload["partitions"][0]["serials"] = RAW_SERIALS[:-1]
            payload["recordings"] = payload["recordings"][:-1]
            payload["summary"]["resolved"] = 1
            payload["summary"]["total"] = 1
        elif malformation == "unknown-top-level-field":
            payload["unexpected"] = True
        elif malformation == "unknown-partition-field":
            payload["partitions"][0]["unexpected"] = True
        elif malformation == "boolean-version":
            payload["version"] = True
        elif malformation == "boolean-contract-version":
            payload["contract_version"] = True
        elif malformation == "boolean-summary":
            payload["summary"]["partition_count"] = True

    digest = _rewrite_contract_manifest(manifest, mutate)
    monkeypatch.setattr(
        recovery_service,
        "_resolved_contract_class",
        lambda: _fake_contract_class(),
    )
    service = RecoveryService(
        recovery_layout["raw_root"],
        recovery_layout["lerobot_root"],
        validation_runner=_validation_runner,
        contract_manifest_path=manifest,
        authorized_contract_manifest_sha256=digest,
    )

    with pytest.raises(RecoveryError) as exc_info:
        service.inspect(CELL_TASK)

    assert exc_info.value.code == "invalid_contract_manifest"


@pytest.mark.parametrize(
    "unsafe_file",
    [
        pytest.param("mode", id="mode-0640"),
        pytest.param("hardlink", id="multiple-hardlinks"),
    ],
)
def test_contract_manifest_rejects_unsafe_file_identity(
    recovery_layout: dict[str, Path],
    tmp_path: Path,
    unsafe_file: str,
):
    manifest, digest = _contract_manifest(tmp_path)
    if unsafe_file == "mode":
        manifest.chmod(0o640)
    else:
        os.link(manifest, manifest.with_suffix(".hardlink"))

    with pytest.raises(RecoveryError) as exc_info:
        RecoveryService(
            recovery_layout["raw_root"],
            recovery_layout["lerobot_root"],
            contract_manifest_path=manifest,
            authorized_contract_manifest_sha256=digest,
        )

    assert exc_info.value.code == "unsafe_file"


@pytest.mark.parametrize("protected_root", ["raw_root", "lerobot_root"])
def test_contract_manifest_must_be_outside_mutated_data_roots(
    recovery_layout: dict[str, Path],
    protected_root: str,
):
    manifest = recovery_layout[protected_root] / "contract-manifest.json"
    _write_json(manifest, {"version": 1})
    manifest.chmod(0o600)
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()

    with pytest.raises(RecoveryError) as exc_info:
        RecoveryService(
            recovery_layout["raw_root"],
            recovery_layout["lerobot_root"],
            contract_manifest_path=manifest,
            authorized_contract_manifest_sha256=digest,
        )

    assert exc_info.value.code == "unsafe_contract_manifest_path"


def test_contract_manifest_fifo_is_rejected_without_blocking(
    recovery_layout: dict[str, Path],
    tmp_path: Path,
):
    manifest = tmp_path / "contract-manifest.json"
    os.mkfifo(manifest)
    cancel_release = threading.Event()
    release_attempted = threading.Event()
    release_errors: list[OSError] = []

    def release_blocked_reader() -> None:
        if cancel_release.wait(0.4):
            return
        release_attempted.set()
        try:
            writer_fd = os.open(manifest, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            if exc.errno != errno.ENXIO:
                release_errors.append(exc)
        else:
            os.close(writer_fd)

    release = threading.Thread(target=release_blocked_reader)
    release.start()
    try:
        with pytest.raises(RecoveryError) as exc_info:
            RecoveryService(
                recovery_layout["raw_root"],
                recovery_layout["lerobot_root"],
                contract_manifest_path=manifest,
                authorized_contract_manifest_sha256="0" * 64,
            )
    finally:
        cancel_release.set()
        release.join(timeout=1)

    assert exc_info.value.code == "unsafe_file"
    assert release_errors == []
    assert not release_attempted.is_set()
    assert not release.is_alive()


def test_resolved_contract_class_loads_from_submodule_fallback():
    contract_class = recovery_service._resolved_contract_class()

    assert contract_class.__name__ == "ResolvedRecordingContract"


def test_contract_semantic_mismatch_becomes_quarantine_validation_failure(
    recovery_layout: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="legacy-action16",
    )
    info_path = layout["output"] / "meta" / "info.json"
    info_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(info_path, {"fps": 24, "features": {}})
    manifest, digest = _contract_manifest(tmp_path)
    _mock_matching_raw_contract(monkeypatch, manifest)
    monkeypatch.setattr(
        recovery_service,
        "_resolved_contract_class",
        lambda: _fake_contract_class(compatible=False),
    )

    inspection = RecoveryService(
        layout["raw_root"],
        layout["lerobot_root"],
        validation_runner=_validation_runner,
        contract_manifest_path=manifest,
        authorized_contract_manifest_sha256=digest,
    ).inspect(CELL_TASK)

    assert inspection["output"]["validation"]["status"] == "failed"
    assert "expected" in inspection["output"]["validation"]["summary"]
    assert inspection["recommended_modes"] == ["quarantine-restart"]
    assert inspection["contract_manifest"]["authorized_sha256"] == digest
    assert inspection["contract_manifest"]["contract_digest"] == "9" * 64


def test_contract_manifest_inode_replacement_fails_closed(
    recovery_layout: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    manifest, digest = _contract_manifest(tmp_path)
    _mock_matching_raw_contract(monkeypatch, manifest)
    monkeypatch.setattr(
        recovery_service,
        "_resolved_contract_class",
        lambda: _fake_contract_class(),
    )
    service = RecoveryService(
        recovery_layout["raw_root"],
        recovery_layout["lerobot_root"],
        validation_runner=_validation_runner,
        contract_manifest_path=manifest,
        authorized_contract_manifest_sha256=digest,
    )
    original = manifest.read_bytes()
    replacement = manifest.with_suffix(".replacement")
    replacement.write_bytes(original)
    replacement.chmod(0o600)
    replacement.replace(manifest)

    with pytest.raises(RecoveryError) as exc_info:
        service.inspect(CELL_TASK)

    assert exc_info.value.code == "manifest_tampered"


@pytest.mark.parametrize(
    "tampering",
    [
        pytest.param("replace-inode", id="replace-inode"),
        pytest.param("change-bytes", id="change-bytes"),
    ],
)
def test_contract_manifest_tampering_on_replay_fails_before_output_rename(
    recovery_layout: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tampering: str,
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="legacy-action16",
    )
    info_path = layout["output"] / "meta" / "info.json"
    info_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(info_path, {"fps": 24, "features": {}})
    manifest, digest = _contract_manifest(tmp_path)
    _mock_matching_raw_contract(monkeypatch, manifest)
    monkeypatch.setattr(
        recovery_service,
        "_resolved_contract_class",
        lambda: _fake_contract_class(compatible=False),
    )

    def crash_after_intent(window: str) -> None:
        if window == "after_intent_write":
            raise RuntimeError("simulated crash")

    service = RecoveryService(
        layout["raw_root"],
        layout["lerobot_root"],
        validation_runner=_validation_runner,
        crash_hook=crash_after_intent,
        contract_manifest_path=manifest,
        authorized_contract_manifest_sha256=digest,
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        service.recover(CELL_TASK, "quarantine-restart")
    output_before = _directory_fingerprint(layout["output"])

    if tampering == "replace-inode":
        replacement = manifest.with_suffix(".replacement")
        replacement.write_bytes(manifest.read_bytes())
        replacement.chmod(0o600)
        replacement.replace(manifest)
    else:
        _rewrite_contract_manifest(
            manifest,
            lambda payload: payload.__setitem__("task", "cell001/task_b"),
        )
    service.crash_hook = lambda _window: None

    with pytest.raises(RecoveryError) as exc_info:
        service.recover(CELL_TASK, "quarantine-restart")

    assert exc_info.value.code == "manifest_tampered"
    assert _directory_fingerprint(layout["output"]) == output_before
    assert not list(
        layout["output_parent"].glob(".task_a.recovery-quarantine-*")
    )


def test_current_raw_contract_mismatch_fails_before_output_rename(
    recovery_layout: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="legacy-action16",
    )
    manifest, digest = _contract_manifest(tmp_path)
    current_raw_manifest = json.loads(manifest.read_text(encoding="utf-8"))
    current_raw_manifest["partitions"][0]["contract"] = {
        "fixture": "different-current-raw-contract"
    }
    monkeypatch.setattr(
        recovery_service,
        "_current_raw_contract_manifest",
        lambda **_kwargs: current_raw_manifest,
    )
    monkeypatch.setattr(
        recovery_service,
        "_resolved_contract_class",
        lambda: _fake_contract_class(compatible=False),
    )
    output_before = _directory_fingerprint(layout["output"])

    with pytest.raises(RecoveryError) as exc_info:
        RecoveryService(
            layout["raw_root"],
            layout["lerobot_root"],
            validation_runner=_validation_runner,
            contract_manifest_path=manifest,
            authorized_contract_manifest_sha256=digest,
        ).recover(CELL_TASK, "quarantine-restart")

    assert exc_info.value.code == "raw_contract_mismatch"
    assert _directory_fingerprint(layout["output"]) == output_before
    assert not list(
        layout["output_parent"].glob(".task_a.recovery-quarantine-*")
    )


def test_raw_mutation_during_contract_probe_fails_before_output_rename(
    recovery_layout: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="legacy-action16",
    )
    manifest, digest = _contract_manifest(tmp_path)
    authorized_payload = json.loads(manifest.read_text(encoding="utf-8"))

    def mutate_raw_during_probe(**_kwargs) -> dict:
        (layout["raw_task"] / RAW_SERIALS[0] / "metacard.json").write_text(
            "{}",
            encoding="utf-8",
        )
        return authorized_payload

    monkeypatch.setattr(
        recovery_service,
        "_current_raw_contract_manifest",
        mutate_raw_during_probe,
    )
    monkeypatch.setattr(
        recovery_service,
        "_resolved_contract_class",
        lambda: _fake_contract_class(compatible=False),
    )
    output_before = _directory_fingerprint(layout["output"])

    with pytest.raises(RecoveryError) as exc_info:
        RecoveryService(
            layout["raw_root"],
            layout["lerobot_root"],
            validation_runner=_validation_runner,
            contract_manifest_path=manifest,
            authorized_contract_manifest_sha256=digest,
        ).recover(CELL_TASK, "quarantine-restart")

    assert exc_info.value.code == "raw_task_changed"
    assert _directory_fingerprint(layout["output"]) == output_before
    assert not list(
        layout["output_parent"].glob(".task_a.recovery-quarantine-*")
    )


def test_raw_contract_probe_cache_is_revalidated_after_raw_change_on_replay(
    recovery_layout: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="legacy-action16",
    )
    info_path = layout["output"] / "meta" / "info.json"
    info_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(info_path, {"fps": 24, "features": {}})
    manifest, digest = _contract_manifest(tmp_path)
    authorized_payload = json.loads(manifest.read_text(encoding="utf-8"))
    probe_calls = 0

    def probe_current_raw(**_kwargs) -> dict:
        nonlocal probe_calls
        probe_calls += 1
        payload = json.loads(json.dumps(authorized_payload))
        if probe_calls > 1:
            payload["partitions"][0]["contract"] = {
                "fixture": "replacement-raw-contract"
            }
        return payload

    monkeypatch.setattr(
        recovery_service,
        "_current_raw_contract_manifest",
        probe_current_raw,
    )
    monkeypatch.setattr(
        recovery_service,
        "_resolved_contract_class",
        lambda: _fake_contract_class(compatible=False),
    )

    def crash_after_intent(window: str) -> None:
        if window == "after_intent_write":
            raise RuntimeError("simulated crash")

    service = RecoveryService(
        layout["raw_root"],
        layout["lerobot_root"],
        validation_runner=_validation_runner,
        crash_hook=crash_after_intent,
        contract_manifest_path=manifest,
        authorized_contract_manifest_sha256=digest,
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        service.recover(CELL_TASK, "quarantine-restart")
    assert probe_calls == 1
    output_before = _directory_fingerprint(layout["output"])
    (layout["raw_task"] / RAW_SERIALS[0] / "metacard.json").write_text(
        "{}",
        encoding="utf-8",
    )
    service.crash_hook = lambda _window: None

    with pytest.raises(RecoveryError) as exc_info:
        service.recover(CELL_TASK, "quarantine-restart")

    assert exc_info.value.code == "raw_contract_mismatch"
    assert probe_calls == 2
    assert _directory_fingerprint(layout["output"]) == output_before
    assert not list(
        layout["output_parent"].glob(".task_a.recovery-quarantine-*")
    )


def test_contract_validator_programming_error_does_not_authorize_quarantine(
    recovery_layout: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="legacy-output",
    )
    info_path = layout["output"] / "meta" / "info.json"
    info_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(info_path, {"fps": 24, "features": {}})
    manifest, digest = _contract_manifest(tmp_path)
    _mock_matching_raw_contract(monkeypatch, manifest)
    monkeypatch.setattr(
        recovery_service,
        "_resolved_contract_class",
        lambda: _fake_contract_class(programming_error=True),
    )

    service = RecoveryService(
        layout["raw_root"],
        layout["lerobot_root"],
        validation_runner=_validation_runner,
        contract_manifest_path=manifest,
        authorized_contract_manifest_sha256=digest,
    )

    with pytest.raises(RuntimeError, match="contract validator bug"):
        service.inspect(CELL_TASK)

    assert not list(
        layout["output_parent"].glob(".task_a.recovery-intent.json")
    )


def test_contract_info_swap_restore_cannot_authorize_quarantine(
    recovery_layout: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="matching-output",
    )
    info_path = layout["output"] / "meta" / "info.json"
    _write_json(info_path, {"fps": 24, "features": {}})
    manifest, digest = _contract_manifest(tmp_path)
    _mock_matching_raw_contract(monkeypatch, manifest)
    monkeypatch.setattr(
        recovery_service,
        "_resolved_contract_class",
        lambda: _fake_contract_class(compatible=True),
    )

    def swap_restore_runner(dataset_dir: Path) -> dict[str, str]:
        anchored_info = dataset_dir / "meta" / "info.json"
        original = anchored_info.read_bytes()
        anchored_info.write_bytes(b'{"fps":30,"features":{}}')
        anchored_info.write_bytes(original)
        return {
            "status": "failed",
            "summary": "Full failed: transient swapped info",
        }

    info_before = info_path.read_bytes()
    service = RecoveryService(
        layout["raw_root"],
        layout["lerobot_root"],
        validation_runner=swap_restore_runner,
        contract_manifest_path=manifest,
        authorized_contract_manifest_sha256=digest,
    )

    with pytest.raises(RecoveryError) as exc_info:
        service.recover(CELL_TASK, "quarantine-restart")

    assert exc_info.value.code == "tree_changed"
    assert info_path.read_bytes() == info_before
    assert layout["output"].is_dir()
    assert not list(
        layout["output_parent"].glob(".task_a.recovery-quarantine-*")
    )


def test_inspect_recommends_commit_for_verified_rebuild_with_valid_output(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="verified-output",
    )
    archive = layout["output_parent"] / ".task_a.rebuild-output-1"
    _rebuild_marker(layout, archive, phase="verified")

    inspection = _service(layout).inspect(CELL_TASK)

    assert inspection["recommended_modes"] == ["commit-verified"]


def test_rollback_preserves_current_output_and_restores_valid_archive(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=[],
        validation_status="failed",
        identity="incomplete-output",
    )
    archive = layout["output_parent"] / ".task_a.rebuild-output-1"
    _write_dataset(
        archive,
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="valid-archive",
    )
    _finalization_marker(layout)
    _rebuild_marker(layout, archive, phase="prepared")

    result = _service(layout).recover(CELL_TASK, "rollback")

    assert result["phase"] == "receipt_durable"
    assert json.loads(
        (layout["output"] / "validation-result.json").read_text(encoding="utf-8")
    )["identity"] == "valid-archive"
    quarantine = layout["output_parent"] / result["paths"]["quarantine"]
    assert json.loads(
        (quarantine / "validation-result.json").read_text(encoding="utf-8")
    )["identity"] == "incomplete-output"


@pytest.mark.parametrize(
    "snapshot_sha256",
    [
        pytest.param("0" * 64, id="all-zero"),
        pytest.param("not-a-sha256", id="invalid"),
    ],
)
def test_rollback_rejects_invalid_marker_snapshot_before_output_rename(
    recovery_layout: dict[str, Path],
    snapshot_sha256: str,
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=[],
        validation_status="failed",
        identity="incomplete-output",
    )
    archive = layout["output_parent"] / ".task_a.rebuild-output-1"
    _write_dataset(
        archive,
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="valid-archive",
    )
    finalization = _finalization_marker(layout)
    rebuild = _rebuild_marker(layout, archive, phase="prepared")
    finalization_payload = json.loads(finalization.read_text(encoding="utf-8"))
    finalization_payload["raw_snapshot_before"] = snapshot_sha256
    _write_json(finalization, finalization_payload)
    rebuild_payload = json.loads(rebuild.read_text(encoding="utf-8"))
    rebuild_payload["expected_snapshot_sha256"] = snapshot_sha256
    _write_json(rebuild, rebuild_payload)

    with pytest.raises(RecoveryError):
        _service(layout).recover(CELL_TASK, "rollback")

    assert layout["output"].is_dir()
    assert archive.is_dir()
    assert not list(
        layout["output_parent"].glob(".task_a.recovery-quarantine-*")
    )


def test_adoption_rejects_valid_but_stale_marker_snapshot(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="complete-output",
    )
    marker = _finalization_marker(layout)
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["raw_snapshot_before"] = "1" * 64
    _write_json(marker, payload)

    with pytest.raises(RecoveryError) as exc_info:
        _service(layout).recover(CELL_TASK, "adopt-finalization")

    assert exc_info.value.code == "marker_snapshot_mismatch"
    assert marker.is_file()
    assert not list(layout["output_parent"].glob(".task_a.recovery-intent.json"))


def test_explicit_legacy_marker_authorization_binds_full_marker_and_serials(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="complete-output",
    )
    marker = _finalization_marker(layout)
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["raw_snapshot_before"] = "1" * 64
    payload["raw_serials_before"] = RAW_SERIALS
    _write_json(marker, payload)
    marker_sha256 = _regular_fingerprint(marker)["sha256"]

    result = _service(
        layout,
        authorized_legacy_marker_sha256s={marker_sha256},
    ).recover(CELL_TASK, "adopt-finalization")

    assert result["phase"] == "receipt_durable"


def test_legacy_marker_authorization_rejects_embedded_serial_mismatch(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="complete-output",
    )
    marker = _finalization_marker(layout)
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["raw_snapshot_before"] = "1" * 64
    payload["raw_serials_before"] = RAW_SERIALS[:-1]
    _write_json(marker, payload)
    marker_sha256 = _regular_fingerprint(marker)["sha256"]

    with pytest.raises(RecoveryError) as exc_info:
        _service(
            layout,
            authorized_legacy_marker_sha256s={marker_sha256},
        ).recover(CELL_TASK, "adopt-finalization")

    assert exc_info.value.code == "marker_snapshot_mismatch"


def test_adopt_finalization_updates_only_target_state_entry(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="complete-output",
    )
    _finalization_marker(layout)
    state_before = layout["state"].read_bytes()

    result = _service(layout).recover(CELL_TASK, "adopt-finalization")

    state = _read_state(layout)
    assert state[CELL_TASK]["converted_count"] == len(RAW_SERIALS)
    assert state[CELL_TASK]["failed_serials"] == []
    assert state["cell999/untouched"] == {"converted_count": 7, "sentinel": True}
    backup = layout["lerobot_root"] / result["paths"]["state_backup"]
    assert backup.read_bytes() == state_before


def test_quarantine_restart_resets_target_state_and_keeps_output_in_quarantine(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=[],
        validation_status="failed",
        identity="broken-output",
    )

    result = _service(layout).recover(CELL_TASK, "quarantine-restart")

    assert not layout["output"].exists()
    assert (layout["output_parent"] / result["paths"]["quarantine"]).is_dir()
    assert _read_state(layout)[CELL_TASK]["converted_count"] == 0


def test_commit_verified_keeps_output_and_audits_rebuild_marker(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="verified-output",
    )
    archive = layout["output_parent"] / ".task_a.rebuild-output-1"
    _write_dataset(
        archive,
        serials=[RAW_SERIALS[0]],
        validation_status="passed",
        identity="preserved-archive",
    )
    marker = _rebuild_marker(layout, archive, phase="verified")

    result = _service(layout).recover(CELL_TASK, "commit-verified")

    assert layout["output"].is_dir()
    assert not marker.exists()
    audit = next(
        item
        for item in result["inputs"]["markers"]
        if item["kind"] == "rebuild"
    )
    assert (layout["output_parent"] / audit["audit"]).is_file()


def test_commit_verified_accepts_legacy_verified_journal_without_archive(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="verified-output",
    )
    marker = layout["output_parent"] / ".task_a.rebuild-journal.json"
    _write_json(
        marker,
        {
            "version": 1,
            "cell_task": CELL_TASK,
            "output_root": str(layout["output"]),
            "archive_path": None,
            "phase": "verified",
            "rebuild_token": "token-1",
            "build_fingerprint": "build-1",
            "expected_snapshot_sha256": _serial_digest(RAW_SERIALS),
        },
    )

    result = _service(layout).recover(CELL_TASK, "commit-verified")

    assert result["phase"] == "receipt_durable"
    assert result["paths"]["archive"] is None
    assert layout["output"].is_dir()


def test_commit_verified_accepts_prepared_journal_with_compatible_adoption(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="compatible-output",
    )
    marker, audit = _compatible_prepared_marker(layout)

    result = _service(layout).recover(CELL_TASK, "commit-verified")

    assert result["phase"] == "receipt_durable"
    assert not marker.exists()
    assert audit.is_file()
    assert result["inputs"]["compatible_adoption"]["basename"] == audit.name


def test_partial_validation_rejects_adoption_without_mutating_state(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=[],
        validation_status="partial",
        identity="partial-output",
    )
    _finalization_marker(layout)
    state_before = layout["state"].read_bytes()

    with pytest.raises(RecoveryError) as exc_info:
        _service(layout).recover(CELL_TASK, "adopt-finalization")

    assert exc_info.value.code == "validation_not_passed"
    assert layout["state"].read_bytes() == state_before


def test_durable_serial_outside_raw_set_rejects_adoption(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=[*RAW_SERIALS, "20260727_010103"],
        validation_status="passed",
        identity="foreign-serial-output",
    )
    _finalization_marker(layout)
    state_before = layout["state"].read_bytes()

    with pytest.raises(RecoveryError) as exc_info:
        _service(layout).recover(CELL_TASK, "adopt-finalization")

    assert exc_info.value.code == "raw_subset_mismatch"
    assert layout["state"].read_bytes() == state_before


def test_rollback_rejects_foreign_archive_before_moving_current_output(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=[],
        validation_status="failed",
        identity="incomplete-output",
    )
    archive = layout["output_parent"] / ".task_a.rebuild-output-1"
    _write_dataset(
        archive,
        serials=[*RAW_SERIALS, "20260727_010103"],
        validation_status="passed",
        identity="foreign-archive",
    )
    _finalization_marker(layout)
    _rebuild_marker(layout, archive, phase="prepared")

    with pytest.raises(RecoveryError) as exc_info:
        _service(layout).recover(CELL_TASK, "rollback")

    assert exc_info.value.code == "raw_subset_mismatch"
    assert layout["output"].is_dir()
    assert archive.is_dir()


def test_duplicate_durable_serial_rejects_inspection(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=[RAW_SERIALS[0], RAW_SERIALS[0]],
        validation_status="passed",
        identity="duplicate-output",
    )

    with pytest.raises(RecoveryError) as exc_info:
        _service(layout).inspect(CELL_TASK)

    assert exc_info.value.code == "duplicate_serial"


def test_symlink_output_is_rejected_without_following_target(
    recovery_layout: dict[str, Path],
    tmp_path: Path,
):
    layout = recovery_layout
    outside = tmp_path / "outside"
    _write_dataset(
        outside,
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="outside",
    )
    layout["output"].symlink_to(outside, target_is_directory=True)

    with pytest.raises(RecoveryError) as exc_info:
        _service(layout).inspect(CELL_TASK)

    assert exc_info.value.code == "unsafe_tree"


def test_marker_symlink_is_rejected_without_following_target(
    recovery_layout: dict[str, Path],
    tmp_path: Path,
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="complete-output",
    )
    outside = tmp_path / "outside-marker.json"
    _write_json(
        outside,
        {
            "version": 1,
            "cell_task": CELL_TASK,
            "output_root": str(layout["output"]),
            "phase": "armed",
        },
    )
    (layout["output_parent"] / ".task_a.finalization-pending.json").symlink_to(outside)

    with pytest.raises(RecoveryError) as exc_info:
        _service(layout).inspect(CELL_TASK)

    assert exc_info.value.code == "unsafe_file"


@pytest.mark.parametrize(
    "missing",
    [
        "meta/info.json",
        "meta/tasks.parquet",
        "meta/episodes",
        "data",
    ],
)
def test_recovery_blockers_marks_markerless_incomplete_output(
    recovery_layout: dict[str, Path],
    missing: str,
):
    layout = recovery_layout
    _write_complete_output_skeleton(layout["output"])
    target = layout["output"] / missing
    if target.is_dir():
        for child in sorted(target.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink()
            else:
                child.rmdir()
        target.rmdir()
    else:
        target.unlink()

    assert recovery_blockers(
        CELL_TASK,
        lerobot_root=layout["lerobot_root"],
    ) == ["incomplete-output"]


def test_recovery_blockers_allows_markerless_complete_output_skeleton(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    _write_complete_output_skeleton(layout["output"])

    assert recovery_blockers(
        CELL_TASK,
        lerobot_root=layout["lerobot_root"],
    ) == []


@pytest.mark.parametrize("corruption", ["info-json", "data-parquet", "empty-data"])
def test_recovery_blockers_marks_markerless_corrupt_output(
    recovery_layout: dict[str, Path],
    corruption: str,
):
    layout = recovery_layout
    _write_complete_output_skeleton(layout["output"])
    if corruption == "info-json":
        (layout["output"] / "meta" / "info.json").write_text(
            "{broken",
            encoding="utf-8",
        )
    elif corruption == "data-parquet":
        (
            layout["output"] / "data" / "chunk-000" / "file-000.parquet"
        ).write_bytes(b"not parquet")
    else:
        (
            layout["output"] / "data" / "chunk-000" / "file-000.parquet"
        ).unlink()

    assert recovery_blockers(
        CELL_TASK,
        lerobot_root=layout["lerobot_root"],
    ) == ["incomplete-output"]


def test_tampered_output_after_durable_intent_is_rejected_on_resume(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="complete-output",
    )
    _finalization_marker(layout)

    def crash_after_intent(window: str) -> None:
        if window == "after_intent_write":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        _service(layout, crash_hook=crash_after_intent).recover(
            CELL_TASK,
            "adopt-finalization",
        )
    _write_json(
        layout["output"] / "validation-result.json",
        {"status": "passed", "identity": "tampered-output"},
    )

    with pytest.raises(RecoveryError) as exc_info:
        _service(layout).recover(CELL_TASK, "adopt-finalization")

    assert exc_info.value.code == "candidate_changed"


def test_tampered_intent_path_is_rejected_before_replay(
    recovery_layout: dict[str, Path],
    tmp_path: Path,
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="complete-output",
    )
    _finalization_marker(layout)

    def crash_after_intent(window: str) -> None:
        if window == "after_intent_write":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        _service(layout, crash_hook=crash_after_intent).recover(
            CELL_TASK,
            "adopt-finalization",
        )
    intent_path = layout["output_parent"] / ".task_a.recovery-intent.json"
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    intent["paths"]["state_backup"] = "../escaped-state.json"
    _write_json(intent_path, intent)

    with pytest.raises(RecoveryError) as exc_info:
        _service(layout).recover(CELL_TASK, "adopt-finalization")

    assert exc_info.value.code == "invalid_intent"
    assert not (tmp_path / "escaped-state.json").exists()


def test_tampered_intent_phase_is_rejected_before_replay(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="complete-output",
    )
    _finalization_marker(layout)

    def crash_after_intent(window: str) -> None:
        if window == "after_intent_write":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        _service(layout, crash_hook=crash_after_intent).recover(
            CELL_TASK,
            "adopt-finalization",
        )
    intent_path = layout["output_parent"] / ".task_a.recovery-intent.json"
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    intent["phase"] = "output_quarantine_pending"
    _write_json(intent_path, intent)

    with pytest.raises(RecoveryError) as exc_info:
        _service(layout).recover(CELL_TASK, "adopt-finalization")

    assert exc_info.value.code == "invalid_intent"


def test_tampered_intent_root_identity_is_rejected_before_replay(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="complete-output",
    )
    _finalization_marker(layout)

    def crash_after_intent(window: str) -> None:
        if window == "after_intent_write":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        _service(layout, crash_hook=crash_after_intent).recover(
            CELL_TASK,
            "adopt-finalization",
        )
    intent_path = layout["output_parent"] / ".task_a.recovery-intent.json"
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    intent["roots"]["task_parent"]["ino"] += 1
    _write_json(intent_path, intent)

    with pytest.raises(RecoveryError) as exc_info:
        _service(layout).recover(CELL_TASK, "adopt-finalization")

    assert exc_info.value.code == "root_identity_changed"


def test_intent_owner_is_bound_to_created_inode_not_euid_or_task_parent(
    recovery_layout: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="complete-output",
    )
    _finalization_marker(layout)
    monkeypatch.setattr(
        recovery_service.os,
        "geteuid",
        lambda: layout["output_parent"].stat().st_uid + 10_000,
    )

    def crash_after_intent(window: str) -> None:
        if window == "after_intent_write":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        _service(layout, crash_hook=crash_after_intent).recover(
            CELL_TASK,
            "adopt-finalization",
        )
    intent_path = layout["output_parent"] / ".task_a.recovery-intent.json"
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    bound_owner = intent["intent_owner"]
    assert bound_owner == {
        "uid": intent_path.stat().st_uid,
        "gid": intent_path.stat().st_gid,
    }
    assert intent["intent_file"] == {
        "dev": intent_path.stat().st_dev,
        "ino": intent_path.stat().st_ino,
    }

    real_fstat = recovery_service.os.fstat
    shifted_parent_owner = {
        "uid": bound_owner["uid"] + 20_000,
        "gid": bound_owner["gid"] + 20_000,
    }

    class ShiftedParentOwner:
        def __init__(self, info: os.stat_result):
            self._info = info
            self.st_uid = shifted_parent_owner["uid"]
            self.st_gid = shifted_parent_owner["gid"]

        def __getattr__(self, name: str):
            return getattr(self._info, name)

    def fstat_with_different_task_parent_owner(fd: int):
        info = real_fstat(fd)
        try:
            opened_path = Path(os.readlink(f"/proc/self/fd/{fd}"))
        except OSError:
            return info
        if opened_path == layout["output_parent"]:
            return ShiftedParentOwner(info)
        return info

    monkeypatch.setattr(
        recovery_service.os,
        "fstat",
        fstat_with_different_task_parent_owner,
    )
    result = _service(layout).recover(CELL_TASK, "adopt-finalization")
    receipt = layout["output_parent"] / result["receipt_path"]
    receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert receipt_payload["intent_owner"] == bound_owner
    assert receipt_payload["intent_file"] == {
        "dev": receipt.stat().st_dev,
        "ino": receipt.stat().st_ino,
    }
    assert receipt.stat().st_uid == bound_owner["uid"]
    assert receipt.stat().st_gid == bound_owner["gid"]
    assert bound_owner != shifted_parent_owner
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    assert receipt.stat().st_nlink == 1


def test_tampered_intent_owner_binding_is_rejected_before_replay(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="complete-output",
    )
    _finalization_marker(layout)
    state_before = layout["state"].read_bytes()

    def crash_after_intent(window: str) -> None:
        if window == "after_intent_write":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        _service(layout, crash_hook=crash_after_intent).recover(
            CELL_TASK,
            "adopt-finalization",
        )
    intent_path = layout["output_parent"] / ".task_a.recovery-intent.json"
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    intent["intent_owner"]["uid"] += 1
    _write_json(intent_path, intent)

    with pytest.raises(RecoveryError) as exc_info:
        _service(layout).recover(CELL_TASK, "adopt-finalization")

    assert exc_info.value.code == "invalid_intent"
    assert layout["state"].read_bytes() == state_before
    assert layout["output"].is_dir()


def test_replaced_intent_inode_is_rejected_before_replay(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="complete-output",
    )
    _finalization_marker(layout)
    state_before = layout["state"].read_bytes()

    def crash_after_intent(window: str) -> None:
        if window == "after_intent_write":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        _service(layout, crash_hook=crash_after_intent).recover(
            CELL_TASK,
            "adopt-finalization",
        )
    intent_path = layout["output_parent"] / ".task_a.recovery-intent.json"
    preserved = layout["output_parent"] / ".task_a.replaced-intent.json"
    original_payload = intent_path.read_bytes()
    original_mode = stat.S_IMODE(intent_path.stat().st_mode)
    intent_path.rename(preserved)
    intent_path.write_bytes(original_payload)
    intent_path.chmod(original_mode)
    assert intent_path.stat().st_ino != preserved.stat().st_ino

    with pytest.raises(RecoveryError) as exc_info:
        _service(layout).recover(CELL_TASK, "adopt-finalization")

    assert exc_info.value.code == "invalid_intent"
    assert preserved.is_file()
    assert layout["state"].read_bytes() == state_before
    assert layout["output"].is_dir()


def test_tampered_marker_fingerprint_is_rejected_before_replay(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="complete-output",
    )
    marker = _finalization_marker(layout)

    def crash_after_intent(window: str) -> None:
        if window == "after_intent_write":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        _service(layout, crash_hook=crash_after_intent).recover(
            CELL_TASK,
            "adopt-finalization",
        )
    marker_payload = json.loads(marker.read_text(encoding="utf-8"))
    marker_payload["phase"] = "tampered"
    _write_json(marker, marker_payload)

    with pytest.raises(RecoveryError) as exc_info:
        _service(layout).recover(CELL_TASK, "adopt-finalization")

    assert exc_info.value.code == "marker_tampered"


@pytest.mark.parametrize(
    ("field", "forged_value"),
    [
        pytest.param(
            "archive_path",
            ".task_a.rebuild-output-forged",
            id="archive-relation",
        ),
        pytest.param("phase", "verified", id="phase"),
    ],
)
def test_rollback_reparses_marker_semantics_before_output_rename(
    recovery_layout: dict[str, Path],
    field: str,
    forged_value: str,
):
    layout = recovery_layout
    archive, marker, intent_path, intent = _prepare_rollback_intent(layout)
    marker_payload = json.loads(marker.read_text(encoding="utf-8"))
    marker_payload[field] = (
        str(layout["output_parent"] / forged_value)
        if field == "archive_path"
        else forged_value
    )
    _write_json(marker, marker_payload)
    rebuild_evidence = next(
        evidence
        for evidence in intent["inputs"]["markers"]
        if evidence["kind"] == "rebuild"
    )
    rebuild_evidence["fingerprint"] = _regular_fingerprint(marker)
    _write_json(intent_path, intent)
    quarantine = layout["output_parent"] / intent["paths"]["quarantine"]

    with pytest.raises(RecoveryError):
        _service(layout).recover(CELL_TASK, "rollback")

    assert layout["output"].is_dir()
    assert archive.is_dir()
    assert not quarantine.exists()


def test_rollback_revalidates_changed_archive_before_output_rename(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    archive, _, _, intent = _prepare_rollback_intent(layout)
    _write_json(
        archive / "validation-result.json",
        {"status": "passed", "identity": "changed-after-intent"},
    )
    quarantine = layout["output_parent"] / intent["paths"]["quarantine"]

    with pytest.raises(RecoveryError):
        _service(layout).recover(CELL_TASK, "rollback")

    assert layout["output"].is_dir()
    assert archive.is_dir()
    assert not quarantine.exists()


def test_rollback_revalidates_changed_raw_serials_before_output_rename(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    archive, _, _, intent = _prepare_rollback_intent(layout)
    (layout["raw_task"] / "20260727_010103").mkdir()
    quarantine = layout["output_parent"] / intent["paths"]["quarantine"]

    with pytest.raises(RecoveryError):
        _service(layout).recover(CELL_TASK, "rollback")

    assert layout["output"].is_dir()
    assert archive.is_dir()
    assert not quarantine.exists()


@pytest.mark.parametrize(
    "raw_change",
    [
        pytest.param("mcap-add", id="mcap-file-add"),
        pytest.param("metadata-stat", id="metadata-stat-change"),
    ],
)
def test_rollback_revalidates_raw_serial_contents_before_output_rename(
    recovery_layout: dict[str, Path],
    raw_change: str,
):
    layout = recovery_layout
    metadata = layout["raw_task"] / RAW_SERIALS[0] / "metadata.json"
    if raw_change == "metadata-stat":
        _write_json(metadata, {"robot_type": "test"})
    archive, _, _, intent = _prepare_rollback_intent(layout)
    if raw_change == "mcap-add":
        (layout["raw_task"] / RAW_SERIALS[0] / "recording.mcap").write_bytes(
            b"new raw payload"
        )
    else:
        metadata_info = metadata.stat()
        os.utime(
            metadata,
            ns=(
                metadata_info.st_atime_ns,
                metadata_info.st_mtime_ns + 1_000_000_000,
            ),
        )
    quarantine = layout["output_parent"] / intent["paths"]["quarantine"]

    with pytest.raises(RecoveryError) as exc_info:
        _service(layout).recover(CELL_TASK, "rollback")

    assert exc_info.value.code == "raw_task_changed"
    assert layout["output"].is_dir()
    assert archive.is_dir()
    assert not quarantine.exists()


def test_quarantine_revalidates_failed_status_before_output_rename(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=[],
        validation_status="failed",
        identity="broken-output",
    )

    def crash_after_intent(window: str) -> None:
        if window == "after_intent_write":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        _service(layout, crash_hook=crash_after_intent).recover(
            CELL_TASK,
            "quarantine-restart",
        )
    intent_path = layout["output_parent"] / ".task_a.recovery-intent.json"
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    _write_json(
        layout["output"] / "validation-result.json",
        {"status": "passed", "identity": "now-valid-output"},
    )
    forged_output = _directory_fingerprint(layout["output"])
    intent["inputs"]["output"] = forged_output
    intent["validation"]["tree"] = forged_output
    _write_json(intent_path, intent)
    quarantine = layout["output_parent"] / intent["paths"]["quarantine"]

    with pytest.raises(RecoveryError):
        _service(layout).recover(CELL_TASK, "quarantine-restart")

    assert layout["output"].is_dir()
    assert not quarantine.exists()


def test_replaced_raw_root_identity_rejects_active_intent(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="complete-output",
    )
    _finalization_marker(layout)

    def crash_after_intent(window: str) -> None:
        if window == "after_intent_write":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        _service(layout, crash_hook=crash_after_intent).recover(
            CELL_TASK,
            "adopt-finalization",
        )
    original_raw = layout["raw_root"].with_name("raw-original")
    layout["raw_root"].rename(original_raw)
    for serial in RAW_SERIALS:
        (layout["raw_task"] / serial).mkdir(parents=True)

    with pytest.raises(RecoveryError) as exc_info:
        _service(layout).recover(CELL_TASK, "adopt-finalization")

    assert exc_info.value.code == "root_identity_changed"


def test_missing_state_replacement_is_detected_before_canonical_state_backup(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="complete-output",
    )
    _finalization_marker(layout)
    state_before = layout["state"].read_bytes()

    def crash_before_backup(window: str) -> None:
        if window == "before_state_backup_rename":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        _service(layout, crash_hook=crash_before_backup).recover(
            CELL_TASK,
            "adopt-finalization",
        )
    intent_path = layout["output_parent"] / ".task_a.recovery-intent.json"
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    replacement = layout["lerobot_root"] / intent["paths"]["state_replacement"]
    replacement.unlink()

    with pytest.raises(RecoveryError) as exc_info:
        _service(layout).recover(CELL_TASK, "adopt-finalization")

    assert exc_info.value.code == "missing_state_replacement"
    assert layout["state"].read_bytes() == state_before
    assert not (
        layout["lerobot_root"] / intent["paths"]["state_backup"]
    ).exists()


def test_concurrent_non_target_state_change_is_not_overwritten(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="complete-output",
    )
    _finalization_marker(layout)

    def crash_before_backup(window: str) -> None:
        if window == "before_state_backup_rename":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        _service(layout, crash_hook=crash_before_backup).recover(
            CELL_TASK,
            "adopt-finalization",
        )
    state = _read_state(layout)
    state["cell999/untouched"]["concurrent_update"] = "preserve-me"
    _write_json(layout["state"], state)

    with pytest.raises(RecoveryError) as exc_info:
        _service(layout).recover(CELL_TASK, "adopt-finalization")

    assert exc_info.value.code == "state_artifact_tampered"
    assert _read_state(layout)["cell999/untouched"]["concurrent_update"] == "preserve-me"


def test_forged_state_plan_cannot_change_non_target_before_state_backup(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="complete-output",
    )
    _finalization_marker(layout)
    state_before = layout["state"].read_bytes()

    def crash_before_backup(window: str) -> None:
        if window == "before_state_backup_rename":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        _service(layout, crash_hook=crash_before_backup).recover(
            CELL_TASK,
            "adopt-finalization",
        )
    intent_path = layout["output_parent"] / ".task_a.recovery-intent.json"
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    replacement_path = (
        layout["lerobot_root"] / intent["paths"]["state_replacement"]
    )
    replacement = json.loads(replacement_path.read_text(encoding="utf-8"))
    replacement["cell999/untouched"]["sentinel"] = "forged"
    replacement_path.write_bytes(
        recovery_service._canonical_json_bytes(replacement)
    )
    replacement_bytes = replacement_path.read_bytes()
    forged_non_target = {
        key: value
        for key, value in replacement.items()
        if key != CELL_TASK
    }
    forged_non_target_digest = _compact_json_digest(forged_non_target)
    intent["state"]["replacement_fingerprint"] = _regular_fingerprint(
        replacement_path
    )
    intent["state"]["replacement_sha256"] = hashlib.sha256(
        replacement_bytes
    ).hexdigest()
    intent["state"]["non_target_before_sha256"] = forged_non_target_digest
    intent["state"]["non_target_after_sha256"] = forged_non_target_digest
    _write_json(intent_path, intent)
    backup = layout["lerobot_root"] / intent["paths"]["state_backup"]

    with pytest.raises(RecoveryError):
        _service(layout).recover(CELL_TASK, "adopt-finalization")

    assert layout["state"].read_bytes() == state_before
    assert not backup.exists()


def test_recovery_never_calls_destructive_filesystem_apis(
    recovery_layout: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=[],
        validation_status="failed",
        identity="broken-output",
    )

    def fail_destructive(*_args, **_kwargs):
        raise AssertionError("recovery must preserve artifacts using rename only")

    monkeypatch.setattr(recovery_service.os, "unlink", fail_destructive)
    monkeypatch.setattr(recovery_service.os, "remove", fail_destructive)
    monkeypatch.setattr(recovery_service.os, "rmdir", fail_destructive)

    result = _service(layout).recover(CELL_TASK, "quarantine-restart")

    assert result["phase"] == "receipt_durable"


def test_recovery_replays_after_crash_between_quarantine_rename_and_fsync(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=[],
        validation_status="failed",
        identity="incomplete-output",
    )
    archive = layout["output_parent"] / ".task_a.rebuild-output-1"
    _write_dataset(
        archive,
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="valid-archive",
    )
    _finalization_marker(layout)
    _rebuild_marker(layout, archive, phase="prepared")

    def crash_after_rename(window: str) -> None:
        if window == "after_output_quarantine_rename":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        _service(layout, crash_hook=crash_after_rename).recover(
            CELL_TASK,
            "rollback",
        )

    result = _service(layout).recover(CELL_TASK, "rollback")

    assert result["phase"] == "receipt_durable"
    assert layout["output"].is_dir()


def test_legacy_intent_without_contract_manifest_replays_without_manifest(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="complete-output",
    )
    _finalization_marker(layout)

    def crash_after_intent(window: str) -> None:
        if window == "after_intent_write":
            raise RuntimeError("simulated crash")

    service = _service(layout, crash_hook=crash_after_intent)
    with pytest.raises(RuntimeError, match="simulated crash"):
        service.recover(CELL_TASK, "adopt-finalization")
    intent_path = layout["output_parent"] / ".task_a.recovery-intent.json"
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    intent.pop("contract_manifest")
    _write_json(intent_path, intent)
    service.crash_hook = lambda _window: None

    result = service.recover(CELL_TASK, "adopt-finalization")

    assert result["phase"] == "receipt_durable"
    assert not intent_path.exists()


def test_repeated_recovery_returns_existing_receipt_without_new_artifacts(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="complete-output",
    )
    _finalization_marker(layout)
    service = _service(layout)
    first = service.recover(CELL_TASK, "adopt-finalization")
    artifacts_before = sorted(path.name for path in layout["output_parent"].iterdir())

    second = service.recover(CELL_TASK, "adopt-finalization")

    assert second["receipt_path"] == first["receipt_path"]
    assert sorted(path.name for path in layout["output_parent"].iterdir()) == artifacts_before
    assert all(not path.exists() for path in _active_artifacts(layout))


def test_old_receipt_does_not_hide_a_new_canonical_marker(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="complete-output",
    )
    _finalization_marker(layout)
    service = _service(layout)
    first = service.recover(CELL_TASK, "adopt-finalization")

    _finalization_marker(layout)
    second = service.recover(CELL_TASK, "adopt-finalization")

    assert second["receipt_path"] != first["receipt_path"]
    assert not (
        layout["output_parent"] / ".task_a.finalization-pending.json"
    ).exists()


def test_stale_receipt_is_not_returned_after_terminal_state_changes(
    recovery_layout: dict[str, Path],
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="complete-output",
    )
    _finalization_marker(layout)
    service = _service(layout)
    service.recover(CELL_TASK, "adopt-finalization")
    state = _read_state(layout)
    state[CELL_TASK]["external_update"] = True
    _write_json(layout["state"], state)

    with pytest.raises(RecoveryError) as exc_info:
        service.recover(CELL_TASK, "adopt-finalization")

    assert exc_info.value.code == "wrong_mode"


@pytest.mark.parametrize(
    "crash_window",
    [
        "before_intent_write",
        "after_intent_write",
        "before_output_quarantine_rename",
        "after_output_quarantine_rename",
        "after_output_quarantine_fsync",
        "before_archive_restore_rename",
        "after_archive_restore_rename",
        "after_archive_restore_fsync",
        "after_state_replacement_write",
        "before_state_backup_rename",
        "after_state_backup_rename",
        "after_state_backup_fsync",
        "before_state_install_rename",
        "after_state_install_rename",
        "after_state_install_fsync",
        "before_marker_audit_finalization_rename",
        "after_marker_audit_finalization_rename",
        "after_marker_audit_finalization_fsync",
        "before_marker_audit_rebuild_rename",
        "after_marker_audit_rebuild_rename",
        "after_marker_audit_rebuild_fsync",
        "before_receipt_publish_rename",
        "after_receipt_publish_rename",
        "after_receipt_publish_fsync",
    ],
)
def test_rollback_converges_from_every_injected_crash_window(
    recovery_layout: dict[str, Path],
    crash_window: str,
):
    layout = recovery_layout
    _write_dataset(
        layout["output"],
        serials=[],
        validation_status="failed",
        identity="incomplete-output",
    )
    archive = layout["output_parent"] / ".task_a.rebuild-output-1"
    _write_dataset(
        archive,
        serials=RAW_SERIALS,
        validation_status="passed",
        identity="valid-archive",
    )
    _finalization_marker(layout)
    _rebuild_marker(layout, archive, phase="prepared")
    fired = False

    def crash_once(window: str) -> None:
        nonlocal fired
        if not fired and window == crash_window:
            fired = True
            raise RuntimeError(f"crash at {window}")

    with pytest.raises(RuntimeError, match="crash at"):
        _service(layout, crash_hook=crash_once).recover(CELL_TASK, "rollback")
    assert fired

    result = _service(layout).recover(CELL_TASK, "rollback")

    assert result["phase"] == "receipt_durable"
    assert layout["output"].is_dir()
    assert (
        layout["output_parent"] / result["paths"]["quarantine"]
    ).is_dir()
    assert all(not path.exists() for path in _active_artifacts(layout))
