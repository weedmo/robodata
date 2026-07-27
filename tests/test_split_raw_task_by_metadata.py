import errno
import json
import multiprocessing
import os
import stat
from pathlib import Path

import pytest

from scripts import split_raw_task_by_metadata as split_module
from scripts.split_raw_task_by_metadata import (
    apply_split,
    apply_split_as_symlink_view,
    build_split_plan,
    materialize_symlink_view_as_hardlinks,
)


def test_materialization_unsupported_nfs_never_uses_plain_rename(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.write_bytes(b"preserve-source")
    destination = tmp_path / "destination"
    source_info = source.stat(follow_symlinks=False)
    rename_calls = []

    class UnsupportedRenameAt2:
        def __call__(self, *args):
            split_module.ctypes.set_errno(errno.EOPNOTSUPP)
            return -1

    class UnsupportedLibC:
        renameat2 = UnsupportedRenameAt2()

    def forbidden_plain_rename(*args, **kwargs):
        rename_calls.append((args, kwargs))
        raise AssertionError("plain os.rename must not be used")

    monkeypatch.setattr(split_module.ctypes, "CDLL", lambda *args, **kwargs: UnsupportedLibC())
    monkeypatch.setattr(
        split_module,
        "_filesystem_type",
        lambda descriptor: split_module._NFS_SUPER_MAGIC,
    )
    monkeypatch.setattr(split_module.os, "rename", forbidden_plain_rename)
    monkeypatch.setenv("CURATION_RECOVERY_ISOLATED", "true")
    parent_descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises((OSError, RuntimeError)):
            split_module._rename_noreplace(
                parent_descriptor,
                source.name,
                parent_descriptor,
                destination.name,
            )
    finally:
        os.close(parent_descriptor)

    assert rename_calls == []
    assert source.read_bytes() == b"preserve-source"
    assert (source.stat().st_dev, source.stat().st_ino) == (
        source_info.st_dev,
        source_info.st_ino,
    )
    assert not destination.exists()


def _recording(root: Path, serial: str, robot: str, action_topics: list[str]) -> None:
    directory = root / serial
    directory.mkdir(parents=True)
    (directory / "metacard.json").write_text(
        json.dumps({
            "robot_type": robot,
            "fps": 30,
            "joint_names": ["j0"],
            "action_topics_map": {name: f"/{name}" for name in action_topics},
            "camera_topic_map": {"cam_head": "/cam/head"},
        }),
        encoding="utf-8",
    )
    (directory / f"{serial}_0.mcap").write_bytes(b"mcap")


def _materialization_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "cell007" / "task"
    _recording(source, "A", "robot-a", ["left", "right"])
    plan = build_split_plan(source)
    backing = source.with_name(".task__metadata_source_20260721")
    apply_split_as_symlink_view(plan, backing)
    return source, backing, tmp_path / "materialize.json"


def _hybrid_materialization_fixture(
    tmp_path: Path,
) -> tuple[Path, Path, Path, dict[str, Path]]:
    source = tmp_path / "cell006" / "task"
    backing = source.with_name(".task__metadata_source_20260721")
    plain_serial = source / "A"
    backing_plain_serial = backing / "A"
    backing_linked_serial = backing / "B"
    quarantine = source / ".conversion-quarantine-stale"
    for directory in (
        plain_serial,
        backing_plain_serial,
        backing_linked_serial,
        quarantine,
    ):
        directory.mkdir(parents=True)

    plain_metacard = plain_serial / "metacard.json"
    plain_metacard.write_text('{"robot_type":"rby1"}', encoding="utf-8")
    backing_plain_mcap = backing_plain_serial / "A_0.mcap"
    backing_plain_mcap.write_bytes(b"plain-serial-mcap")
    backing_plain_metadata = backing_plain_serial / "metadata.yaml"
    backing_plain_metadata.write_text("fps: 30\n", encoding="utf-8")
    (plain_serial / "A_0.mcap").symlink_to(backing_plain_mcap)
    (plain_serial / "metadata.yaml").symlink_to(backing_plain_metadata)

    linked_metacard = backing_linked_serial / "metacard.json"
    linked_metacard.write_text('{"robot_type":"rby1"}', encoding="utf-8")
    linked_mcap = backing_linked_serial / "B_0.mcap"
    linked_mcap.write_bytes(b"linked-serial-mcap")
    linked_metadata = backing_linked_serial / "metadata.yaml"
    linked_metadata.write_text("fps: 30\n", encoding="utf-8")
    (source / "B").symlink_to(backing_linked_serial, target_is_directory=True)

    quarantine_file = quarantine / "failed.mcap"
    quarantine_file.write_bytes(b"quarantined-original")
    return source, backing, tmp_path / "materialize.json", {
        "plain_metacard": plain_metacard,
        "backing_plain_mcap": backing_plain_mcap,
        "backing_plain_metadata": backing_plain_metadata,
        "linked_metacard": linked_metacard,
        "linked_mcap": linked_mcap,
        "linked_metadata": linked_metadata,
        "quarantine": quarantine,
        "quarantine_file": quarantine_file,
    }


def _detached_destination(tmp_path: Path) -> Path:
    parent = tmp_path / "detached-private"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    return parent / "task-detached"


def _hybrid_snapshot(source: Path, backing: Path) -> dict[str, tuple]:
    paths = [source, backing]
    for root in (source, backing):
        for child in root.iterdir():
            paths.append(child)
            if child.is_dir() and not child.is_symlink():
                paths.extend(child.iterdir())
    snapshot = {}
    for path in paths:
        info = path.lstat()
        payload = (
            os.readlink(path)
            if path.is_symlink()
            else path.read_bytes()
            if stat.S_ISREG(info.st_mode)
            else None
        )
        snapshot[str(path)] = (
            info.st_mode,
            info.st_dev,
            info.st_ino,
            info.st_size,
            payload,
        )
    return snapshot


def _crash_detached_materialization(
    source: Path,
    backing: Path,
    destination: Path,
    manifest_path: Path,
) -> None:
    real_link = split_module.os.link

    def link_then_crash(src, dst, **kwargs):
        real_link(src, dst, **kwargs)
        os._exit(111)

    split_module.os.link = link_then_crash
    split_module.materialize_link_view_detached_as_hardlinks(
        source,
        backing_source=backing,
        destination_task=destination,
        manifest_path=manifest_path,
    )


def _hybrid_source_identities(origins: dict[str, Path]) -> dict[str, tuple[int, int]]:
    return {
        relative_path: (path.stat().st_dev, path.stat().st_ino)
        for relative_path, path in {
            "A/metacard.json": origins["plain_metacard"],
            "A/A_0.mcap": origins["backing_plain_mcap"],
            "A/metadata.yaml": origins["backing_plain_metadata"],
            "B/metacard.json": origins["linked_metacard"],
            "B/B_0.mcap": origins["linked_mcap"],
            "B/metadata.yaml": origins["linked_metadata"],
        }.items()
    }


def _assert_hybrid_materialized(
    source: Path,
    manifest: dict,
    expected_identities: dict[str, tuple[int, int]],
    quarantine_identity: tuple[int, int],
) -> None:
    for serial in ("A", "B"):
        assert (source / serial).is_dir() and not (source / serial).is_symlink()
    for relative_path, expected_identity in expected_identities.items():
        visible = source / relative_path
        assert visible.is_file() and not visible.is_symlink()
        assert (visible.stat().st_dev, visible.stat().st_ino) == expected_identity
    quarantine_name = manifest["preserved_entries"][0]["name"]
    assert not (source / quarantine_name).exists()
    rollback_quarantine = Path(manifest["rollback_view"]) / quarantine_name
    assert (
        rollback_quarantine.stat().st_dev,
        rollback_quarantine.stat().st_ino,
    ) == quarantine_identity


def _legacy_v1_manifest(manifest: dict) -> dict:
    legacy = json.loads(json.dumps(manifest))
    legacy["version"] = 1
    legacy.pop("preserved_entries", None)
    for recording in legacy["recordings"]:
        recording.pop("source_kind", None)
        recording.pop("source_device", None)
        recording.pop("source_inode", None)
        for source_file in recording["files"]:
            source_file.pop("source_kind", None)
            source_file.pop("view_device", None)
            source_file.pop("view_inode", None)
            source_file.pop("view_mode", None)
            source_file.pop("source_parent_device", None)
            source_file.pop("source_parent_inode", None)
    return legacy


def _crash_materialization(
    source: Path,
    backing: Path,
    manifest_path: Path,
    window: str,
) -> None:
    real_rename = split_module._rename_materialization_noreplace
    real_link = split_module.os.link
    real_open = split_module.os.open
    real_write_manifest = split_module.write_manifest

    def rename_then_crash(src, dst):
        real_rename(src, dst)
        source_path = Path(src)
        destination_path = Path(dst)
        if (
            window == "after_archive"
            and destination_path.name.startswith(".task.symlink-view-rollback-")
        ):
            os._exit(91)
        if (
            window == "after_install"
            and source_path.name.startswith(".task.hardlink-staging-")
            and destination_path == source
        ):
            os._exit(92)
        if (
            window == "after_quarantine_move"
            and ".hardlink-quarantine-" in destination_path.name
        ):
            os._exit(99)

    def link_then_crash(src, dst, **kwargs):
        real_link(src, dst, **kwargs)
        if window == "partial_staging":
            os._exit(93)

    def write_then_maybe_crash(path, plan):
        if window == "before_quarantine_move" and plan.get(
            "phase"
        ) == "staging_quarantining":
            real_write_manifest(path, plan)
            os._exit(98)
        if plan.get("phase") == "preparing":
            durable_phase = (
                json.loads(Path(path).read_text(encoding="utf-8"))["phase"]
                if Path(path).exists()
                else None
            )
            if window == "before_initial_identity" and durable_phase == "reserved":
                os._exit(94)
            if (
                window == "before_replacement_identity"
                and durable_phase == "staging_replacing"
            ):
                os._exit(95)
        real_write_manifest(path, plan)

    def open_then_maybe_crash(path, flags, mode=0o777, *, dir_fd=None):
        if (
            window == "before_construction_marker"
            and str(path).startswith(".robodata-reservation-")
            and flags & os.O_CREAT
        ):
            os._exit(96)
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if (
            window == "after_construction_marker"
            and str(path).startswith(".robodata-reservation-")
            and flags & os.O_CREAT
        ):
            os._exit(97)
        return descriptor

    split_module._rename_materialization_noreplace = rename_then_crash
    split_module.os.link = link_then_crash
    split_module.os.open = open_then_maybe_crash
    split_module.write_manifest = write_then_maybe_crash
    materialize_symlink_view_as_hardlinks(
        source,
        backing_source=backing,
        manifest_path=manifest_path,
    )


def _assert_materialized(source: Path, backing: Path, manifest: dict) -> None:
    assert manifest["phase"] == "committed"
    rollback_view = Path(manifest["rollback_view"])
    assert rollback_view.is_dir() and not rollback_view.is_symlink()
    assert all(path.is_symlink() for path in rollback_view.iterdir())
    assert not list(source.parent.glob(".task.hardlink-staging-*"))
    for quarantine in manifest["staging_quarantines"]:
        path = Path(quarantine["path"])
        assert path.is_dir()
        assert (path.stat().st_dev, path.stat().st_ino) == (
            quarantine["device"],
            quarantine["inode"],
        )
    for recording in manifest["recordings"]:
        serial = recording["serial"]
        for source_file in recording["files"]:
            visible = source / serial / source_file["name"]
            original = backing / serial / source_file["name"]
            assert (visible.stat().st_dev, visible.stat().st_ino) == (
                original.stat().st_dev,
                original.stat().st_ino,
            )


def test_split_plan_keeps_largest_group_and_apply_moves_only_other_groups(tmp_path):
    source = tmp_path / "cell007" / "task"
    _recording(source, "A", "robot-a", ["left", "right"])
    _recording(source, "B", "robot-a", ["left", "right"])
    _recording(source, "C", "robot-a", ["left", "right", "vacuum"])
    _recording(source, "D", "robot-b", ["left", "right"])

    plan = build_split_plan(source)

    assert [group["count"] for group in plan["groups"]] == [2, 1, 1]
    assert plan["groups"][0]["keep_in_source"] is True
    assert sorted(path.name for path in source.iterdir()) == ["A", "B", "C", "D"]

    assert apply_split(plan) == 2
    assert sorted(path.name for path in source.iterdir()) == ["A", "B"]
    moved_groups = [group for group in plan["groups"] if not group["keep_in_source"]]
    assert sorted(
        path.name
        for group in moved_groups
        for path in Path(group["destination"]).iterdir()
    ) == ["C", "D"]


def test_symlink_view_hides_mixed_source_and_exposes_every_group(tmp_path):
    source = tmp_path / "cell007" / "task"
    _recording(source, "A", "robot-a", ["left", "right"])
    _recording(source, "B", "robot-a", ["left", "right"])
    _recording(source, "C", "robot-a", ["left", "right", "vacuum"])
    _recording(source, "D", "robot-b", ["left", "right"])
    plan = build_split_plan(source)
    backing = source.with_name(".task__metadata_source_20260721")

    assert apply_split_as_symlink_view(plan, backing) == 4

    assert sorted(path.name for path in backing.iterdir()) == ["A", "B", "C", "D"]
    assert all(path.is_dir() and path.is_symlink() for path in source.iterdir())
    assert len(list(source.iterdir())) == 2
    for group in plan["groups"]:
        destination = Path(group["destination"])
        assert len(list(destination.iterdir())) == group["count"]
        assert build_split_plan(destination)["invalid"] == []


def test_materialize_symlink_view_builds_plain_hardlink_recordings(tmp_path):
    source = tmp_path / "cell007" / "task"
    _recording(source, "A", "robot-a", ["left", "right"])
    _recording(source, "B", "robot-a", ["left", "right"])
    plan = build_split_plan(source)
    backing = source.with_name(".task__metadata_source_20260721")
    apply_split_as_symlink_view(plan, backing)
    manifest_path = tmp_path / "materialize.json"

    manifest = materialize_symlink_view_as_hardlinks(
        source,
        backing_source=backing,
        manifest_path=manifest_path,
    )

    assert manifest["phase"] == "committed"
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["phase"] == "committed"
    rollback_view = Path(manifest["rollback_view"])
    assert rollback_view.is_dir() and not rollback_view.is_symlink()
    assert all(path.is_symlink() for path in rollback_view.iterdir())
    for serial in ("A", "B"):
        visible_recording = source / serial
        backing_recording = backing / serial
        assert visible_recording.is_dir() and not visible_recording.is_symlink()
        for filename in ("metacard.json", f"{serial}_0.mcap"):
            visible = visible_recording / filename
            original = backing_recording / filename
            assert visible.is_file() and not visible.is_symlink()
            assert (visible.stat().st_dev, visible.stat().st_ino) == (
                original.stat().st_dev,
                original.stat().st_ino,
            )


def test_materialize_hybrid_view_builds_plain_recordings_from_each_source_kind(
    tmp_path,
):
    source, backing, manifest_path, origins = _hybrid_materialization_fixture(
        tmp_path
    )
    expected_sources = {
        ("A", "metacard.json"): origins["plain_metacard"],
        ("A", "A_0.mcap"): origins["backing_plain_mcap"],
        ("A", "metadata.yaml"): origins["backing_plain_metadata"],
        ("B", "metacard.json"): origins["linked_metacard"],
        ("B", "B_0.mcap"): origins["linked_mcap"],
        ("B", "metadata.yaml"): origins["linked_metadata"],
    }
    expected_identities = {
        key: (path.stat().st_dev, path.stat().st_ino)
        for key, path in expected_sources.items()
    }

    manifest = materialize_symlink_view_as_hardlinks(
        source,
        backing_source=backing,
        manifest_path=manifest_path,
    )

    recordings = {recording["serial"]: recording for recording in manifest["recordings"]}
    assert recordings["A"]["source_kind"] == "plain_directory"
    assert recordings["B"]["source_kind"] == "directory_symlink"
    assert {
        source_file["name"]: source_file["source_kind"]
        for source_file in recordings["A"]["files"]
    } == {
        "A_0.mcap": "file_symlink",
        "metacard.json": "regular_file",
        "metadata.yaml": "file_symlink",
    }
    manifest_files = {
        (recording["serial"], source_file["name"]): source_file
        for recording in manifest["recordings"]
        for source_file in recording["files"]
    }
    for (serial, filename), original in expected_sources.items():
        visible = source / serial / filename
        source_file = manifest_files[(serial, filename)]
        assert source_file["source"] == str(original)
        assert (source_file["device"], source_file["inode"]) == (
            expected_identities[(serial, filename)]
        )
        assert visible.is_file() and not visible.is_symlink()
        assert (visible.stat().st_dev, visible.stat().st_ino) == expected_identities[
            (serial, filename)
        ]


def test_materialize_hybrid_view_preserves_quarantine_only_in_rollback(
    tmp_path,
):
    source, backing, manifest_path, origins = _hybrid_materialization_fixture(
        tmp_path
    )
    quarantine_identity = origins["quarantine"].stat()
    quarantine_file_identity = origins["quarantine_file"].stat()
    quarantine_bytes = origins["quarantine_file"].read_bytes()

    manifest = materialize_symlink_view_as_hardlinks(
        source,
        backing_source=backing,
        manifest_path=manifest_path,
    )

    assert not (source / origins["quarantine"].name).exists()
    rollback_quarantine = Path(manifest["rollback_view"]) / origins["quarantine"].name
    rollback_file = rollback_quarantine / origins["quarantine_file"].name
    assert (rollback_quarantine.stat().st_dev, rollback_quarantine.stat().st_ino) == (
        quarantine_identity.st_dev,
        quarantine_identity.st_ino,
    )
    assert (rollback_file.stat().st_dev, rollback_file.stat().st_ino) == (
        quarantine_file_identity.st_dev,
        quarantine_file_identity.st_ino,
    )
    assert rollback_file.read_bytes() == quarantine_bytes
    assert manifest["preserved_entries"] == [
        {
            "name": origins["quarantine"].name,
            "kind": "directory",
            "device": quarantine_identity.st_dev,
            "inode": quarantine_identity.st_ino,
            "mode": stat.S_IMODE(quarantine_identity.st_mode),
            "tree": [
                {
                    "relative_path": ".",
                    "kind": "directory",
                    "device": quarantine_identity.st_dev,
                    "inode": quarantine_identity.st_ino,
                    "mode": stat.S_IMODE(quarantine_identity.st_mode),
                },
                {
                    "relative_path": origins["quarantine_file"].name,
                    "kind": "regular_file",
                    "device": quarantine_file_identity.st_dev,
                    "inode": quarantine_file_identity.st_ino,
                    "mode": stat.S_IMODE(quarantine_file_identity.st_mode),
                    "size": quarantine_file_identity.st_size,
                },
            ],
        }
    ]


def test_materialize_hybrid_view_committed_replay_is_idempotent(tmp_path):
    source, backing, manifest_path, _ = _hybrid_materialization_fixture(tmp_path)
    first = materialize_symlink_view_as_hardlinks(
        source,
        backing_source=backing,
        manifest_path=manifest_path,
    )
    manifest_bytes = manifest_path.read_bytes()
    installed_identities = {
        str(path.relative_to(source)): (path.stat().st_dev, path.stat().st_ino)
        for serial in ("A", "B")
        for path in (source / serial).iterdir()
    }

    second = materialize_symlink_view_as_hardlinks(
        source,
        backing_source=backing,
        manifest_path=manifest_path,
    )

    assert second == first
    assert manifest_path.read_bytes() == manifest_bytes
    assert {
        str(path.relative_to(source)): (path.stat().st_dev, path.stat().st_ino)
        for serial in ("A", "B")
        for path in (source / serial).iterdir()
    } == installed_identities


def test_materialize_hybrid_view_rejects_inner_symlink_escaping_backing(
    tmp_path,
):
    source, backing, manifest_path, _ = _hybrid_materialization_fixture(tmp_path)
    foreign = tmp_path / "foreign.mcap"
    foreign.write_bytes(b"foreign-preserve")
    inner_link = source / "A" / "A_0.mcap"
    inner_link.unlink()
    inner_link.symlink_to(foreign)

    with pytest.raises((ValueError, RuntimeError)):
        materialize_symlink_view_as_hardlinks(
            source,
            backing_source=backing,
            manifest_path=manifest_path,
        )

    assert foreign.read_bytes() == b"foreign-preserve"
    assert inner_link.is_symlink()


def test_materialize_hybrid_view_rejects_inner_symlink_for_wrong_serial(
    tmp_path,
):
    source, backing, manifest_path, origins = _hybrid_materialization_fixture(
        tmp_path
    )
    inner_link = source / "A" / "A_0.mcap"
    inner_link.unlink()
    inner_link.symlink_to(origins["linked_mcap"])

    with pytest.raises((ValueError, RuntimeError)):
        materialize_symlink_view_as_hardlinks(
            source,
            backing_source=backing,
            manifest_path=manifest_path,
        )

    assert origins["linked_mcap"].read_bytes() == b"linked-serial-mcap"
    assert inner_link.is_symlink()


def test_materialize_hybrid_view_rejects_symlink_quarantine_without_deleting_target(
    tmp_path,
):
    source, backing, manifest_path, origins = _hybrid_materialization_fixture(
        tmp_path
    )
    quarantine = origins["quarantine"]
    quarantine_file = origins["quarantine_file"]
    foreign = tmp_path / "foreign-quarantine"
    quarantine.rename(foreign)
    quarantine.symlink_to(foreign, target_is_directory=True)
    foreign_identity = foreign.stat()
    foreign_file_identity = (foreign / quarantine_file.name).stat()

    with pytest.raises((ValueError, RuntimeError)):
        materialize_symlink_view_as_hardlinks(
            source,
            backing_source=backing,
            manifest_path=manifest_path,
        )

    assert quarantine.is_symlink()
    assert (foreign.stat().st_dev, foreign.stat().st_ino) == (
        foreign_identity.st_dev,
        foreign_identity.st_ino,
    )
    assert (
        (foreign / quarantine_file.name).stat().st_dev,
        (foreign / quarantine_file.name).stat().st_ino,
    ) == (foreign_file_identity.st_dev, foreign_file_identity.st_ino)
    assert (foreign / quarantine_file.name).read_bytes() == b"quarantined-original"


def test_materialize_hybrid_view_quarantine_replacement_fails_closed(
    tmp_path,
    monkeypatch,
):
    source, backing, manifest_path, origins = _hybrid_materialization_fixture(
        tmp_path
    )
    quarantine = origins["quarantine"]
    displaced = tmp_path / "displaced-original-quarantine"
    real_write_manifest = split_module.write_manifest
    swapped = False

    def swap_quarantine_after_plan(path, plan):
        nonlocal swapped
        real_write_manifest(path, plan)
        if plan.get("phase") == "reserved" and not swapped:
            quarantine.rename(displaced)
            quarantine.mkdir()
            (quarantine / "foreign").write_text("preserve", encoding="utf-8")
            swapped = True

    monkeypatch.setattr(split_module, "write_manifest", swap_quarantine_after_plan)

    with pytest.raises((ValueError, RuntimeError)):
        materialize_symlink_view_as_hardlinks(
            source,
            backing_source=backing,
            manifest_path=manifest_path,
        )

    assert swapped
    assert (quarantine / "foreign").read_text(encoding="utf-8") == "preserve"
    assert (displaced / origins["quarantine_file"].name).read_bytes() == (
        b"quarantined-original"
    )


@pytest.mark.parametrize(
    ("window", "exit_code"),
    [
        ("after_archive", 91),
        ("after_install", 92),
        ("partial_staging", 93),
    ],
)
def test_materialize_hybrid_view_replays_crash_to_canonical_tree(
    tmp_path,
    window,
    exit_code,
):
    source, backing, manifest_path, origins = _hybrid_materialization_fixture(
        tmp_path
    )
    expected_identities = _hybrid_source_identities(origins)
    quarantine_info = origins["quarantine"].stat()
    quarantine_identity = (quarantine_info.st_dev, quarantine_info.st_ino)
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_materialization,
        args=(source, backing, manifest_path, window),
    )

    process.start()
    process.join(10)
    assert process.exitcode == exit_code

    manifest = materialize_symlink_view_as_hardlinks(
        source,
        backing_source=backing,
        manifest_path=manifest_path,
    )

    assert manifest["phase"] == "committed"
    _assert_hybrid_materialized(
        source,
        manifest,
        expected_identities,
        quarantine_identity,
    )
    rollback_file = (
        Path(manifest["rollback_view"])
        / origins["quarantine"].name
        / origins["quarantine_file"].name
    )
    assert rollback_file.read_bytes() == b"quarantined-original"


def test_materialize_detached_hybrid_view_preserves_sources_and_builds_hardlinks(
    tmp_path,
):
    source, backing, manifest_path, origins = _hybrid_materialization_fixture(
        tmp_path
    )
    destination = _detached_destination(tmp_path)
    before = _hybrid_snapshot(source, backing)
    expected_identities = _hybrid_source_identities(origins)

    manifest = split_module.materialize_link_view_detached_as_hardlinks(
        source,
        backing_source=backing,
        destination_task=destination,
        manifest_path=manifest_path,
    )

    assert manifest["version"] == 3
    assert manifest["operation"] == "materialize_link_view_detached_as_hardlinks"
    assert manifest["phase"] == "committed"
    assert "rollback_view" not in manifest
    assert _hybrid_snapshot(source, backing) == before
    destination_info = destination.stat(follow_symlinks=False)
    assert (
        manifest["destination_task"],
        manifest["destination_device"],
        manifest["destination_inode"],
        manifest["destination_mode"],
    ) == (
        str(destination),
        destination_info.st_dev,
        destination_info.st_ino,
        stat.S_IMODE(destination_info.st_mode),
    )
    assert sorted(path.name for path in destination.iterdir() if not path.name.startswith(".")) == [
        "A",
        "B",
    ]
    assert not (destination / origins["quarantine"].name).exists()
    for serial in ("A", "B"):
        recording = destination / serial
        assert recording.is_dir() and not recording.is_symlink()
        assert len(list(recording.iterdir())) == 3
    for relative_path, expected_identity in expected_identities.items():
        visible = destination / relative_path
        assert visible.is_file() and not visible.is_symlink()
        assert (visible.stat().st_dev, visible.stat().st_ino) == expected_identity


def test_materialize_detached_committed_replay_is_idempotent(tmp_path):
    source, backing, manifest_path, _ = _hybrid_materialization_fixture(tmp_path)
    destination = _detached_destination(tmp_path)
    first = split_module.materialize_link_view_detached_as_hardlinks(
        source,
        backing_source=backing,
        destination_task=destination,
        manifest_path=manifest_path,
    )
    manifest_bytes = manifest_path.read_bytes()
    destination_identity = (
        destination.stat().st_dev,
        destination.stat().st_ino,
    )
    installed_identities = {
        str(path.relative_to(destination)): (path.stat().st_dev, path.stat().st_ino)
        for serial in ("A", "B")
        for path in (destination / serial).iterdir()
    }

    second = split_module.materialize_link_view_detached_as_hardlinks(
        source,
        backing_source=backing,
        destination_task=destination,
        manifest_path=manifest_path,
    )

    assert second == first
    assert manifest_path.read_bytes() == manifest_bytes
    assert (destination.stat().st_dev, destination.stat().st_ino) == (
        destination_identity
    )
    assert {
        str(path.relative_to(destination)): (path.stat().st_dev, path.stat().st_ino)
        for serial in ("A", "B")
        for path in (destination / serial).iterdir()
    } == installed_identities


def test_materialize_detached_rejects_preexisting_destination_without_deleting_it(
    tmp_path,
):
    source, backing, manifest_path, _ = _hybrid_materialization_fixture(tmp_path)
    destination = _detached_destination(tmp_path)
    destination.mkdir()
    foreign = destination / "foreign"
    foreign.write_bytes(b"preserve")
    destination_info = destination.stat(follow_symlinks=False)
    before = _hybrid_snapshot(source, backing)

    with pytest.raises((FileExistsError, RuntimeError, ValueError)):
        split_module.materialize_link_view_detached_as_hardlinks(
            source,
            backing_source=backing,
            destination_task=destination,
            manifest_path=manifest_path,
        )

    assert _hybrid_snapshot(source, backing) == before
    assert (destination.stat().st_dev, destination.stat().st_ino) == (
        destination_info.st_dev,
        destination_info.st_ino,
    )
    assert foreign.read_bytes() == b"preserve"


@pytest.mark.parametrize("parent_kind", ["non_private", "nonempty"])
def test_materialize_detached_rejects_unsafe_parent_before_manifest(
    tmp_path,
    parent_kind,
):
    source, backing, manifest_path, _ = _hybrid_materialization_fixture(tmp_path)
    parent = tmp_path / "unsafe-detached-parent"
    parent.mkdir(mode=0o700)
    parent.chmod(0o755 if parent_kind == "non_private" else 0o700)
    foreign = parent / "foreign"
    if parent_kind == "nonempty":
        foreign.write_bytes(b"preserve")
    destination = parent / "task-detached"
    before = _hybrid_snapshot(source, backing)

    with pytest.raises((ValueError, RuntimeError)):
        split_module.materialize_link_view_detached_as_hardlinks(
            source,
            backing_source=backing,
            destination_task=destination,
            manifest_path=manifest_path,
        )

    assert _hybrid_snapshot(source, backing) == before
    assert not manifest_path.exists()
    assert not destination.exists()
    if parent_kind == "nonempty":
        assert foreign.read_bytes() == b"preserve"


@pytest.mark.parametrize("drift_kind", ["mode", "identity"])
def test_materialize_detached_parent_drift_on_replay_fails_closed(
    tmp_path,
    drift_kind,
):
    source, backing, manifest_path, _ = _hybrid_materialization_fixture(tmp_path)
    destination = _detached_destination(tmp_path)
    manifest = split_module.materialize_link_view_detached_as_hardlinks(
        source,
        backing_source=backing,
        destination_task=destination,
        manifest_path=manifest_path,
    )
    parent = destination.parent
    displaced = tmp_path / "displaced-private-parent"
    if drift_kind == "mode":
        parent.chmod(0o755)
    else:
        parent.rename(displaced)
        parent.mkdir(mode=0o700)
        parent.chmod(0o700)

    with pytest.raises((ValueError, RuntimeError)):
        split_module.materialize_link_view_detached_as_hardlinks(
            source,
            backing_source=backing,
            destination_task=destination,
            manifest_path=manifest_path,
        )

    durable = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert durable["phase"] == "recovery_failed"
    assert manifest["destination_parent_device"] == durable[
        "destination_parent_device"
    ]
    assert manifest["destination_parent_inode"] == durable[
        "destination_parent_inode"
    ]
    if drift_kind == "mode":
        assert destination.is_dir()
    else:
        assert (displaced / destination.name).is_dir()
        assert list(parent.iterdir()) == []


def test_materialize_detached_root_swap_before_open_is_never_bound(
    tmp_path,
    monkeypatch,
):
    source, backing, manifest_path, _ = _hybrid_materialization_fixture(tmp_path)
    destination = _detached_destination(tmp_path)
    displaced = tmp_path / "displaced-created-destination"
    real_open = split_module.os.open
    swapped = False
    foreign_identity = None

    def swap_root_before_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped, foreign_identity
        if path == destination.name and dir_fd is not None and not swapped:
            destination.rename(displaced)
            destination.mkdir(mode=0o711)
            info = destination.stat(follow_symlinks=False)
            foreign_identity = (info.st_dev, info.st_ino)
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(split_module.os, "open", swap_root_before_open)

    with pytest.raises(RuntimeError, match="identity changed before binding"):
        split_module.materialize_link_view_detached_as_hardlinks(
            source,
            backing_source=backing,
            destination_task=destination,
            manifest_path=manifest_path,
        )

    durable = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert swapped
    assert durable["phase"] == "recovery_failed"
    assert durable["destination_device"] is None
    assert durable["destination_inode"] is None
    assert (destination.stat().st_dev, destination.stat().st_ino) == (
        foreign_identity
    )
    assert list(destination.iterdir()) == []
    assert displaced.is_dir()

    with pytest.raises(RuntimeError, match="recovery_failed"):
        split_module.materialize_link_view_detached_as_hardlinks(
            source,
            backing_source=backing,
            destination_task=destination,
            manifest_path=manifest_path,
        )


def test_materialize_detached_never_adopts_precreated_recording_directory(
    tmp_path,
    monkeypatch,
):
    source, backing, manifest_path, _ = _hybrid_materialization_fixture(tmp_path)
    destination = _detached_destination(tmp_path)
    real_write_manifest = split_module.write_manifest
    precreated_identity = None
    precreated_mode = None

    def precreate_serial_after_root_binding(path, manifest):
        nonlocal precreated_identity, precreated_mode
        real_write_manifest(path, manifest)
        if manifest.get("phase") == "preparing" and precreated_identity is None:
            precreated = destination / "A"
            precreated.mkdir(mode=0o711)
            info = precreated.stat(follow_symlinks=False)
            precreated_identity = (info.st_dev, info.st_ino)
            precreated_mode = stat.S_IMODE(info.st_mode)

    monkeypatch.setattr(
        split_module,
        "write_manifest",
        precreate_serial_after_root_binding,
    )

    with pytest.raises(RuntimeError):
        split_module.materialize_link_view_detached_as_hardlinks(
            source,
            backing_source=backing,
            destination_task=destination,
            manifest_path=manifest_path,
        )

    durable = json.loads(manifest_path.read_text(encoding="utf-8"))
    recording = next(
        item for item in durable["recordings"] if item["serial"] == "A"
    )
    assert durable["phase"] == "recovery_failed"
    assert recording["destination_device"] is None
    assert recording["destination_inode"] is None
    assert (
        (destination / "A").stat().st_dev,
        (destination / "A").stat().st_ino,
    ) == precreated_identity
    assert stat.S_IMODE((destination / "A").stat().st_mode) == precreated_mode
    assert list((destination / "A").iterdir()) == []

    with pytest.raises(RuntimeError, match="recovery_failed"):
        split_module.materialize_link_view_detached_as_hardlinks(
            source,
            backing_source=backing,
            destination_task=destination,
            manifest_path=manifest_path,
        )


@pytest.mark.parametrize(
    ("destination_factory", "manifest_factory"),
    [
        (
            lambda source, _backing: source / "nested-destination",
            lambda source, _backing, _destination: source.parent / "manifest.json",
        ),
        (
            lambda source, backing: backing / "nested-destination",
            lambda source, _backing, _destination: source.parent / "manifest.json",
        ),
        (
            lambda source, _backing: source.with_name("task-detached"),
            lambda source, _backing, _destination: source / "manifest.json",
        ),
        (
            lambda source, _backing: source.with_name("task-detached"),
            lambda _source, _backing, destination: destination / "manifest.json",
        ),
    ],
)
def test_materialize_detached_rejects_protected_destination_and_manifest_paths(
    tmp_path,
    destination_factory,
    manifest_factory,
):
    source, backing, _, _ = _hybrid_materialization_fixture(tmp_path)
    destination = destination_factory(source, backing)
    manifest_path = manifest_factory(source, backing, destination)
    before = _hybrid_snapshot(source, backing)

    with pytest.raises((ValueError, RuntimeError)):
        split_module.materialize_link_view_detached_as_hardlinks(
            source,
            backing_source=backing,
            destination_task=destination,
            manifest_path=manifest_path,
        )

    assert _hybrid_snapshot(source, backing) == before
    assert not destination.exists()
    assert not manifest_path.exists()


@pytest.mark.parametrize("race_kind", ["foreign_entry", "identity_swap"])
def test_materialize_detached_destination_race_preserves_foreign_data(
    tmp_path,
    monkeypatch,
    race_kind,
):
    source, backing, manifest_path, _ = _hybrid_materialization_fixture(tmp_path)
    destination = _detached_destination(tmp_path)
    displaced = tmp_path / "displaced-detached"
    foreign_bytes = f"foreign-{race_kind}".encode()
    real_write_manifest = split_module.write_manifest
    raced = False
    foreign_root_identity = None

    def race_destination(path, plan):
        nonlocal raced, foreign_root_identity
        real_write_manifest(path, plan)
        if (
            plan.get("phase") in {"preparing", "finalizing"}
            and destination.is_dir()
            and not raced
        ):
            if race_kind == "foreign_entry":
                (destination / "foreign").write_bytes(foreign_bytes)
            else:
                destination.rename(displaced)
                destination.mkdir()
                (destination / "foreign").write_bytes(foreign_bytes)
                info = destination.stat(follow_symlinks=False)
                foreign_root_identity = (info.st_dev, info.st_ino)
            raced = True

    monkeypatch.setattr(split_module, "write_manifest", race_destination)

    with pytest.raises((OSError, RuntimeError)):
        split_module.materialize_link_view_detached_as_hardlinks(
            source,
            backing_source=backing,
            destination_task=destination,
            manifest_path=manifest_path,
        )

    durable = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert raced
    assert durable["phase"] == "recovery_failed"
    assert (destination / "foreign").read_bytes() == foreign_bytes
    if race_kind == "identity_swap":
        assert (destination.stat().st_dev, destination.stat().st_ino) == (
            foreign_root_identity
        )
        assert displaced.is_dir()


@pytest.mark.parametrize("swap_kind", ["source", "backing"])
def test_materialize_detached_source_swap_fails_closed(
    tmp_path,
    monkeypatch,
    swap_kind,
):
    source, backing, manifest_path, origins = _hybrid_materialization_fixture(
        tmp_path
    )
    destination = _detached_destination(tmp_path)
    victim = (
        origins["plain_metacard"]
        if swap_kind == "source"
        else origins["backing_plain_mcap"]
    )
    displaced = tmp_path / f"displaced-{swap_kind}"
    original_bytes = victim.read_bytes()
    foreign_bytes = f"foreign-{swap_kind}".encode()
    real_write_manifest = split_module.write_manifest
    swapped = False

    def swap_after_reservation(path, plan):
        nonlocal swapped
        real_write_manifest(path, plan)
        if plan.get("phase") == "reserved" and not swapped:
            victim.rename(displaced)
            victim.write_bytes(foreign_bytes)
            swapped = True

    monkeypatch.setattr(split_module, "write_manifest", swap_after_reservation)

    with pytest.raises((OSError, RuntimeError)):
        split_module.materialize_link_view_detached_as_hardlinks(
            source,
            backing_source=backing,
            destination_task=destination,
            manifest_path=manifest_path,
        )

    durable = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert swapped
    assert durable["phase"] == "recovery_failed"
    assert displaced.read_bytes() == original_bytes
    assert victim.read_bytes() == foreign_bytes


def test_materialize_detached_partial_crash_preserves_source_and_replays_safely(
    tmp_path,
):
    source, backing, manifest_path, _ = _hybrid_materialization_fixture(tmp_path)
    destination = _detached_destination(tmp_path)
    before = _hybrid_snapshot(source, backing)
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_detached_materialization,
        args=(source, backing, destination, manifest_path),
    )

    process.start()
    process.join(10)
    assert process.exitcode == 111
    assert _hybrid_snapshot(source, backing) == before

    try:
        manifest = split_module.materialize_link_view_detached_as_hardlinks(
            source,
            backing_source=backing,
            destination_task=destination,
            manifest_path=manifest_path,
        )
    except RuntimeError:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert _hybrid_snapshot(source, backing) == before
    assert manifest["phase"] in {"committed", "recovery_failed"}
    if manifest["phase"] == "recovery_failed":
        assert destination.exists()


def test_materialize_committed_legacy_v1_manifest_replay_is_byte_exact_no_op(
    tmp_path,
):
    source, backing, manifest_path = _materialization_fixture(tmp_path)
    manifest = materialize_symlink_view_as_hardlinks(
        source,
        backing_source=backing,
        manifest_path=manifest_path,
    )
    legacy = _legacy_v1_manifest(manifest)
    split_module.write_manifest(manifest_path, legacy)
    manifest_bytes = manifest_path.read_bytes()
    installed_inode = (source / "A" / "metacard.json").stat().st_ino

    replayed = materialize_symlink_view_as_hardlinks(
        source,
        backing_source=backing,
        manifest_path=manifest_path,
    )

    assert replayed == legacy
    assert manifest_path.read_bytes() == manifest_bytes
    assert (source / "A" / "metacard.json").stat().st_ino == installed_inode


def test_materialize_committed_legacy_v1_replay_rejects_backing_mode_drift(
    tmp_path,
):
    source, backing, manifest_path = _materialization_fixture(tmp_path)
    manifest = materialize_symlink_view_as_hardlinks(
        source,
        backing_source=backing,
        manifest_path=manifest_path,
    )
    legacy = _legacy_v1_manifest(manifest)
    split_module.write_manifest(manifest_path, legacy)
    backing_recording = backing / "A"
    drifted_mode = legacy["recordings"][0]["source_mode"] ^ stat.S_IXOTH
    backing_recording.chmod(drifted_mode)

    with pytest.raises(RuntimeError, match="backing changed"):
        materialize_symlink_view_as_hardlinks(
            source,
            backing_source=backing,
            manifest_path=manifest_path,
        )

    assert stat.S_IMODE(backing_recording.stat().st_mode) == drifted_mode


@pytest.mark.parametrize("missing_kind", ["recording", "file"])
def test_materialize_v2_manifest_rejects_missing_source_kind(
    tmp_path,
    missing_kind,
):
    source, backing, manifest_path = _materialization_fixture(tmp_path)
    manifest = materialize_symlink_view_as_hardlinks(
        source,
        backing_source=backing,
        manifest_path=manifest_path,
    )
    damaged = json.loads(json.dumps(manifest))
    if missing_kind == "recording":
        damaged["recordings"][0].pop("source_kind")
    else:
        damaged["recordings"][0]["files"][0].pop("source_kind")
    split_module.write_manifest(manifest_path, damaged)

    with pytest.raises(RuntimeError, match="invalid materialization recovery manifest"):
        materialize_symlink_view_as_hardlinks(
            source,
            backing_source=backing,
            manifest_path=manifest_path,
        )


@pytest.mark.parametrize(
    "race_point",
    [
        "construction_publish",
        "partial_quarantine",
        "archive",
        "restore",
        "install",
    ],
)
def test_materialize_namespace_move_preserves_racing_destination(
    tmp_path,
    monkeypatch,
    race_point,
):
    source, backing, manifest_path = _materialization_fixture(tmp_path)
    if race_point == "partial_quarantine":
        process = multiprocessing.get_context("spawn").Process(
            target=_crash_materialization,
            args=(source, backing, manifest_path, "partial_staging"),
        )
        process.start()
        process.join(10)
        assert process.exitcode == 93

    real_rename = split_module._rename_materialization_noreplace
    raced_destination = None
    raced_identity = None
    forced_install_failure = False

    def race_destination(source_path, destination_path):
        nonlocal raced_destination, raced_identity, forced_install_failure
        source_path = Path(source_path)
        destination_path = Path(destination_path)
        is_construction_publish = (
            ".hardlink-construction-" in source_path.name
            and ".hardlink-staging-" in destination_path.name
        )
        is_partial_quarantine = (
            ".hardlink-staging-" in source_path.name
            and ".hardlink-quarantine-" in destination_path.name
        )
        is_archive = (
            source_path == source
            and ".symlink-view-rollback-" in destination_path.name
        )
        is_restore = (
            ".symlink-view-rollback-" in source_path.name
            and destination_path == source
        )
        is_install = (
            ".hardlink-staging-" in source_path.name
            and destination_path == source
        )
        if race_point == "restore" and is_install and not forced_install_failure:
            forced_install_failure = True
            raise OSError("injected install failure before restore")
        should_race = {
            "construction_publish": is_construction_publish,
            "partial_quarantine": is_partial_quarantine,
            "archive": is_archive,
            "restore": is_restore,
            "install": is_install,
        }[race_point]
        if should_race and raced_destination is None:
            destination_path.mkdir()
            info = destination_path.stat(follow_symlinks=False)
            raced_destination = destination_path
            raced_identity = (info.st_dev, info.st_ino)
        return real_rename(source_path, destination_path)

    monkeypatch.setattr(
        split_module,
        "_rename_materialization_noreplace",
        race_destination,
    )

    with pytest.raises((OSError, RuntimeError)):
        materialize_symlink_view_as_hardlinks(
            source,
            backing_source=backing,
            manifest_path=manifest_path,
        )

    assert raced_destination is not None
    assert raced_identity is not None
    assert raced_destination.is_dir() and not raced_destination.is_symlink()
    assert (
        raced_destination.stat().st_dev,
        raced_destination.stat().st_ino,
    ) == raced_identity
    assert list(raced_destination.iterdir()) == []


def test_materialize_hybrid_view_rejects_unknown_hidden_root_without_deleting_it(
    tmp_path,
):
    source, backing, manifest_path, _ = _hybrid_materialization_fixture(tmp_path)
    unknown = source / ".unexpected-hidden"
    unknown.mkdir()
    foreign = unknown / "foreign"
    foreign.write_text("preserve", encoding="utf-8")

    with pytest.raises((ValueError, RuntimeError), match="unexpected entry"):
        materialize_symlink_view_as_hardlinks(
            source,
            backing_source=backing,
            manifest_path=manifest_path,
        )

    assert foreign.read_text(encoding="utf-8") == "preserve"


@pytest.mark.parametrize("entry_kind", ["symlink", "fifo"])
def test_materialize_hybrid_view_rejects_non_regular_quarantine_descendant(
    tmp_path,
    entry_kind,
):
    source, backing, manifest_path, origins = _hybrid_materialization_fixture(
        tmp_path
    )
    nested = origins["quarantine"] / "nested-special"
    foreign = tmp_path / "foreign-preserved"
    if entry_kind == "symlink":
        foreign.write_text("preserve", encoding="utf-8")
        nested.symlink_to(foreign)
    else:
        os.mkfifo(nested)

    with pytest.raises((ValueError, RuntimeError), match="symlink or special"):
        materialize_symlink_view_as_hardlinks(
            source,
            backing_source=backing,
            manifest_path=manifest_path,
        )

    if entry_kind == "symlink":
        assert nested.is_symlink()
        assert foreign.read_text(encoding="utf-8") == "preserve"
    else:
        assert stat.S_ISFIFO(nested.lstat().st_mode)


def test_materialize_preserves_exact_audit_marker_without_hiding_recordings(
    tmp_path,
):
    source, backing, manifest_path = _materialization_fixture(tmp_path)

    manifest = materialize_symlink_view_as_hardlinks(
        source,
        backing_source=backing,
        manifest_path=manifest_path,
    )

    marker_name = f".robodata-reservation-{manifest['staging_reservation']}"
    marker = source / marker_name
    marker_info = marker.stat(follow_symlinks=False)
    assert marker.is_file() and marker.read_bytes() == b""
    assert (marker_info.st_dev, marker_info.st_ino) == (
        manifest["staging_marker_device"],
        manifest["staging_marker_inode"],
    )
    assert sorted(path.name for path in source.iterdir() if path.is_dir()) == [
        "A"
    ]
    assert build_split_plan(source)["invalid"] == []


@pytest.mark.parametrize(
    ("window", "exit_code", "stale_phase"),
    [
        ("after_archive", 91, "prepared"),
        ("after_install", 92, "view_archived"),
        ("partial_staging", 93, "preparing"),
    ],
)
def test_materialize_reconciles_real_process_crash_windows(
    tmp_path,
    window,
    exit_code,
    stale_phase,
):
    source, backing, manifest_path = _materialization_fixture(tmp_path)
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_materialization,
        args=(source, backing, manifest_path, window),
    )

    process.start()
    process.join(10)

    assert process.exitcode == exit_code
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["phase"] == stale_phase

    manifest = materialize_symlink_view_as_hardlinks(
        source,
        backing_source=backing,
        manifest_path=manifest_path,
    )

    _assert_materialized(source, backing, manifest)


def test_materialize_committed_replay_is_a_no_op(tmp_path):
    source, backing, manifest_path = _materialization_fixture(tmp_path)
    first = materialize_symlink_view_as_hardlinks(
        source,
        backing_source=backing,
        manifest_path=manifest_path,
    )
    visible_inode = (source / "A" / "metacard.json").stat().st_ino
    manifest_bytes = manifest_path.read_bytes()

    second = materialize_symlink_view_as_hardlinks(
        source,
        backing_source=backing,
        manifest_path=manifest_path,
    )

    assert second == first
    assert manifest_path.read_bytes() == manifest_bytes
    assert (source / "A" / "metacard.json").stat().st_ino == visible_inode
    _assert_materialized(source, backing, second)


def test_materialize_does_not_roll_back_after_commit_point(tmp_path, monkeypatch):
    source, backing, manifest_path = _materialization_fixture(tmp_path)
    real_write_manifest = split_module.write_manifest

    def fail_committed_manifest(path, plan):
        if plan.get("phase") == "committed":
            raise OSError("injected committed manifest failure")
        real_write_manifest(path, plan)

    monkeypatch.setattr(split_module, "write_manifest", fail_committed_manifest)

    with pytest.raises(OSError, match="injected committed manifest failure"):
        materialize_symlink_view_as_hardlinks(
            source,
            backing_source=backing,
            manifest_path=manifest_path,
        )

    stale_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert stale_manifest["phase"] == "view_archived"
    assert (source / "A").is_dir() and not (source / "A").is_symlink()
    assert Path(stale_manifest["rollback_view"]).is_dir()

    monkeypatch.undo()
    recovered = materialize_symlink_view_as_hardlinks(
        source,
        backing_source=backing,
        manifest_path=manifest_path,
    )
    _assert_materialized(source, backing, recovered)


def test_materialize_duplicate_original_view_fails_closed(tmp_path):
    source, backing, manifest_path = _materialization_fixture(tmp_path)
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_materialization,
        args=(source, backing, manifest_path, "after_archive"),
    )
    process.start()
    process.join(10)
    assert process.exitcode == 91
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rollback_view = Path(manifest["rollback_view"])
    source.mkdir()
    for entry in rollback_view.iterdir():
        os.link(entry, source / entry.name, follow_symlinks=False)
    before = sorted(path.name for path in source.parent.iterdir())

    with pytest.raises(RuntimeError, match="ambiguous|recovery"):
        materialize_symlink_view_as_hardlinks(
            source,
            backing_source=backing,
            manifest_path=manifest_path,
        )

    assert sorted(path.name for path in source.parent.iterdir()) == before
    assert source.is_dir()
    assert rollback_view.is_dir()


def test_materialize_rejects_symlink_lock_without_touching_target(tmp_path):
    source, backing, manifest_path = _materialization_fixture(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("sentinel", encoding="utf-8")
    lock_path = source.with_name(
        f".{source.name}.hardlink-materialization.lock"
    )
    lock_path.symlink_to(victim)

    with pytest.raises(OSError):
        materialize_symlink_view_as_hardlinks(
            source,
            backing_source=backing,
            manifest_path=manifest_path,
        )

    assert victim.read_text(encoding="utf-8") == "sentinel"
    assert lock_path.is_symlink()
    assert not manifest_path.exists()
    assert all(path.is_symlink() for path in source.iterdir())


def test_materialize_symlink_view_rolls_back_when_commit_rename_fails(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "cell007" / "task"
    _recording(source, "A", "robot-a", ["left", "right"])
    plan = build_split_plan(source)
    backing = source.with_name(".task__metadata_source_20260721")
    apply_split_as_symlink_view(plan, backing)
    manifest_path = tmp_path / "materialize.json"
    real_rename = split_module._rename_materialization_noreplace

    def fail_staging_commit(src, dst):
        if (
            Path(src).name.startswith(".task.hardlink-staging-")
            and Path(dst) == source
        ):
            raise OSError("injected commit failure")
        return real_rename(src, dst)

    monkeypatch.setattr(
        split_module,
        "_rename_materialization_noreplace",
        fail_staging_commit,
    )

    try:
        materialize_symlink_view_as_hardlinks(
            source,
            backing_source=backing,
            manifest_path=manifest_path,
        )
    except OSError as exc:
        assert str(exc) == "injected commit failure"
    else:
        raise AssertionError("materialization should fail")

    assert source.is_dir() and not source.is_symlink()
    assert all(path.is_symlink() for path in source.iterdir())
    assert not list(source.parent.glob(".task.hardlink-staging-*"))
    assert not list(source.parent.glob(".task.symlink-view-rollback-*"))
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["phase"] == "rolled_back"


def test_materialize_symlink_view_rejects_target_outside_backing(tmp_path):
    source = tmp_path / "cell007" / "task"
    source.mkdir(parents=True)
    backing = source.with_name(".task__metadata_source_20260721")
    backing.mkdir()
    outside = tmp_path / "outside" / "A"
    outside.mkdir(parents=True)
    (outside / "metacard.json").write_text("{}", encoding="utf-8")
    (source / "A").symlink_to(outside, target_is_directory=True)

    try:
        materialize_symlink_view_as_hardlinks(
            source,
            backing_source=backing,
            manifest_path=tmp_path / "materialize.json",
        )
    except ValueError as exc:
        assert "outside backing source" in str(exc)
    else:
        raise AssertionError("escaping symlink target should be rejected")

    assert (source / "A").is_symlink()


def test_materialize_recording_symlink_swap_cannot_chmod_foreign_directory(
    tmp_path,
    monkeypatch,
):
    source, backing, manifest_path = _materialization_fixture(tmp_path)
    victim = tmp_path / "foreign-recording"
    victim.mkdir(mode=0o711)
    original_mode = victim.stat().st_mode & 0o777
    real_open = split_module.os.open
    swapped = False

    def swap_before_recording_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        staging_candidates = list(
            source.parent.glob(".task.hardlink-staging-*")
        )
        if (
            path == "A"
            and dir_fd is not None
            and not swapped
            and staging_candidates
        ):
            staging = staging_candidates[0]
            (staging / "A").rmdir()
            (staging / "A").symlink_to(victim, target_is_directory=True)
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(split_module.os, "open", swap_before_recording_open)

    with pytest.raises((OSError, RuntimeError)):
        materialize_symlink_view_as_hardlinks(
            source,
            backing_source=backing,
            manifest_path=manifest_path,
        )

    assert swapped
    assert victim.stat().st_mode & 0o777 == original_mode
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["phase"] != "committed"


def test_materialize_staging_swap_before_commit_cannot_mark_committed(
    tmp_path,
    monkeypatch,
):
    source, backing, manifest_path = _materialization_fixture(tmp_path)
    real_rename = split_module._rename_materialization_noreplace
    installed_foreign = tmp_path / "installed-foreign"

    def swap_staging_before_commit(src, dst):
        source_path = Path(src)
        destination_path = Path(dst)
        if (
            source_path.name.startswith(".task.hardlink-staging-")
            and destination_path == source
        ):
            real_rename(source_path, installed_foreign)
            source_path.mkdir()
        return real_rename(src, dst)

    monkeypatch.setattr(
        split_module,
        "_rename_materialization_noreplace",
        swap_staging_before_commit,
    )

    with pytest.raises(RuntimeError, match="installed|rollback failed"):
        materialize_symlink_view_as_hardlinks(
            source,
            backing_source=backing,
            manifest_path=manifest_path,
        )

    assert json.loads(manifest_path.read_text(encoding="utf-8"))["phase"] != "committed"
    assert source.is_dir()
    assert list(source.iterdir()) == []
    assert installed_foreign.is_dir()


def test_materialize_does_not_clobber_fixed_temp_hardlink(tmp_path):
    source, backing, manifest_path = _materialization_fixture(tmp_path)
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("unchanged", encoding="utf-8")
    fixed_temporary = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    os.link(sentinel, fixed_temporary)
    before = sentinel.stat()

    manifest = materialize_symlink_view_as_hardlinks(
        source,
        backing_source=backing,
        manifest_path=manifest_path,
    )

    after = sentinel.stat()
    assert manifest["phase"] == "committed"
    assert fixed_temporary.read_text(encoding="utf-8") == "unchanged"
    assert (after.st_dev, after.st_ino, after.st_nlink) == (
        before.st_dev,
        before.st_ino,
        before.st_nlink,
    )


def test_materialize_preserves_foreign_partial_staging_identity(tmp_path):
    source, backing, manifest_path = _materialization_fixture(tmp_path)
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_materialization,
        args=(source, backing, manifest_path, "partial_staging"),
    )
    process.start()
    process.join(10)
    assert process.exitcode == 93
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    staging = Path(manifest["staging"])
    displaced_owned_staging = tmp_path / "owned-staging-displaced"
    staging.rename(displaced_owned_staging)
    staging.mkdir()
    foreign_recording = staging / "A"
    foreign_recording.mkdir()
    marker = foreign_recording / "foreign"
    marker.write_text("preserve", encoding="utf-8")
    foreign_identity = staging.stat().st_dev, staging.stat().st_ino

    with pytest.raises(RuntimeError, match="foreign filesystem state|identity changed"):
        materialize_symlink_view_as_hardlinks(
            source,
            backing_source=backing,
            manifest_path=manifest_path,
        )

    assert staging.is_dir()
    assert (staging.stat().st_dev, staging.stat().st_ino) == foreign_identity
    assert marker.read_text(encoding="utf-8") == "preserve"
    assert displaced_owned_staging.is_dir()


def test_materialize_quarantine_swap_preserves_foreign_root(
    tmp_path,
    monkeypatch,
):
    source, backing, manifest_path = _materialization_fixture(tmp_path)
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_materialization,
        args=(source, backing, manifest_path, "partial_staging"),
    )
    process.start()
    process.join(10)
    assert process.exitcode == 93
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    staging = Path(manifest["staging"])
    displaced_owned = tmp_path / "verified-owned-staging"
    real_rename = split_module._rename_materialization_noreplace
    swapped = False

    def swap_before_quarantine(src, dst):
        nonlocal swapped
        if (
            not swapped
            and Path(src) == staging
            and ".hardlink-quarantine-" in Path(dst).name
        ):
            real_rename(staging, displaced_owned)
            staging.mkdir()
            (staging / "foreign").write_text("preserve", encoding="utf-8")
            swapped = True
        return real_rename(src, dst)

    monkeypatch.setattr(
        split_module,
        "_rename_materialization_noreplace",
        swap_before_quarantine,
    )

    with pytest.raises(RuntimeError, match="remains resumable"):
        materialize_symlink_view_as_hardlinks(
            source,
            backing_source=backing,
            manifest_path=manifest_path,
        )

    quarantines = list(source.parent.glob("*.hardlink-quarantine-*"))
    durable = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert swapped
    assert durable["phase"] == "staging_quarantining"
    assert displaced_owned.is_dir()
    assert len(quarantines) == 1
    assert (quarantines[0] / "foreign").read_text(encoding="utf-8") == "preserve"


@pytest.mark.parametrize("after_move", [False, True])
def test_materialize_replays_ordinary_quarantine_exception(
    tmp_path,
    monkeypatch,
    after_move,
):
    source, backing, manifest_path = _materialization_fixture(tmp_path)
    partial = multiprocessing.get_context("spawn").Process(
        target=_crash_materialization,
        args=(source, backing, manifest_path, "partial_staging"),
    )
    partial.start()
    partial.join(10)
    assert partial.exitcode == 93
    staging = Path(
        json.loads(manifest_path.read_text(encoding="utf-8"))["staging"]
    )
    real_rename = split_module._rename_materialization_noreplace

    def fail_quarantine_once(src, dst):
        if (
            Path(src) == staging
            and ".hardlink-quarantine-" in Path(dst).name
        ):
            if after_move:
                real_rename(src, dst)
            raise OSError("injected ordinary quarantine interruption")
        return real_rename(src, dst)

    monkeypatch.setattr(
        split_module,
        "_rename_materialization_noreplace",
        fail_quarantine_once,
    )

    with pytest.raises(RuntimeError, match="remains resumable"):
        materialize_symlink_view_as_hardlinks(
            source,
            backing_source=backing,
            manifest_path=manifest_path,
        )

    durable = json.loads(manifest_path.read_text(encoding="utf-8"))
    quarantine = Path(durable["staging_quarantines"][-1]["path"])
    assert durable["phase"] == "staging_quarantining"
    assert staging.exists() is (not after_move)
    assert quarantine.exists() is after_move

    monkeypatch.undo()
    recovered = materialize_symlink_view_as_hardlinks(
        source,
        backing_source=backing,
        manifest_path=manifest_path,
    )

    _assert_materialized(source, backing, recovered)
    assert not staging.exists()
    assert quarantine.is_dir()


@pytest.mark.parametrize(
    ("window", "exit_code"),
    [
        ("before_initial_identity", 94),
        ("before_replacement_identity", 95),
    ],
)
def test_materialize_recovers_reserved_staging_before_identity_write(
    tmp_path,
    window,
    exit_code,
):
    source, backing, manifest_path = _materialization_fixture(tmp_path)
    if window == "before_replacement_identity":
        partial = multiprocessing.get_context("spawn").Process(
            target=_crash_materialization,
            args=(source, backing, manifest_path, "partial_staging"),
        )
        partial.start()
        partial.join(10)
        assert partial.exitcode == 93

    process = multiprocessing.get_context("spawn").Process(
        target=_crash_materialization,
        args=(source, backing, manifest_path, window),
    )
    process.start()
    process.join(10)

    assert process.exitcode == exit_code
    durable = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert durable["phase"] in {"reserved", "staging_replacing"}
    staging = Path(durable["staging"])
    marker = f".robodata-reservation-{durable['staging_reservation']}"
    assert (staging / marker).is_file()

    recovered = materialize_symlink_view_as_hardlinks(
        source,
        backing_source=backing,
        manifest_path=manifest_path,
    )

    _assert_materialized(source, backing, recovered)


@pytest.mark.parametrize(
    ("replacement", "window", "exit_code"),
    [
        (False, "before_construction_marker", 96),
        (False, "after_construction_marker", 97),
        (True, "before_construction_marker", 96),
        (True, "after_construction_marker", 97),
    ],
)
def test_materialize_recovers_private_construction_crash_windows(
    tmp_path,
    replacement,
    window,
    exit_code,
):
    source, backing, manifest_path = _materialization_fixture(tmp_path)
    if replacement:
        partial = multiprocessing.get_context("spawn").Process(
            target=_crash_materialization,
            args=(source, backing, manifest_path, "partial_staging"),
        )
        partial.start()
        partial.join(10)
        assert partial.exitcode == 93

    process = multiprocessing.get_context("spawn").Process(
        target=_crash_materialization,
        args=(source, backing, manifest_path, window),
    )
    process.start()
    process.join(10)

    assert process.exitcode == exit_code
    durable = json.loads(manifest_path.read_text(encoding="utf-8"))
    construction = Path(durable["staging_construction"])
    assert construction.is_dir()
    assert not Path(durable["staging"]).exists()

    recovered = materialize_symlink_view_as_hardlinks(
        source,
        backing_source=backing,
        manifest_path=manifest_path,
    )

    _assert_materialized(source, backing, recovered)
    for quarantine in recovered["staging_quarantines"]:
        path = Path(quarantine["path"])
        assert path.is_dir()
        assert (path.stat().st_dev, path.stat().st_ino) == (
            quarantine["device"],
            quarantine["inode"],
        )


@pytest.mark.parametrize(
    ("window", "exit_code"),
    [
        ("before_quarantine_move", 98),
        ("after_quarantine_move", 99),
    ],
)
def test_materialize_recovers_durable_quarantine_move(
    tmp_path,
    window,
    exit_code,
):
    source, backing, manifest_path = _materialization_fixture(tmp_path)
    partial = multiprocessing.get_context("spawn").Process(
        target=_crash_materialization,
        args=(source, backing, manifest_path, "partial_staging"),
    )
    partial.start()
    partial.join(10)
    assert partial.exitcode == 93

    process = multiprocessing.get_context("spawn").Process(
        target=_crash_materialization,
        args=(source, backing, manifest_path, window),
    )
    process.start()
    process.join(10)

    assert process.exitcode == exit_code
    durable = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert durable["phase"] == "staging_quarantining"
    quarantine = durable["staging_quarantines"][-1]

    recovered = materialize_symlink_view_as_hardlinks(
        source,
        backing_source=backing,
        manifest_path=manifest_path,
    )

    quarantine_path = Path(quarantine["path"])
    assert recovered["phase"] == "committed"
    assert not Path(recovered["staging"]).exists()
    assert quarantine_path.is_dir()
    assert (quarantine_path.stat().st_dev, quarantine_path.stat().st_ino) == (
        quarantine["device"],
        quarantine["inode"],
    )


def test_write_manifest_detects_destination_swap_after_replace(
    tmp_path,
    monkeypatch,
):
    manifest_path = tmp_path / "manifest.json"
    displaced = tmp_path / "displaced-manifest"
    real_replace = split_module.os.replace

    def swap_destination_after_replace(src, dst, **kwargs):
        result = real_replace(src, dst, **kwargs)
        if Path(dst).name == manifest_path.name:
            parent_fd = kwargs["dst_dir_fd"]
            os.rename(
                manifest_path.name,
                displaced.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            foreign_fd = os.open(
                manifest_path.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=parent_fd,
            )
            os.write(foreign_fd, b"foreign")
            os.close(foreign_fd)
        return result

    monkeypatch.setattr(split_module.os, "replace", swap_destination_after_replace)

    with pytest.raises(RuntimeError, match="destination identity changed"):
        split_module.write_manifest(manifest_path, {"phase": "test"})

    assert displaced.read_text(encoding="utf-8")
    assert manifest_path.read_text(encoding="utf-8") == "foreign"


def test_write_manifest_detects_swap_after_verified_reopen(
    tmp_path,
    monkeypatch,
):
    manifest_path = tmp_path / "manifest.json"
    displaced = tmp_path / "verified-manifest"
    real_open = split_module.os.open
    swapped = False

    def swap_after_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if (
            not swapped
            and path == manifest_path.name
            and dir_fd is not None
            and not flags & os.O_CREAT
        ):
            os.rename(
                manifest_path.name,
                displaced.name,
                src_dir_fd=dir_fd,
                dst_dir_fd=dir_fd,
            )
            foreign = real_open(
                manifest_path.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=dir_fd,
            )
            os.write(foreign, b"foreign")
            os.close(foreign)
            swapped = True
        return descriptor

    monkeypatch.setattr(split_module.os, "open", swap_after_open)

    with pytest.raises(RuntimeError, match="after verified reopen"):
        split_module.write_manifest(manifest_path, {"phase": "test"})

    assert displaced.is_file()
    assert manifest_path.read_text(encoding="utf-8") == "foreign"


def test_write_manifest_replace_failure_preserves_all_temp_artifacts(
    tmp_path,
    monkeypatch,
):
    manifest_path = tmp_path / "manifest.json"
    displaced = tmp_path / "owned-temp-displaced"
    real_replace = split_module.os.replace
    foreign_name = None

    def fail_after_temp_swap(src, dst, **kwargs):
        nonlocal foreign_name
        if Path(dst).name == manifest_path.name:
            parent_fd = kwargs["src_dir_fd"]
            foreign_name = str(src)
            real_replace(
                src,
                displaced.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            foreign = os.open(
                foreign_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=parent_fd,
            )
            os.write(foreign, b"foreign")
            os.close(foreign)
            raise OSError("injected manifest replace failure")
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(split_module.os, "replace", fail_after_temp_swap)

    with pytest.raises(OSError, match="injected manifest replace failure"):
        split_module.write_manifest(manifest_path, {"phase": "test"})

    assert foreign_name is not None
    assert displaced.read_text(encoding="utf-8")
    assert (tmp_path / foreign_name).read_bytes() == b"foreign"
    assert not manifest_path.exists()


def test_materialize_rejects_manifest_ancestor_symlink_into_backing(tmp_path):
    source, backing, _ = _materialization_fixture(tmp_path)
    alias = tmp_path / "manifest-alias"
    alias.symlink_to(backing, target_is_directory=True)

    with pytest.raises(OSError):
        materialize_symlink_view_as_hardlinks(
            source,
            backing_source=backing,
            manifest_path=alias / "nested" / "materialize.json",
        )

    assert all(path.is_symlink() for path in source.iterdir())
    assert not (backing / "nested").exists()


def test_materialize_source_swap_during_committed_manifest_fails_recovery(
    tmp_path,
    monkeypatch,
):
    source, backing, manifest_path = _materialization_fixture(tmp_path)
    displaced_installed = tmp_path / "displaced-installed"
    expected_displaced_bytes = (backing / "A" / "metacard.json").read_bytes()
    real_write_manifest = split_module.write_manifest
    swapped = False

    def swap_source_during_commit(path, plan):
        nonlocal swapped
        if plan.get("phase") == "committed" and not swapped:
            source.rename(displaced_installed)
            source.mkdir()
            (source / "foreign").write_text("preserve", encoding="utf-8")
            swapped = True
        real_write_manifest(path, plan)

    monkeypatch.setattr(split_module, "write_manifest", swap_source_during_commit)

    with pytest.raises(RuntimeError, match="identity changed during"):
        materialize_symlink_view_as_hardlinks(
            source,
            backing_source=backing,
            manifest_path=manifest_path,
        )

    durable = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert swapped
    assert durable["phase"] == "recovery_failed"
    assert (source / "foreign").read_text(encoding="utf-8") == "preserve"
    assert displaced_installed.is_dir()
    assert (displaced_installed / "A" / "metacard.json").read_bytes() == (
        expected_displaced_bytes
    )


def test_materialize_backing_file_swap_after_install_cannot_commit(
    tmp_path,
    monkeypatch,
):
    source, backing, manifest_path = _materialization_fixture(tmp_path)
    backing_file = backing / "A" / "metacard.json"
    original_bytes = backing_file.read_bytes()
    displaced = tmp_path / "displaced-backing-metacard"
    real_rename = split_module._rename_materialization_noreplace
    swapped = False

    def swap_backing_after_install(source_path, destination_path):
        nonlocal swapped
        result = real_rename(source_path, destination_path)
        if Path(destination_path) == source and not swapped:
            backing_file.rename(displaced)
            backing_file.write_bytes(b"foreign-backing")
            swapped = True
        return result

    monkeypatch.setattr(
        split_module,
        "_rename_materialization_noreplace",
        swap_backing_after_install,
    )

    with pytest.raises(RuntimeError):
        materialize_symlink_view_as_hardlinks(
            source,
            backing_source=backing,
            manifest_path=manifest_path,
        )

    durable = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert swapped
    assert durable["phase"] == "recovery_failed"
    assert displaced.read_bytes() == original_bytes
    assert backing_file.read_bytes() == b"foreign-backing"


def test_materialize_plain_file_swap_after_archive_cannot_commit(
    tmp_path,
    monkeypatch,
):
    source, backing, manifest_path, origins = _hybrid_materialization_fixture(
        tmp_path
    )
    original_bytes = origins["plain_metacard"].read_bytes()
    displaced = tmp_path / "displaced-plain-metacard"
    foreign_bytes = b'{"robot_type":"foreign"}'
    real_rename = split_module._rename_materialization_noreplace
    swapped = False

    def swap_plain_file_after_archive(source_path, destination_path):
        nonlocal swapped
        result = real_rename(source_path, destination_path)
        destination_path = Path(destination_path)
        if (
            ".symlink-view-rollback-" in destination_path.name
            and not swapped
        ):
            archived = destination_path / "A" / "metacard.json"
            archived.rename(displaced)
            archived.write_bytes(foreign_bytes)
            swapped = True
        return result

    monkeypatch.setattr(
        split_module,
        "_rename_materialization_noreplace",
        swap_plain_file_after_archive,
    )

    with pytest.raises(RuntimeError):
        materialize_symlink_view_as_hardlinks(
            source,
            backing_source=backing,
            manifest_path=manifest_path,
        )

    durable = json.loads(manifest_path.read_text(encoding="utf-8"))
    archived = Path(durable["rollback_view"]) / "A" / "metacard.json"
    assert swapped
    assert durable["phase"] != "committed"
    assert displaced.read_bytes() == original_bytes
    assert archived.read_bytes() == foreign_bytes


def test_materialize_marker_swap_fails_closed_without_deleting_foreign(
    tmp_path,
    monkeypatch,
):
    source, backing, manifest_path = _materialization_fixture(tmp_path)
    manifest = materialize_symlink_view_as_hardlinks(
        source,
        backing_source=backing,
        manifest_path=manifest_path,
    )
    marker_name = f".robodata-reservation-{manifest['staging_reservation']}"
    marker = source / marker_name
    displaced_marker = tmp_path / "displaced-owned-marker"
    real_open = split_module.os.open
    swapped = False

    def swap_marker_after_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == marker_name and dir_fd is not None and not swapped:
            marker.rename(displaced_marker)
            foreign = real_open(
                marker_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=dir_fd,
            )
            os.write(foreign, b"foreign")
            os.close(foreign)
            swapped = True
        return descriptor

    monkeypatch.setattr(split_module.os, "open", swap_marker_after_open)

    with pytest.raises(RuntimeError, match="foreign filesystem state"):
        materialize_symlink_view_as_hardlinks(
            source,
            backing_source=backing,
            manifest_path=manifest_path,
        )

    assert swapped
    assert displaced_marker.is_file()
    assert (displaced_marker.stat().st_dev, displaced_marker.stat().st_ino) == (
        manifest["staging_marker_device"],
        manifest["staging_marker_inode"],
    )
    assert marker.read_bytes() == b"foreign"


def test_materialize_source_view_swap_during_archive_fails_closed(
    tmp_path,
    monkeypatch,
):
    source, backing, manifest_path = _materialization_fixture(tmp_path)
    displaced_view = tmp_path / "displaced-original-view"
    real_rename = split_module._rename_materialization_noreplace
    foreign_archive = None

    def swap_source_before_archive(src, dst):
        nonlocal foreign_archive
        source_path = Path(src)
        destination_path = Path(dst)
        if (
            source_path == source
            and destination_path.name.startswith(
                ".task.symlink-view-rollback-"
            )
        ):
            real_rename(source, displaced_view)
            source.mkdir()
            (source / "foreign").write_text("preserve", encoding="utf-8")
            foreign_archive = destination_path
        return real_rename(src, dst)

    monkeypatch.setattr(
        split_module,
        "_rename_materialization_noreplace",
        swap_source_before_archive,
    )

    with pytest.raises(RuntimeError, match="identity changed|rollback failed"):
        materialize_symlink_view_as_hardlinks(
            source,
            backing_source=backing,
            manifest_path=manifest_path,
        )

    durable = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert durable["phase"] == "rollback_failed"
    assert displaced_view.is_dir()
    assert all(path.is_symlink() for path in displaced_view.iterdir())
    assert foreign_archive is not None
    assert (foreign_archive / "foreign").read_text(encoding="utf-8") == "preserve"
    assert not source.exists()


def test_materialize_rollback_view_swap_during_replay_restore_fails_closed(
    tmp_path,
    monkeypatch,
):
    source, backing, manifest_path = _materialization_fixture(tmp_path)
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_materialization,
        args=(source, backing, manifest_path, "after_archive"),
    )
    process.start()
    process.join(10)
    assert process.exitcode == 91
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    staging = Path(manifest["staging"])
    rollback_view = Path(manifest["rollback_view"])
    first_file = next(
        path
        for recording in staging.iterdir()
        if recording.is_dir()
        for path in recording.iterdir()
    )
    first_file.unlink()
    displaced_view = tmp_path / "displaced-replay-view"
    real_rename = split_module._rename_materialization_noreplace

    def swap_rollback_before_restore(src, dst):
        if Path(src) == rollback_view and Path(dst) == source:
            real_rename(rollback_view, displaced_view)
            rollback_view.mkdir()
            (rollback_view / "foreign").write_text(
                "preserve",
                encoding="utf-8",
            )
        return real_rename(src, dst)

    monkeypatch.setattr(
        split_module,
        "_rename_materialization_noreplace",
        swap_rollback_before_restore,
    )

    with pytest.raises(RuntimeError, match="identity changed|rollback failed"):
        materialize_symlink_view_as_hardlinks(
            source,
            backing_source=backing,
            manifest_path=manifest_path,
        )

    durable = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert durable["phase"] == "rollback_failed"
    assert displaced_view.is_dir()
    assert all(path.is_symlink() for path in displaced_view.iterdir())
    assert (source / "foreign").read_text(encoding="utf-8") == "preserve"


def test_materialize_rollback_view_swap_during_error_restore_fails_closed(
    tmp_path,
    monkeypatch,
):
    source, backing, manifest_path = _materialization_fixture(tmp_path)
    displaced_view = tmp_path / "displaced-error-view"
    real_rename = split_module._rename_materialization_noreplace
    rollback_view = None

    def fail_install_and_swap_restore(src, dst):
        nonlocal rollback_view
        source_path = Path(src)
        destination_path = Path(dst)
        if (
            source_path.name.startswith(".task.hardlink-staging-")
            and destination_path == source
        ):
            raise OSError("injected install failure")
        if (
            source_path.name.startswith(".task.symlink-view-rollback-")
            and destination_path == source
        ):
            rollback_view = source_path
            real_rename(source_path, displaced_view)
            source_path.mkdir()
            (source_path / "foreign").write_text(
                "preserve",
                encoding="utf-8",
            )
        return real_rename(src, dst)

    monkeypatch.setattr(
        split_module,
        "_rename_materialization_noreplace",
        fail_install_and_swap_restore,
    )

    with pytest.raises(RuntimeError, match="rollback failed"):
        materialize_symlink_view_as_hardlinks(
            source,
            backing_source=backing,
            manifest_path=manifest_path,
        )

    durable = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert durable["phase"] == "rollback_failed"
    assert rollback_view is not None
    assert displaced_view.is_dir()
    assert all(path.is_symlink() for path in displaced_view.iterdir())
    assert (source / "foreign").read_text(encoding="utf-8") == "preserve"


@pytest.mark.parametrize("child_kind", ["recording", "file"])
def test_materialize_rollback_restore_child_swap_cannot_mark_rolled_back(
    tmp_path,
    monkeypatch,
    child_kind,
):
    if child_kind == "recording":
        source, backing, manifest_path = _materialization_fixture(tmp_path)
        original_path = source / "A"
        original_bytes = os.readlink(original_path).encode()
    else:
        source, backing, manifest_path, origins = _hybrid_materialization_fixture(
            tmp_path
        )
        original_path = origins["plain_metacard"]
        original_bytes = original_path.read_bytes()
    displaced = tmp_path / f"displaced-{child_kind}"
    foreign_bytes = f"foreign-{child_kind}".encode()
    real_rename = split_module._rename_materialization_noreplace
    install_failed = False
    swapped = False

    def swap_child_during_restore(source_path, destination_path):
        nonlocal install_failed, swapped
        source_path = Path(source_path)
        destination_path = Path(destination_path)
        if (
            ".hardlink-staging-" in source_path.name
            and destination_path == source
            and not install_failed
        ):
            install_failed = True
            raise OSError("injected install failure")
        if (
            ".symlink-view-rollback-" in source_path.name
            and destination_path == source
            and not swapped
        ):
            archived_child = (
                source_path / "A"
                if child_kind == "recording"
                else source_path / "A" / "metacard.json"
            )
            archived_child.rename(displaced)
            if child_kind == "recording":
                archived_child.mkdir()
                (archived_child / "foreign").write_bytes(foreign_bytes)
            else:
                archived_child.write_bytes(foreign_bytes)
            swapped = True
        return real_rename(source_path, destination_path)

    monkeypatch.setattr(
        split_module,
        "_rename_materialization_noreplace",
        swap_child_during_restore,
    )

    with pytest.raises((OSError, RuntimeError)):
        materialize_symlink_view_as_hardlinks(
            source,
            backing_source=backing,
            manifest_path=manifest_path,
        )

    durable = json.loads(manifest_path.read_text(encoding="utf-8"))
    restored_root = source if source.exists() else Path(durable["rollback_view"])
    foreign_path = (
        restored_root / "A" / "foreign"
        if child_kind == "recording"
        else restored_root / "A" / "metacard.json"
    )
    assert install_failed and swapped
    assert durable["phase"] in {"rollback_failed", "recovery_failed"}
    assert foreign_path.read_bytes() == foreign_bytes
    if child_kind == "recording":
        assert os.readlink(displaced).encode() == original_bytes
    else:
        assert displaced.read_bytes() == original_bytes
