from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

import scripts.partition_raw_by_contract as partition_module
from scripts.partition_raw_by_contract import apply_partition, rollback_partition


KEEP_CONTRACT = {"canonical_schema": {"version": 1}, "fixture": "keep"}
MOVE_CONTRACT = {"canonical_schema": {"version": 1}, "fixture": "move"}
OTHER_CONTRACT = {"canonical_schema": {"version": 1}, "fixture": "other"}


def _digest(contract: dict) -> str:
    encoded = json.dumps(
        contract,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


KEEP_DIGEST = _digest(KEEP_CONTRACT)
MOVE_DIGEST = _digest(MOVE_CONTRACT)
OTHER_DIGEST = _digest(OTHER_CONTRACT)
CONTRACTS = {
    KEEP_DIGEST: KEEP_CONTRACT,
    MOVE_DIGEST: MOVE_CONTRACT,
    OTHER_DIGEST: OTHER_CONTRACT,
}


def _recording(task: Path, serial: str, *, payload: bytes | None = None) -> Path:
    recording = task / serial
    recording.mkdir(parents=True)
    (recording / "metacard.json").write_text(
        json.dumps({"serial": serial}),
        encoding="utf-8",
    )
    (recording / f"{serial}_0.mcap").write_bytes(payload or serial.encode())
    return recording


def _manifest(
    path: Path,
    *,
    partitions: dict[str, list[str]],
    recording_digests: dict[str, str] | None = None,
) -> Path:
    recordings = recording_digests or {
        serial: digest
        for digest, serials in partitions.items()
        for serial in serials
    }
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
                "contract": CONTRACTS[digest],
                "digest": digest,
                "serials": serials,
            }
            for digest, serials in partitions.items()
        ],
        "recordings": [
            {"digest": digest, "serial": serial, "status": "resolved"}
            for serial, digest in sorted(recordings.items())
        ],
        "summary": {
            "invalid": 0,
            "partition_count": len(partitions),
            "resolved": len(recordings),
            "total": len(recordings),
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    return path


def _journal_path(tmp_path: Path) -> Path:
    parent = tmp_path / "private-journal"
    parent.mkdir()
    parent.chmod(0o700)
    return parent / "partition-journal.json"


def _basic_partition(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    source = tmp_path / "raw" / "task"
    _recording(source, "keep-a")
    _recording(source, "move-b")
    manifest = _manifest(
        tmp_path / "contract-partitions.json",
        partitions={
            KEEP_DIGEST: ["keep-a"],
            MOVE_DIGEST: ["move-b"],
        },
    )
    return (
        source,
        manifest,
        _journal_path(tmp_path),
        source.parent / "moved",
    )


def _snapshot(path: Path) -> dict[str, tuple[int, int, int, bytes | str | None]]:
    snapshot: dict[str, tuple[int, int, int, bytes | str | None]] = {}
    if not path.exists() and not path.is_symlink():
        return snapshot
    candidates = [path, *sorted(path.rglob("*"))]
    for candidate in candidates:
        info = candidate.lstat()
        if stat.S_ISREG(info.st_mode):
            payload: bytes | str | None = candidate.read_bytes()
        elif stat.S_ISLNK(info.st_mode):
            payload = os.readlink(candidate)
        else:
            payload = None
        snapshot[str(candidate.relative_to(path.parent))] = (
            info.st_mode,
            info.st_dev,
            info.st_ino,
            payload,
        )
    return snapshot


def test_apply_partition_keeps_selected_contract_and_moves_other_contract(
    tmp_path: Path,
):
    source, manifest, journal, destination = _basic_partition(tmp_path)

    result = apply_partition(
        source,
        manifest,
        journal,
        KEEP_DIGEST,
        {MOVE_DIGEST: destination},
    )

    assert result["phase"] == "committed"
    assert sorted(path.name for path in source.iterdir()) == ["keep-a"]
    assert sorted(path.name for path in destination.iterdir()) == ["move-b"]
    assert (destination / "move-b" / "move-b_0.mcap").read_bytes() == b"move-b"


def test_apply_partition_resumes_idempotently_after_injected_interruption(
    tmp_path: Path,
):
    source = tmp_path / "raw" / "task"
    for serial in ("keep-a", "move-b", "move-c"):
        _recording(source, serial)
    manifest = _manifest(
        tmp_path / "contract-partitions.json",
        partitions={
            KEEP_DIGEST: ["keep-a"],
            MOVE_DIGEST: ["move-b", "move-c"],
        },
    )
    journal = _journal_path(tmp_path)
    destination = source.parent / "moved"

    with pytest.raises(RuntimeError, match="injected|interrupt|fault"):
        apply_partition(
            source,
            manifest,
            journal,
            KEEP_DIGEST,
            {MOVE_DIGEST: destination},
            fault_after_moves=1,
        )

    result = apply_partition(
        source,
        manifest,
        journal,
        KEEP_DIGEST,
        {MOVE_DIGEST: destination},
    )
    repeated = apply_partition(
        source,
        manifest,
        journal,
        KEEP_DIGEST,
        {MOVE_DIGEST: destination},
    )

    assert result["phase"] == "committed"
    assert repeated == result
    assert sorted(path.name for path in source.iterdir()) == ["keep-a"]
    assert sorted(path.name for path in destination.iterdir()) == [
        "move-b",
        "move-c",
    ]


@pytest.mark.parametrize(
    "checkpoint",
    [
        "state-log-created",
        "bootstrap-durable",
        "journal-created",
        "journal-durable",
        "state-durable",
    ],
)
def test_apply_partition_resumes_each_bootstrap_crash_boundary(
    tmp_path: Path,
    monkeypatch,
    checkpoint: str,
):
    source, manifest, journal, destination = _basic_partition(tmp_path)
    original_checkpoint = partition_module._bootstrap_checkpoint

    def crash(selected: str):
        if selected == checkpoint:
            raise RuntimeError(f"injected bootstrap crash: {selected}")

    monkeypatch.setattr(
        partition_module,
        "_bootstrap_checkpoint",
        crash,
    )
    with pytest.raises(RuntimeError, match="bootstrap crash"):
        apply_partition(
            source,
            manifest,
            journal,
            KEEP_DIGEST,
            {MOVE_DIGEST: destination},
        )
    monkeypatch.setattr(
        partition_module,
        "_bootstrap_checkpoint",
        original_checkpoint,
    )

    result = apply_partition(
        source,
        manifest,
        journal,
        KEEP_DIGEST,
        {MOVE_DIGEST: destination},
    )

    assert result["phase"] == "committed"
    assert sorted(path.name for path in source.iterdir()) == ["keep-a"]
    assert sorted(path.name for path in destination.iterdir()) == ["move-b"]


def test_rollback_partition_restores_every_moved_recording(tmp_path: Path):
    source, manifest, journal, destination = _basic_partition(tmp_path)
    apply_partition(
        source,
        manifest,
        journal,
        KEEP_DIGEST,
        {MOVE_DIGEST: destination},
    )

    result = rollback_partition(source, journal)

    assert result["phase"] == "rolled_back"
    assert sorted(path.name for path in source.iterdir()) == ["keep-a", "move-b"]
    assert not destination.exists() or list(destination.iterdir()) == []


def test_cross_owner_artifact_authority_requires_journal_parent_owner(tmp_path: Path):
    source, manifest, journal, destination = _basic_partition(tmp_path)
    source_uid = source.stat().st_uid
    source_gid = source.stat().st_gid
    with pytest.raises(PermissionError, match="private file owner|journal parent"):
        apply_partition(
            source,
            manifest,
            journal,
            KEEP_DIGEST,
            {MOVE_DIGEST: destination},
            artifact_uid=source_uid + 1,
            artifact_gid=source_gid,
        )
    assert not journal.exists()


def test_apply_partition_rejects_manifest_that_omits_a_source_recording(
    tmp_path: Path,
):
    source = tmp_path / "raw" / "task"
    _recording(source, "recorded")
    _recording(source, "omitted")
    manifest = _manifest(
        tmp_path / "contract-partitions.json",
        partitions={KEEP_DIGEST: ["recorded"]},
    )
    journal = _journal_path(tmp_path)
    before = _snapshot(source)

    with pytest.raises((RuntimeError, ValueError), match="exact|omitted|source"):
        apply_partition(source, manifest, journal, KEEP_DIGEST, {})

    assert _snapshot(source) == before
    assert not journal.exists()


def test_apply_partition_rejects_manifest_recording_missing_from_source(
    tmp_path: Path,
):
    source = tmp_path / "raw" / "task"
    _recording(source, "present")
    manifest = _manifest(
        tmp_path / "contract-partitions.json",
        partitions={KEEP_DIGEST: ["missing", "present"]},
    )
    journal = _journal_path(tmp_path)
    before = _snapshot(source)

    with pytest.raises((RuntimeError, ValueError), match="exact|missing|source"):
        apply_partition(source, manifest, journal, KEEP_DIGEST, {})

    assert _snapshot(source) == before
    assert not journal.exists()


def test_apply_partition_rejects_overlapping_contract_partitions(tmp_path: Path):
    source = tmp_path / "raw" / "task"
    _recording(source, "duplicate")
    manifest = _manifest(
        tmp_path / "contract-partitions.json",
        partitions={
            KEEP_DIGEST: ["duplicate"],
            MOVE_DIGEST: ["duplicate"],
        },
        recording_digests={"duplicate": KEEP_DIGEST},
    )
    journal = _journal_path(tmp_path)
    before = _snapshot(source)

    with pytest.raises((RuntimeError, ValueError), match="disjoint|duplicate|overlap"):
        apply_partition(
            source,
            manifest,
            journal,
            KEEP_DIGEST,
            {MOVE_DIGEST: source.parent / "moved"},
        )

    assert _snapshot(source) == before
    assert not journal.exists()


def test_apply_partition_rejects_symlink_recording_without_following_it(
    tmp_path: Path,
):
    source = tmp_path / "raw" / "task"
    source.mkdir(parents=True)
    outside = _recording(tmp_path / "outside", "linked")
    (source / "linked").symlink_to(outside, target_is_directory=True)
    manifest = _manifest(
        tmp_path / "contract-partitions.json",
        partitions={KEEP_DIGEST: ["linked"]},
    )
    journal = _journal_path(tmp_path)
    outside_before = _snapshot(outside)

    with pytest.raises((RuntimeError, ValueError), match="symlink|regular|directory"):
        apply_partition(source, manifest, journal, KEEP_DIGEST, {})

    assert (source / "linked").is_symlink()
    assert _snapshot(outside) == outside_before
    assert not journal.exists()


def test_apply_partition_rejects_fifo_inside_recording_without_blocking(
    tmp_path: Path,
):
    source = tmp_path / "raw" / "task"
    recording = _recording(source, "fifo")
    fifo = recording / "unexpected.pipe"
    os.mkfifo(fifo)
    manifest = _manifest(
        tmp_path / "contract-partitions.json",
        partitions={KEEP_DIGEST: ["fifo"]},
    )
    journal = _journal_path(tmp_path)

    with pytest.raises((RuntimeError, ValueError), match="regular|fifo|file"):
        apply_partition(source, manifest, journal, KEEP_DIGEST, {})

    assert stat.S_ISFIFO(fifo.lstat().st_mode)
    assert not journal.exists()


def test_apply_partition_rejects_fifo_lock_without_blocking_or_mutating(
    tmp_path: Path,
):
    source, manifest, journal, destination = _basic_partition(tmp_path)
    lock = source.with_name(f".{source.name}.contract-partition.lock")
    os.mkfifo(lock, 0o640)
    before_mode = stat.S_IMODE(lock.lstat().st_mode)

    with pytest.raises((RuntimeError, OSError), match="lock|regular"):
        apply_partition(
            source,
            manifest,
            journal,
            KEEP_DIGEST,
            {MOVE_DIGEST: destination},
        )

    assert stat.S_ISFIFO(lock.lstat().st_mode)
    assert stat.S_IMODE(lock.lstat().st_mode) == before_mode
    assert not journal.exists()


def test_resume_rejects_source_recording_replaced_after_interruption(
    tmp_path: Path,
):
    source = tmp_path / "raw" / "task"
    for serial in ("keep-a", "move-b", "move-c"):
        _recording(source, serial)
    manifest = _manifest(
        tmp_path / "contract-partitions.json",
        partitions={
            KEEP_DIGEST: ["keep-a"],
            MOVE_DIGEST: ["move-b", "move-c"],
        },
    )
    journal = _journal_path(tmp_path)
    destination = source.parent / "moved"

    with pytest.raises(RuntimeError):
        apply_partition(
            source,
            manifest,
            journal,
            KEEP_DIGEST,
            {MOVE_DIGEST: destination},
            fault_after_moves=1,
        )
    pending = next(
        serial
        for serial in ("move-b", "move-c")
        if (source / serial).exists()
    )
    original = source / pending
    replacement = source / f".{pending}.replacement"
    _recording(source, f".{pending}.replacement", payload=b"replacement")
    original.rename(source / f".{pending}.original")
    replacement.rename(original)
    before_resume = _snapshot(source)

    with pytest.raises((RuntimeError, ValueError), match="changed|identity|replaced"):
        apply_partition(
            source,
            manifest,
            journal,
            KEEP_DIGEST,
            {MOVE_DIGEST: destination},
        )

    assert _snapshot(source) == before_resume


def test_apply_partition_never_clobbers_existing_destination_recording(
    tmp_path: Path,
):
    source, manifest, journal, destination = _basic_partition(tmp_path)
    _recording(destination, "move-b", payload=b"destination")
    source_before = _snapshot(source)
    destination_before = _snapshot(destination)

    with pytest.raises((FileExistsError, RuntimeError), match="exist|clobber"):
        apply_partition(
            source,
            manifest,
            journal,
            KEEP_DIGEST,
            {MOVE_DIGEST: destination},
        )

    assert _snapshot(source) == source_before
    assert _snapshot(destination) == destination_before
    assert not journal.exists()


def test_apply_and_rollback_preserve_hidden_source_entries(tmp_path: Path):
    source, manifest, journal, destination = _basic_partition(tmp_path)
    reservation = source / ".robodata-reservation-test"
    reservation.write_text("reserved", encoding="utf-8")
    quarantine = source / ".conversion-quarantine-test"
    quarantine.mkdir()
    (quarantine / "failed.mcap").write_bytes(b"forensic")
    hidden_before = {
        reservation.name: _snapshot(reservation),
        quarantine.name: _snapshot(quarantine),
    }

    apply_partition(
        source,
        manifest,
        journal,
        KEEP_DIGEST,
        {MOVE_DIGEST: destination},
    )
    rollback_partition(source, journal)

    assert _snapshot(reservation) == hidden_before[reservation.name]
    assert _snapshot(quarantine) == hidden_before[quarantine.name]


def test_apply_partition_requires_private_contract_manifest(tmp_path: Path):
    source = tmp_path / "raw" / "task"
    _recording(source, "keep-a")
    manifest = _manifest(
        tmp_path / "contract-partitions.json",
        partitions={KEEP_DIGEST: ["keep-a"]},
    )
    manifest.chmod(0o644)
    journal = _journal_path(tmp_path)
    before = _snapshot(source)

    with pytest.raises((PermissionError, RuntimeError, ValueError), match="0600|mode|private"):
        apply_partition(source, manifest, journal, KEEP_DIGEST, {})

    assert stat.S_IMODE(manifest.stat().st_mode) == 0o644
    assert _snapshot(source) == before
    assert not journal.exists()


def test_apply_partition_rejects_contract_digest_tampering(tmp_path: Path):
    source, manifest, journal, destination = _basic_partition(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["partitions"][0]["contract"]["fixture"] = "tampered"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    manifest.chmod(0o600)
    before = _snapshot(source)

    with pytest.raises((RuntimeError, ValueError), match="digest|contract"):
        apply_partition(
            source,
            manifest,
            journal,
            KEEP_DIGEST,
            {MOVE_DIGEST: destination},
        )

    assert _snapshot(source) == before
    assert not journal.exists()


def test_apply_partition_rejects_manifest_symlink_without_following(
    tmp_path: Path,
):
    source, manifest, journal, destination = _basic_partition(tmp_path)
    real_manifest = manifest.with_name("real-contract.json")
    manifest.rename(real_manifest)
    manifest.symlink_to(real_manifest)
    before = _snapshot(source)

    with pytest.raises(OSError):
        apply_partition(
            source,
            manifest,
            journal,
            KEEP_DIGEST,
            {MOVE_DIGEST: destination},
        )

    assert _snapshot(source) == before
    assert not journal.exists()


def test_apply_partition_rejects_manifest_fifo_without_blocking(tmp_path: Path):
    source, manifest, journal, destination = _basic_partition(tmp_path)
    manifest.unlink()
    os.mkfifo(manifest)
    before = _snapshot(source)

    with pytest.raises((OSError, RuntimeError, ValueError)):
        apply_partition(
            source,
            manifest,
            journal,
            KEEP_DIGEST,
            {MOVE_DIGEST: destination},
        )

    assert stat.S_ISFIFO(manifest.lstat().st_mode)
    assert _snapshot(source) == before
    assert not journal.exists()


def test_resume_rejects_replaced_private_journal(tmp_path: Path):
    source = tmp_path / "raw" / "task"
    for serial in ("keep-a", "move-b", "move-c"):
        _recording(source, serial)
    manifest = _manifest(
        tmp_path / "contract-partitions.json",
        partitions={
            KEEP_DIGEST: ["keep-a"],
            MOVE_DIGEST: ["move-b", "move-c"],
        },
    )
    journal = _journal_path(tmp_path)
    destination = source.parent / "moved"
    with pytest.raises(RuntimeError):
        apply_partition(
            source,
            manifest,
            journal,
            KEEP_DIGEST,
            {MOVE_DIGEST: destination},
            fault_after_moves=1,
        )
    real_journal = journal.with_name("real-journal.json")
    journal.rename(real_journal)
    journal.symlink_to(real_journal)
    before = {
        "source": _snapshot(source),
        "destination": _snapshot(destination),
    }

    with pytest.raises(OSError):
        apply_partition(
            source,
            manifest,
            journal,
            KEEP_DIGEST,
            {MOVE_DIGEST: destination},
        )

    assert _snapshot(source) == before["source"]
    assert _snapshot(destination) == before["destination"]


def test_apply_preserves_preexisting_invalid_journal_without_bootstrap(
    tmp_path: Path,
):
    source, manifest, journal, destination = _basic_partition(tmp_path)
    foreign_bytes = b"foreign journal bytes that are not JSON\n"
    journal.write_bytes(foreign_bytes)
    journal.chmod(0o600)
    before = _snapshot(source)

    with pytest.raises(
        (FileExistsError, RuntimeError),
        match="journal|bootstrap|exist",
    ):
        apply_partition(
            source,
            manifest,
            journal,
            KEEP_DIGEST,
            {MOVE_DIGEST: destination},
        )

    assert journal.read_bytes() == foreign_bytes
    assert _snapshot(source) == before
    assert not destination.exists()
    assert not journal.with_name(f".{journal.name}.state-log").exists()


def test_resume_rejects_identical_bytes_in_replacement_journal(
    tmp_path: Path,
):
    source = tmp_path / "raw" / "task"
    for serial in ("keep-a", "move-b", "move-c"):
        _recording(source, serial)
    manifest = _manifest(
        tmp_path / "contract-partitions.json",
        partitions={
            KEEP_DIGEST: ["keep-a"],
            MOVE_DIGEST: ["move-b", "move-c"],
        },
    )
    journal = _journal_path(tmp_path)
    destination = source.parent / "moved"
    with pytest.raises(RuntimeError):
        apply_partition(
            source,
            manifest,
            journal,
            KEEP_DIGEST,
            {MOVE_DIGEST: destination},
            fault_after_moves=1,
        )
    journal_bytes = journal.read_bytes()
    replaced = journal.with_name("replaced-journal.json")
    journal.rename(replaced)
    journal.write_bytes(journal_bytes)
    journal.chmod(0o600)
    before = {
        "source": _snapshot(source),
        "destination": _snapshot(destination),
    }

    with pytest.raises(RuntimeError, match="journal|inode|authority"):
        apply_partition(
            source,
            manifest,
            journal,
            KEEP_DIGEST,
            {MOVE_DIGEST: destination},
        )

    assert _snapshot(source) == before["source"]
    assert _snapshot(destination) == before["destination"]


def test_resume_rejects_replacement_state_log(tmp_path: Path):
    source = tmp_path / "raw" / "task"
    for serial in ("keep-a", "move-b", "move-c"):
        _recording(source, serial)
    manifest = _manifest(
        tmp_path / "contract-partitions.json",
        partitions={
            KEEP_DIGEST: ["keep-a"],
            MOVE_DIGEST: ["move-b", "move-c"],
        },
    )
    journal = _journal_path(tmp_path)
    destination = source.parent / "moved"
    with pytest.raises(RuntimeError):
        apply_partition(
            source,
            manifest,
            journal,
            KEEP_DIGEST,
            {MOVE_DIGEST: destination},
            fault_after_moves=1,
        )
    state_log = journal.with_name(f".{journal.name}.state-log")
    state_bytes = state_log.read_bytes()
    state_log.rename(state_log.with_name(f"{state_log.name}.replaced"))
    state_log.write_bytes(state_bytes)
    state_log.chmod(0o600)
    before = {
        "source": _snapshot(source),
        "destination": _snapshot(destination),
    }

    with pytest.raises(RuntimeError, match="state log|inode|binding"):
        apply_partition(
            source,
            manifest,
            journal,
            KEEP_DIGEST,
            {MOVE_DIGEST: destination},
        )

    assert _snapshot(source) == before["source"]
    assert _snapshot(destination) == before["destination"]


def test_resume_truncates_partial_state_record_before_append(tmp_path: Path):
    source = tmp_path / "raw" / "task"
    for serial in ("keep-a", "move-b", "move-c"):
        _recording(source, serial)
    manifest = _manifest(
        tmp_path / "contract-partitions.json",
        partitions={
            KEEP_DIGEST: ["keep-a"],
            MOVE_DIGEST: ["move-b", "move-c"],
        },
    )
    journal = _journal_path(tmp_path)
    destination = source.parent / "moved"
    with pytest.raises(RuntimeError):
        apply_partition(
            source,
            manifest,
            journal,
            KEEP_DIGEST,
            {MOVE_DIGEST: destination},
            fault_after_moves=1,
        )
    state_log = journal.with_name(f".{journal.name}.state-log")
    with state_log.open("ab") as stream:
        stream.write(b'{"kind":"state","payload":')
        stream.flush()
        os.fsync(stream.fileno())

    result = apply_partition(
        source,
        manifest,
        journal,
        KEEP_DIGEST,
        {MOVE_DIGEST: destination},
    )

    assert result["phase"] == "committed"
    assert state_log.read_bytes().endswith(b"\n")
    assert sorted(path.name for path in source.iterdir()) == ["keep-a"]
    assert sorted(path.name for path in destination.iterdir()) == [
        "move-b",
        "move-c",
    ]


def test_state_log_path_swap_during_append_fails_before_another_raw_move(
    tmp_path: Path,
    monkeypatch,
):
    source = tmp_path / "raw" / "task"
    for serial in ("keep-a", "move-b", "move-c"):
        _recording(source, serial)
    manifest = _manifest(
        tmp_path / "contract-partitions.json",
        partitions={
            KEEP_DIGEST: ["keep-a"],
            MOVE_DIGEST: ["move-b", "move-c"],
        },
    )
    journal = _journal_path(tmp_path)
    destination = source.parent / "moved"
    with pytest.raises(RuntimeError):
        apply_partition(
            source,
            manifest,
            journal,
            KEEP_DIGEST,
            {MOVE_DIGEST: destination},
            fault_after_moves=1,
        )
    state_log = journal.with_name(f".{journal.name}.state-log")
    original_write_all = partition_module._write_all
    swapped = False

    def swap_visible_state_log(descriptor: int, payload: bytes):
        nonlocal swapped
        if not swapped and b'"kind":"state"' in payload:
            swapped = True
            previous = state_log.with_name(f"{state_log.name}.previous")
            state_bytes = state_log.read_bytes()
            state_log.rename(previous)
            state_log.write_bytes(state_bytes)
            state_log.chmod(0o600)
        original_write_all(descriptor, payload)

    monkeypatch.setattr(
        partition_module,
        "_write_all",
        swap_visible_state_log,
    )
    before = {
        "source": _snapshot(source),
        "destination": _snapshot(destination),
    }

    with pytest.raises(RuntimeError, match="path changed|state-log"):
        apply_partition(
            source,
            manifest,
            journal,
            KEEP_DIGEST,
            {MOVE_DIGEST: destination},
        )

    assert swapped is True
    assert _snapshot(source) == before["source"]
    assert _snapshot(destination) == before["destination"]


def test_apply_resumes_empty_owned_construction_after_marker_create_crash(
    tmp_path: Path,
    monkeypatch,
):
    source, manifest, journal, destination = _basic_partition(tmp_path)
    original_create_marker = partition_module._create_destination_marker

    def crash_before_marker(*args, **kwargs):
        raise RuntimeError("injected marker creation crash")

    monkeypatch.setattr(
        partition_module,
        "_create_destination_marker",
        crash_before_marker,
    )
    with pytest.raises(RuntimeError, match="marker creation crash"):
        apply_partition(
            source,
            manifest,
            journal,
            KEEP_DIGEST,
            {MOVE_DIGEST: destination},
        )
    construction = next(
        entry
        for entry in source.parent.iterdir()
        if ".contract-partition-construction-" in entry.name
    )
    assert construction.is_dir()
    assert list(construction.iterdir()) == []
    monkeypatch.setattr(
        partition_module,
        "_create_destination_marker",
        original_create_marker,
    )

    result = apply_partition(
        source,
        manifest,
        journal,
        KEEP_DIGEST,
        {MOVE_DIGEST: destination},
    )

    assert result["phase"] == "committed"
    assert sorted(path.name for path in source.iterdir()) == ["keep-a"]
    assert sorted(path.name for path in destination.iterdir()) == ["move-b"]


def test_rollback_adopts_published_destination_before_journal_binding(
    tmp_path: Path,
    monkeypatch,
):
    source, manifest, journal, destination = _basic_partition(tmp_path)
    original_write_journal = partition_module._write_journal

    def crash_before_binding(*args, **kwargs):
        if kwargs["expected_identity"] is not None:
            raise RuntimeError("injected destination binding crash")
        return original_write_journal(*args, **kwargs)

    monkeypatch.setattr(
        partition_module,
        "_write_journal",
        crash_before_binding,
    )
    with pytest.raises(RuntimeError, match="destination binding crash"):
        apply_partition(
            source,
            manifest,
            journal,
            KEEP_DIGEST,
            {MOVE_DIGEST: destination},
        )
    assert destination.is_dir()
    monkeypatch.setattr(
        partition_module,
        "_write_journal",
        original_write_journal,
    )

    result = rollback_partition(source, journal)

    assert result["phase"] == "rolled_back"
    assert sorted(path.name for path in source.iterdir()) == [
        "keep-a",
        "move-b",
    ]
    assert list(destination.iterdir()) == []


def test_resume_rejects_destination_marker_replacement(tmp_path: Path):
    source = tmp_path / "raw" / "task"
    for serial in ("keep-a", "move-b", "move-c"):
        _recording(source, serial)
    manifest = _manifest(
        tmp_path / "contract-partitions.json",
        partitions={
            KEEP_DIGEST: ["keep-a"],
            MOVE_DIGEST: ["move-b", "move-c"],
        },
    )
    journal = _journal_path(tmp_path)
    destination = source.parent / "moved"
    with pytest.raises(RuntimeError):
        apply_partition(
            source,
            manifest,
            journal,
            KEEP_DIGEST,
            {MOVE_DIGEST: destination},
            fault_after_moves=1,
        )
    marker = next(
        entry for entry in destination.iterdir() if entry.name.startswith(".")
    )
    marker.unlink()
    marker.write_bytes(b"")
    marker.chmod(0o600)
    before = {
        "source": _snapshot(source),
        "destination": _snapshot(destination),
    }

    with pytest.raises((RuntimeError, ValueError), match="marker|identity"):
        apply_partition(
            source,
            manifest,
            journal,
            KEEP_DIGEST,
            {MOVE_DIGEST: destination},
        )

    assert _snapshot(source) == before["source"]
    assert _snapshot(destination) == before["destination"]


def test_apply_partition_resumes_after_finalizing_crash(
    tmp_path: Path,
    monkeypatch,
):
    source, manifest, journal, destination = _basic_partition(tmp_path)
    original_finalize = partition_module._finalize_destinations

    def finalize_then_crash(payload):
        original_finalize(payload)
        raise RuntimeError("injected finalizing crash")

    monkeypatch.setattr(
        partition_module,
        "_finalize_destinations",
        finalize_then_crash,
    )
    with pytest.raises(RuntimeError, match="finalizing crash"):
        apply_partition(
            source,
            manifest,
            journal,
            KEEP_DIGEST,
            {MOVE_DIGEST: destination},
        )
    monkeypatch.setattr(
        partition_module,
        "_finalize_destinations",
        original_finalize,
    )

    result = apply_partition(
        source,
        manifest,
        journal,
        KEEP_DIGEST,
        {MOVE_DIGEST: destination},
    )

    assert result["phase"] == "committed"
    assert sorted(path.name for path in source.iterdir()) == ["keep-a"]
    assert sorted(path.name for path in destination.iterdir()) == ["move-b"]
