import json
import multiprocessing
import os
from pathlib import Path

import pytest

from scripts import split_raw_task_by_metadata as split_module
from scripts.split_raw_task_by_metadata import (
    apply_split,
    apply_split_as_symlink_view,
    build_split_plan,
    materialize_symlink_view_as_hardlinks,
)


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


def _crash_materialization(
    source: Path,
    backing: Path,
    manifest_path: Path,
    window: str,
) -> None:
    real_replace = split_module.os.replace
    real_link = split_module.os.link
    real_open = split_module.os.open
    real_write_manifest = split_module.write_manifest

    def replace_then_crash(src, dst, **kwargs):
        real_replace(src, dst, **kwargs)
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

    split_module.os.replace = replace_then_crash
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
    real_replace = __import__("os").replace

    def fail_staging_commit(src, dst, **kwargs):
        if (
            Path(src).name.startswith(".task.hardlink-staging-")
            and Path(dst) == source
        ):
            raise OSError("injected commit failure")
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(
        "scripts.split_raw_task_by_metadata.os.replace",
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
    real_replace = split_module.os.replace
    installed_foreign = tmp_path / "installed-foreign"

    def swap_staging_before_commit(src, dst, **kwargs):
        source_path = Path(src)
        destination_path = Path(dst)
        if (
            source_path.name.startswith(".task.hardlink-staging-")
            and destination_path == source
        ):
            real_replace(source_path, installed_foreign)
            source_path.mkdir()
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(split_module.os, "replace", swap_staging_before_commit)

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
    real_replace = split_module.os.replace
    swapped = False

    def swap_before_quarantine(src, dst, **kwargs):
        nonlocal swapped
        if (
            not swapped
            and Path(src) == staging
            and ".hardlink-quarantine-" in Path(dst).name
        ):
            real_replace(staging, displaced_owned)
            staging.mkdir()
            (staging / "foreign").write_text("preserve", encoding="utf-8")
            swapped = True
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(split_module.os, "replace", swap_before_quarantine)

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
    real_replace = split_module.os.replace

    def fail_quarantine_once(src, dst, **kwargs):
        if (
            Path(src) == staging
            and ".hardlink-quarantine-" in Path(dst).name
        ):
            if after_move:
                real_replace(src, dst, **kwargs)
            raise OSError("injected ordinary quarantine interruption")
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(split_module.os, "replace", fail_quarantine_once)

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
    real_replace = split_module.os.replace
    foreign_archive = None

    def swap_source_before_archive(src, dst, **kwargs):
        nonlocal foreign_archive
        source_path = Path(src)
        destination_path = Path(dst)
        if (
            source_path == source
            and destination_path.name.startswith(
                ".task.symlink-view-rollback-"
            )
        ):
            real_replace(source, displaced_view)
            source.mkdir()
            (source / "foreign").write_text("preserve", encoding="utf-8")
            foreign_archive = destination_path
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(split_module.os, "replace", swap_source_before_archive)

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
    real_replace = split_module.os.replace

    def swap_rollback_before_restore(src, dst, **kwargs):
        if Path(src) == rollback_view and Path(dst) == source:
            real_replace(rollback_view, displaced_view)
            rollback_view.mkdir()
            (rollback_view / "foreign").write_text(
                "preserve",
                encoding="utf-8",
            )
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(
        split_module.os,
        "replace",
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
    real_replace = split_module.os.replace
    rollback_view = None

    def fail_install_and_swap_restore(src, dst, **kwargs):
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
            real_replace(source_path, displaced_view)
            source_path.mkdir()
            (source_path / "foreign").write_text(
                "preserve",
                encoding="utf-8",
            )
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(
        split_module.os,
        "replace",
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
