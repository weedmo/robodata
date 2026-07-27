#!/usr/bin/env python3
"""Split one raw task directory into conversion-compatible metadata groups."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SERIAL_RE = re.compile(r"^\d{8}_\d{6}(?:_\d+)?$|^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _mapping_keys(value: Any) -> list[str]:
    return sorted(str(key) for key in value) if isinstance(value, dict) else []


def _normalized_action_joint_order(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): [str(joint) for joint in joints]
        for key, joints in sorted(value.items())
        if isinstance(joints, list)
    }


def conversion_signature(metadata: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return a stable digest and the fields that shape LeRobot output."""
    payload = {
        "robot_type": str(metadata.get("robot_type", "")),
        "fps": int(metadata.get("fps") or 0),
        "joint_names": [str(joint) for joint in (metadata.get("joint_names") or [])],
        "action_order": [str(name) for name in (metadata.get("action_order") or [])],
        "action_joint_order": _normalized_action_joint_order(
            metadata.get("action_joint_order")
        ),
        "action_topic_names": _mapping_keys(metadata.get("action_topics_map")),
        "camera_names": _mapping_keys(metadata.get("camera_topic_map")),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:8], payload


def _slug(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    return normalized or "unknown"


def build_split_plan(source_task: Path, keep_signature: str | None = None) -> dict:
    source_task = source_task.resolve()
    if not source_task.is_dir():
        raise ValueError(f"raw task directory not found: {source_task}")

    grouped: dict[str, dict[str, Any]] = {}
    invalid: list[dict[str, str]] = []
    for recording_dir in sorted(path for path in source_task.iterdir() if path.is_dir()):
        metacard = recording_dir / "metacard.json"
        try:
            metadata = json.loads(metacard.read_text(encoding="utf-8"))
        except Exception as exc:
            invalid.append({"recording": recording_dir.name, "error": str(exc)})
            continue
        digest, signature = conversion_signature(metadata)
        group = grouped.setdefault(
            digest,
            {"signature": signature, "recordings": []},
        )
        if group["signature"] != signature:
            raise RuntimeError(f"signature hash collision: {digest}")
        group["recordings"].append(recording_dir.name)

    if not grouped:
        raise ValueError(f"no valid metacards found under {source_task}")
    if keep_signature is None:
        keep_signature = max(grouped, key=lambda key: len(grouped[key]["recordings"]))
    if keep_signature not in grouped:
        raise ValueError(f"keep signature not found: {keep_signature}")

    groups = []
    for digest, group in sorted(
        grouped.items(), key=lambda item: (-len(item[1]["recordings"]), item[0])
    ):
        robot_type = group["signature"]["robot_type"]
        destination = (
            source_task
            if digest == keep_signature
            else source_task.with_name(
                f"{source_task.name}__{_slug(robot_type)}__{digest}"
            )
        )
        groups.append({
            "signature_id": digest,
            "signature": group["signature"],
            "count": len(group["recordings"]),
            "destination": str(destination),
            "keep_in_source": digest == keep_signature,
            "recordings": group["recordings"],
        })

    return {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_task": str(source_task),
        "keep_signature": keep_signature,
        "groups": groups,
        "invalid": invalid,
    }


def _open_directory_chain_nofollow(path: Path) -> int:
    absolute = Path(os.path.abspath(path))
    descriptor = os.open(
        absolute.anchor,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        for component in absolute.parts[1:]:
            next_descriptor = os.open(
                component,
                os.O_RDONLY
                | os.O_DIRECTORY
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _directory_chain_identities(path: Path) -> set[tuple[int, int]]:
    absolute = Path(os.path.abspath(path))
    descriptor = os.open(
        absolute.anchor,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    identities: set[tuple[int, int]] = set()
    try:
        root_info = os.fstat(descriptor)
        identities.add((root_info.st_dev, root_info.st_ino))
        for component in absolute.parts[1:]:
            next_descriptor = os.open(
                component,
                os.O_RDONLY
                | os.O_DIRECTORY
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
            info = os.fstat(descriptor)
            identities.add((info.st_dev, info.st_ino))
        return identities
    finally:
        os.close(descriptor)


def _directory_tree_identities(path: Path) -> set[tuple[int, int]]:
    descriptor = _open_directory_chain_nofollow(path)

    def collect(current_descriptor: int) -> set[tuple[int, int]]:
        current_info = os.fstat(current_descriptor)
        identities = {(current_info.st_dev, current_info.st_ino)}
        for name in os.listdir(current_descriptor):
            entry_info = os.stat(
                name,
                dir_fd=current_descriptor,
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(entry_info.st_mode):
                continue
            child_descriptor = os.open(
                name,
                os.O_RDONLY
                | os.O_DIRECTORY
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=current_descriptor,
            )
            try:
                child_info = os.fstat(child_descriptor)
                if (child_info.st_dev, child_info.st_ino) != (
                    entry_info.st_dev,
                    entry_info.st_ino,
                ):
                    raise RuntimeError(
                        "protected directory identity changed during validation"
                    )
                identities.update(collect(child_descriptor))
            finally:
                os.close(child_descriptor)
        return identities

    try:
        return collect(descriptor)
    finally:
        os.close(descriptor)


def write_manifest(path: Path, plan: dict) -> None:
    parent_descriptor = _open_directory_chain_nofollow(path.parent)
    temporary_name = f".{path.name}.tmp-{secrets.token_hex(16)}"
    descriptor = -1
    verification_descriptor = -1
    temporary_identity: tuple[int, int] | None = None
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError("manifest temporary is not a private regular file")
        temporary_identity = (info.st_dev, info.st_ino)
        verification_descriptor = os.dup(descriptor)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(plan, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        path_info = os.stat(
            temporary_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(path_info.st_mode)
            or path_info.st_nlink != 1
            or (path_info.st_dev, path_info.st_ino) != temporary_identity
        ):
            raise RuntimeError("manifest temporary identity changed before replace")
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        os.fsync(parent_descriptor)
        installed_descriptor = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
        try:
            installed_info = os.fstat(installed_descriptor)
            original_info = os.fstat(verification_descriptor)
            if (
                not stat.S_ISREG(installed_info.st_mode)
                or (installed_info.st_dev, installed_info.st_ino)
                != (original_info.st_dev, original_info.st_ino)
            ):
                raise RuntimeError(
                    "manifest destination identity changed during replace"
                )
            final_path_info = os.stat(
                path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(final_path_info.st_mode)
                or (final_path_info.st_dev, final_path_info.st_ino)
                != (installed_info.st_dev, installed_info.st_ino)
            ):
                raise RuntimeError(
                    "manifest destination changed after verified reopen"
                )
        finally:
            os.close(installed_descriptor)
        temporary_identity = None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if verification_descriptor >= 0:
            os.close(verification_descriptor)
        os.close(parent_descriptor)


def apply_split(plan: dict) -> int:
    source_task = Path(plan["source_task"])
    source_device = source_task.stat().st_dev
    moves: list[tuple[Path, Path]] = []
    for group in plan["groups"]:
        if group["keep_in_source"]:
            continue
        destination = Path(group["destination"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.parent.stat().st_dev != source_device:
            raise RuntimeError(f"destination is on a different filesystem: {destination}")
        destination.mkdir(exist_ok=True)
        for serial in group["recordings"]:
            source = source_task / serial
            target = destination / serial
            if not source.is_dir():
                raise FileNotFoundError(f"recording disappeared before move: {source}")
            if target.exists():
                raise FileExistsError(f"destination recording already exists: {target}")
            moves.append((source, target))

    for source, target in moves:
        source.rename(target)
    return len(moves)


def apply_split_as_symlink_view(plan: dict, backing_source: Path | None = None) -> int:
    """Expose metadata groups as symlink directories without rewriting source data.

    This mode is intended for read-only or UID-mapped NAS exports where the task
    directory itself cannot be modified, but its writable parent allows an atomic
    rename.  The original task becomes one hidden backing directory and each
    visible group contains relative symlinks to its recordings.
    """
    source_task = Path(plan["source_task"])
    if backing_source is None:
        backing_source = source_task.with_name(
            f".{source_task.name}__metadata_source"
        )
    backing_source = backing_source.resolve(strict=False)

    if not source_task.is_dir() or source_task.is_symlink():
        raise ValueError(f"source must be a real task directory: {source_task}")
    if backing_source.exists() or backing_source.is_symlink():
        raise FileExistsError(f"backing source already exists: {backing_source}")
    if backing_source.parent != source_task.parent:
        raise ValueError("backing source must be a sibling of the source task")

    source_device = source_task.stat().st_dev
    destinations: list[Path] = []
    recordings: list[tuple[Path, str]] = []
    seen_serials: set[str] = set()
    preexisting_destinations: set[Path] = set()
    for group in plan["groups"]:
        destination = Path(group["destination"])
        if destination.parent.stat().st_dev != source_device:
            raise RuntimeError(f"destination is on a different filesystem: {destination}")
        if destination != source_task:
            if destination.exists():
                if not destination.is_dir() or any(destination.iterdir()):
                    raise FileExistsError(
                        f"destination must be absent or an empty directory: {destination}"
                    )
                preexisting_destinations.add(destination)
            elif destination.is_symlink():
                raise FileExistsError(f"destination symlink already exists: {destination}")
        destinations.append(destination)
        for serial in group["recordings"]:
            if serial in seen_serials:
                raise RuntimeError(f"recording appears in multiple groups: {serial}")
            seen_serials.add(serial)
            recording = source_task / serial
            if not recording.is_dir():
                raise FileNotFoundError(f"recording disappeared before split: {recording}")
            recordings.append((destination, serial))

    created_links: list[Path] = []
    created_destinations: list[Path] = []
    source_task.rename(backing_source)
    try:
        for destination in destinations:
            if destination not in preexisting_destinations:
                destination.mkdir()
                created_destinations.append(destination)
        for destination, serial in recordings:
            link = destination / serial
            if link.exists() or link.is_symlink():
                raise FileExistsError(f"destination recording already exists: {link}")
            relative_target = os.path.relpath(backing_source / serial, destination)
            link.symlink_to(relative_target, target_is_directory=True)
            created_links.append(link)
    except Exception:
        for link in reversed(created_links):
            link.unlink(missing_ok=True)
        for destination in reversed(created_destinations):
            destination.rmdir()
        backing_source.rename(source_task)
        raise
    return len(created_links)


def _plain_directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.stat(follow_symlinks=False).st_mode)
    except OSError:
        return False


def _plain_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.stat(follow_symlinks=False).st_mode)
    except OSError:
        return False


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError(f"refusing to fsync non-regular file: {path}")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_plain_json(path: Path) -> dict[str, Any]:
    parent_descriptor = _open_directory_chain_nofollow(path.parent)
    descriptor = -1
    try:
        descriptor = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError(f"not a regular JSON file: {path}")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            payload = json.load(handle)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)
    if not isinstance(payload, dict):
        raise RuntimeError(f"JSON payload is not an object: {path}")
    return payload


@contextmanager
def _materialization_lock(source_task: Path):
    lock_path = source_task.with_name(
        f".{source_task.name}.hardlink-materialization.lock"
    )
    descriptor = os.open(
        lock_path,
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError(
                f"materialization lock is not a regular file: {lock_path}"
            )
        handle = os.fdopen(descriptor, "a+", encoding="utf-8")
        descriptor = -1
    except BaseException:
        os.close(descriptor)
        raise
    with handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _materialization_token() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{secrets.token_hex(4)}"


def _staging_identity(manifest: dict[str, Any]) -> tuple[int, int]:
    return manifest["staging_device"], manifest["staging_inode"]


def _open_owned_staging(path: Path, manifest: dict[str, Any]) -> int:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    info = os.fstat(descriptor)
    if (info.st_dev, info.st_ino) != _staging_identity(manifest):
        os.close(descriptor)
        raise RuntimeError(f"staging root identity changed: {path}")
    return descriptor


def _staging_reservation_marker(manifest: dict[str, Any]) -> str:
    return f".robodata-reservation-{manifest['staging_reservation']}"


def _validate_staging_reservation_marker(
    root_descriptor: int,
    manifest: dict[str, Any],
    *,
    require_manifest_identity: bool,
) -> os.stat_result:
    marker_name = _staging_reservation_marker(manifest)
    marker_descriptor = os.open(
        marker_name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=root_descriptor,
    )
    try:
        marker_info = os.fstat(marker_descriptor)
        if (
            not stat.S_ISREG(marker_info.st_mode)
            or marker_info.st_size != 0
        ):
            raise RuntimeError("staging reservation marker changed")
        if require_manifest_identity and (
            marker_info.st_dev,
            marker_info.st_ino,
        ) != (
            manifest["staging_marker_device"],
            manifest["staging_marker_inode"],
        ):
            raise RuntimeError("staging reservation marker identity changed")
        path_info = os.stat(
            marker_name,
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        if (path_info.st_dev, path_info.st_ino) != (
            marker_info.st_dev,
            marker_info.st_ino,
        ):
            raise RuntimeError(
                "staging reservation marker pathname changed"
            )
        return marker_info
    finally:
        os.close(marker_descriptor)


def _prepare_staging_construction(
    construction: Path,
    manifest: dict[str, Any],
) -> int:
    parent_descriptor = os.open(
        construction.parent,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    descriptor = -1
    try:
        try:
            os.mkdir(construction.name, mode=0o700, dir_fd=parent_descriptor)
        except FileExistsError:
            pass
        descriptor = os.open(
            construction.name,
            os.O_RDONLY
            | os.O_DIRECTORY
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
        os.fchmod(descriptor, manifest["staging_mode"])
        marker_name = _staging_reservation_marker(manifest)
        entries = os.listdir(descriptor)
        if not entries:
            marker_descriptor = os.open(
                marker_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=descriptor,
            )
            try:
                os.fsync(marker_descriptor)
            finally:
                os.close(marker_descriptor)
        elif entries != [marker_name]:
            raise RuntimeError(
                "staging construction contains ambiguous filesystem state"
            )
        _validate_staging_reservation_marker(
            descriptor,
            manifest,
            require_manifest_identity=False,
        )
        marker_descriptor = os.open(
            marker_name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=descriptor,
        )
        try:
            os.fsync(marker_descriptor)
        finally:
            os.close(marker_descriptor)
        path_info = os.stat(
            construction.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        pinned_info = os.fstat(descriptor)
        if (path_info.st_dev, path_info.st_ino) != (
            pinned_info.st_dev,
            pinned_info.st_ino,
        ):
            raise RuntimeError("staging construction identity changed")
        os.fsync(descriptor)
        os.fsync(parent_descriptor)
        return descriptor
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    finally:
        os.close(parent_descriptor)


def _publish_reserved_staging_root(
    staging: Path,
    manifest: dict[str, Any],
) -> tuple[int, int, int, int]:
    construction = Path(manifest["staging_construction"])
    marker_name = _staging_reservation_marker(manifest)
    if staging.exists() or staging.is_symlink():
        if construction.exists() or construction.is_symlink():
            raise RuntimeError(
                "both staging and construction roots exist during recovery"
            )
        descriptor = os.open(
            staging,
            os.O_RDONLY
            | os.O_DIRECTORY
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            if os.listdir(descriptor) != [marker_name]:
                raise RuntimeError(
                    "published staging root is not the reserved construction"
                )
            marker_info = _validate_staging_reservation_marker(
                descriptor,
                manifest,
                require_manifest_identity=False,
            )
            info = os.fstat(descriptor)
            return (
                info.st_dev,
                info.st_ino,
                marker_info.st_dev,
                marker_info.st_ino,
            )
        finally:
            os.close(descriptor)

    descriptor = _prepare_staging_construction(construction, manifest)
    try:
        pinned_info = os.fstat(descriptor)
        os.replace(construction, staging)
        _fsync_directory(staging.parent)
        published_info = staging.stat(follow_symlinks=False)
        if (published_info.st_dev, published_info.st_ino) != (
            pinned_info.st_dev,
            pinned_info.st_ino,
        ):
            raise RuntimeError(
                "published staging root does not match construction identity"
            )
        marker_info = _validate_staging_reservation_marker(
            descriptor,
            manifest,
            require_manifest_identity=False,
        )
        return (
            pinned_info.st_dev,
            pinned_info.st_ino,
            marker_info.st_dev,
            marker_info.st_ino,
        )
    finally:
        os.close(descriptor)


def _validate_published_staging_marker(
    staging: Path,
    manifest: dict[str, Any],
) -> None:
    descriptor = _open_owned_staging(staging, manifest)
    try:
        _validate_staging_reservation_marker(
            descriptor,
            manifest,
            require_manifest_identity=True,
        )
    finally:
        os.close(descriptor)


def _validate_materialization_quarantines(
    manifest: dict[str, Any],
) -> None:
    quarantines = manifest.get("staging_quarantines")
    if not isinstance(quarantines, list):
        raise RuntimeError("materialization manifest has invalid quarantines")
    for index, quarantine in enumerate(quarantines):
        path = Path(quarantine["path"])
        expected = (quarantine["device"], quarantine["inode"])
        if not all(isinstance(value, int) for value in expected):
            raise RuntimeError("materialization quarantine identity is invalid")
        if not path.exists() and not path.is_symlink():
            if (
                manifest["phase"] == "staging_quarantining"
                and index == len(quarantines) - 1
            ):
                continue
            raise RuntimeError(
                f"materialization quarantine disappeared: {path}"
            )
        descriptor = os.open(
            path,
            os.O_RDONLY
            | os.O_DIRECTORY
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            info = os.fstat(descriptor)
            if (info.st_dev, info.st_ino) != expected:
                raise RuntimeError(
                    f"materialization quarantine identity changed: {path}"
                )
            marker_name = quarantine["marker_name"]
            marker_descriptor = os.open(
                marker_name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            try:
                marker_info = os.fstat(marker_descriptor)
                if (
                    not stat.S_ISREG(marker_info.st_mode)
                    or marker_info.st_size != 0
                    or (marker_info.st_dev, marker_info.st_ino)
                    != (
                        quarantine["marker_device"],
                        quarantine["marker_inode"],
                    )
                ):
                    raise RuntimeError(
                        f"materialization quarantine marker changed: {path}"
                    )
                marker_path_info = os.stat(
                    marker_name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                if (marker_path_info.st_dev, marker_path_info.st_ino) != (
                    marker_info.st_dev,
                    marker_info.st_ino,
                ):
                    raise RuntimeError(
                        f"materialization quarantine marker path changed: {path}"
                    )
            finally:
                os.close(marker_descriptor)
        finally:
            os.close(descriptor)


def _quarantine_materialization_staging(
    staging: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
) -> None:
    if manifest["phase"] != "staging_quarantining":
        descriptor = _open_owned_staging(staging, manifest)
        try:
            info = os.fstat(descriptor)
            marker_info = _validate_staging_reservation_marker(
                descriptor,
                manifest,
                require_manifest_identity=True,
            )
        finally:
            os.close(descriptor)
        token = secrets.token_hex(16)
        quarantine = {
            "path": str(
                staging.with_name(
                    f".{staging.name}.hardlink-quarantine-{token}"
                )
            ),
            "device": info.st_dev,
            "inode": info.st_ino,
            "marker_name": _staging_reservation_marker(manifest),
            "marker_device": marker_info.st_dev,
            "marker_inode": marker_info.st_ino,
        }
        manifest.setdefault("staging_quarantines", []).append(quarantine)
        _write_materialization_phase(
            manifest_path,
            manifest,
            "staging_quarantining",
        )
    else:
        quarantine = manifest["staging_quarantines"][-1]

    quarantine_path = Path(quarantine["path"])
    expected = (quarantine["device"], quarantine["inode"])
    staging_present = staging.exists() or staging.is_symlink()
    quarantine_present = (
        quarantine_path.exists() or quarantine_path.is_symlink()
    )
    if staging_present and quarantine_present:
        raise RuntimeError(
            "both public staging and quarantine exist during recovery"
        )
    if staging_present:
        descriptor = _open_owned_staging(staging, manifest)
        try:
            pinned_info = os.fstat(descriptor)
            if (pinned_info.st_dev, pinned_info.st_ino) != expected:
                raise RuntimeError(
                    "staging identity differs from durable quarantine reservation"
                )
            os.replace(staging, quarantine_path)
            _fsync_directory(staging.parent)
            moved_info = quarantine_path.stat(follow_symlinks=False)
            if (moved_info.st_dev, moved_info.st_ino) != expected:
                raise RuntimeError(
                    "quarantined staging does not match pinned ownership"
                )
        finally:
            os.close(descriptor)
    elif not quarantine_present:
        raise RuntimeError(
            "durable quarantine reservation has no owned staging root"
        )
    _validate_materialization_quarantines(manifest)
    token = secrets.token_hex(32)
    manifest["staging_device"] = None
    manifest["staging_inode"] = None
    manifest["staging_marker_device"] = None
    manifest["staging_marker_inode"] = None
    manifest["staging_reservation"] = token
    manifest["staging_construction"] = str(
        staging.with_name(
            f".{staging.name}.hardlink-construction-{token}"
        )
    )
    _write_materialization_phase(
        manifest_path,
        manifest,
        "staging_replacing",
    )


def _ensure_materialization_staging_identity(
    staging: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
) -> None:
    if manifest["phase"] == "staging_quarantining":
        _quarantine_materialization_staging(
            staging,
            manifest_path,
            manifest,
        )
    if all(
        isinstance(manifest.get(field), int)
        for field in (
            "staging_device",
            "staging_inode",
            "staging_marker_device",
            "staging_marker_inode",
        )
    ):
        if staging.exists() or staging.is_symlink():
            _validate_published_staging_marker(staging, manifest)
        return
    (
        manifest["staging_device"],
        manifest["staging_inode"],
        manifest["staging_marker_device"],
        manifest["staging_marker_inode"],
    ) = _publish_reserved_staging_root(staging, manifest)
    _write_materialization_phase(
        manifest_path,
        manifest,
        "preparing",
    )
    _validate_published_staging_marker(staging, manifest)


def _reserve_materialization_staging_replacement(
    staging: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
) -> None:
    _quarantine_materialization_staging(
        staging,
        manifest_path,
        manifest,
    )
    _ensure_materialization_staging_identity(
        staging,
        manifest_path,
        manifest,
    )


def _plan_materialization_view(
    source_task: Path,
    backing_source: Path,
) -> list[dict[str, Any]]:
    try:
        view_entries = sorted(source_task.iterdir())
    except OSError as exc:
        raise ValueError(f"cannot list symlink view: {source_task}") from exc
    if not view_entries:
        raise ValueError("symlink view has no recordings")

    planned: list[dict[str, Any]] = []
    seen_serials: set[str] = set()
    for view_entry in view_entries:
        if not _SERIAL_RE.fullmatch(view_entry.name):
            raise ValueError(f"unexpected entry in symlink view: {view_entry.name}")
        if view_entry.name in seen_serials:
            raise RuntimeError(f"duplicate recording in symlink view: {view_entry.name}")
        seen_serials.add(view_entry.name)
        view_info = view_entry.lstat()
        if not stat.S_ISLNK(view_info.st_mode):
            raise ValueError(f"recording view entry is not a symlink: {view_entry}")
        raw_target = os.readlink(view_entry)
        target = view_entry.resolve(strict=True)
        if target.parent != backing_source:
            raise ValueError(
                f"recording symlink resolves outside backing source: {view_entry}"
            )
        if target.name != view_entry.name or not _plain_directory(target):
            raise ValueError(f"recording target is not a matching plain directory: {target}")

        try:
            source_files = sorted(target.iterdir())
        except OSError as exc:
            raise ValueError(f"cannot list backing recording: {target}") from exc
        if not source_files:
            raise ValueError(f"backing recording has no files: {target}")
        required_files = {"metacard.json", f"{view_entry.name}_0.mcap"}
        source_names = {source_file.name for source_file in source_files}
        missing_files = sorted(required_files - source_names)
        if missing_files:
            raise ValueError(
                f"backing recording is incomplete ({missing_files}): {target}"
            )
        files: list[dict[str, Any]] = []
        for source_file in source_files:
            if not _plain_regular_file(source_file):
                raise ValueError(
                    f"backing recording contains a non-regular entry: {source_file}"
                )
            info = source_file.stat(follow_symlinks=False)
            files.append(
                {
                    "name": source_file.name,
                    "source": str(source_file),
                    "device": info.st_dev,
                    "inode": info.st_ino,
                    "size": info.st_size,
                    "mode": stat.S_IMODE(info.st_mode),
                }
            )
        planned.append(
            {
                "serial": view_entry.name,
                "view_device": view_info.st_dev,
                "view_inode": view_info.st_ino,
                "view_target": raw_target,
                "source": str(target),
                "source_mode": stat.S_IMODE(target.stat().st_mode),
                "files": files,
            }
        )
    return planned


_MATERIALIZATION_PHASES = frozenset(
    {
        "reserved",
        "staging_quarantining",
        "staging_replacing",
        "preparing",
        "prepared",
        "view_archived",
        "committed",
        "rolled_back",
        "rollback_failed",
        "recovery_failed",
    }
)


def _load_materialization_manifest(
    manifest_path: Path,
    *,
    source_task: Path,
    backing_source: Path,
) -> dict[str, Any]:
    if not _plain_regular_file(manifest_path) or manifest_path.is_symlink():
        raise RuntimeError(
            f"materialization recovery requires a plain manifest: {manifest_path}"
        )
    try:
        manifest = _read_plain_json(manifest_path)
        if (
            not isinstance(manifest, dict)
            or manifest["version"] != 1
            or manifest["operation"] != "materialize_symlink_view_as_hardlinks"
            or manifest["phase"] not in _MATERIALIZATION_PHASES
            or manifest["source_task"] != str(source_task)
            or manifest["backing_source"] != str(backing_source)
        ):
            raise ValueError("manifest identity does not match this materialization")

        staging = Path(manifest["staging"])
        construction = Path(manifest["staging_construction"])
        rollback_view = Path(manifest["rollback_view"])
        staging_prefix = f".{source_task.name}.hardlink-staging-"
        rollback_prefix = f".{source_task.name}.symlink-view-rollback-"
        if (
            staging.parent != source_task.parent
            or construction.parent != source_task.parent
            or rollback_view.parent != source_task.parent
            or not staging.name.startswith(staging_prefix)
            or not rollback_view.name.startswith(rollback_prefix)
            or staging.name.removeprefix(staging_prefix)
            != rollback_view.name.removeprefix(rollback_prefix)
            or not staging.name.removeprefix(staging_prefix)
            or ".hardlink-construction-" not in construction.name
        ):
            raise ValueError("manifest transaction paths are invalid")
        if (
            not isinstance(manifest["staging_mode"], int)
            or not isinstance(manifest["staging_reservation"], str)
            or not manifest["staging_reservation"]
        ):
            raise ValueError("manifest staging identity is invalid")
        staging_identity = (
            manifest.get("staging_device"),
            manifest.get("staging_inode"),
            manifest.get("staging_marker_device"),
            manifest.get("staging_marker_inode"),
        )
        if manifest["phase"] in {
            "reserved",
            "staging_replacing",
            "rolled_back",
        }:
            if any(value is not None for value in staging_identity):
                raise ValueError("reserved manifest already has staging identity")
        elif not all(isinstance(value, int) for value in staging_identity):
            raise ValueError("manifest staging identity is invalid")
        quarantines = manifest["staging_quarantines"]
        if not isinstance(quarantines, list):
            raise ValueError("manifest quarantines are invalid")
        for quarantine in quarantines:
            quarantine_path = Path(quarantine["path"])
            if (
                quarantine_path.parent != source_task.parent
                or ".hardlink-quarantine-" not in quarantine_path.name
                or not isinstance(quarantine["device"], int)
                or not isinstance(quarantine["inode"], int)
                or not isinstance(quarantine["marker_name"], str)
                or not quarantine["marker_name"].startswith(
                    ".robodata-reservation-"
                )
                or not isinstance(quarantine["marker_device"], int)
                or not isinstance(quarantine["marker_inode"], int)
            ):
                raise ValueError("manifest quarantine identity is invalid")
        if not all(
            isinstance(manifest[field], int)
            for field in ("source_view_device", "source_view_inode", "source_view_mode")
        ):
            raise ValueError("manifest source view identity is invalid")

        recordings = manifest["recordings"]
        if not isinstance(recordings, list) or not recordings:
            raise ValueError("manifest has no recordings")
        seen_serials: set[str] = set()
        for recording in recordings:
            serial = recording["serial"]
            if (
                not isinstance(serial, str)
                or not _SERIAL_RE.fullmatch(serial)
                or serial in seen_serials
                or Path(recording["source"]) != backing_source / serial
                or not isinstance(recording["view_device"], int)
                or not isinstance(recording["view_inode"], int)
                or not isinstance(recording["view_target"], str)
            ):
                raise ValueError("manifest recording identity is invalid")
            seen_serials.add(serial)
            if not isinstance(recording["source_mode"], int):
                raise ValueError("manifest recording mode is invalid")
            files = recording["files"]
            if not isinstance(files, list) or not files:
                raise ValueError("manifest recording has no files")
            seen_files: set[str] = set()
            for source_file in files:
                name = source_file["name"]
                if (
                    not isinstance(name, str)
                    or Path(name).name != name
                    or name in seen_files
                    or Path(source_file["source"]) != backing_source / serial / name
                    or not all(
                        isinstance(source_file[field], int)
                        for field in ("device", "inode", "size", "mode")
                    )
                ):
                    raise ValueError("manifest source file identity is invalid")
                seen_files.add(name)
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
        OSError,
        RuntimeError,
    ) as exc:
        raise RuntimeError(
            f"invalid materialization recovery manifest: {manifest_path}"
        ) from exc
    return manifest


def _validate_materialization_backing(
    manifest: dict[str, Any],
    backing_source: Path,
) -> None:
    for recording in manifest["recordings"]:
        source_recording = backing_source / recording["serial"]
        if (
            not _plain_directory(source_recording)
            or stat.S_IMODE(source_recording.stat().st_mode)
            != recording["source_mode"]
        ):
            raise RuntimeError(
                f"materialization backing changed: {source_recording}"
            )
        expected_files = {source_file["name"] for source_file in recording["files"]}
        try:
            actual_files = {path.name for path in source_recording.iterdir()}
        except OSError as exc:
            raise RuntimeError(
                f"cannot inspect materialization backing: {source_recording}"
            ) from exc
        if actual_files != expected_files:
            raise RuntimeError(
                f"materialization backing changed: {source_recording}"
            )
        for source_file in recording["files"]:
            path = Path(source_file["source"])
            if not _plain_regular_file(path):
                raise RuntimeError(f"materialization backing changed: {path}")
            info = path.stat(follow_symlinks=False)
            if (
                info.st_dev,
                info.st_ino,
                info.st_size,
                stat.S_IMODE(info.st_mode),
            ) != (
                source_file["device"],
                source_file["inode"],
                source_file["size"],
                source_file["mode"],
            ):
                raise RuntimeError(f"materialization backing changed: {path}")


def _classify_materialization_view(
    path: Path,
    manifest: dict[str, Any],
) -> str:
    if not path.exists() and not path.is_symlink():
        return "missing"
    if not _plain_directory(path) or path.is_symlink():
        return "foreign"
    root_info = path.stat(follow_symlinks=False)
    if (
        root_info.st_dev,
        root_info.st_ino,
        stat.S_IMODE(root_info.st_mode),
    ) != (
        manifest["source_view_device"],
        manifest["source_view_inode"],
        manifest["source_view_mode"],
    ):
        return "foreign"
    recordings = manifest["recordings"]
    try:
        entries = sorted(path.iterdir())
        if [entry.name for entry in entries] != [
            recording["serial"] for recording in recordings
        ]:
            return "foreign"
        for entry, recording in zip(entries, recordings, strict=True):
            info = entry.lstat()
            if (
                not stat.S_ISLNK(info.st_mode)
                or (info.st_dev, info.st_ino)
                != (recording["view_device"], recording["view_inode"])
                or os.readlink(entry) != recording["view_target"]
                or entry.resolve(strict=True) != Path(recording["source"])
            ):
                return "foreign"
    except OSError:
        return "foreign"
    return "view"


def _open_owned_materialization_view(
    path: Path,
    manifest: dict[str, Any],
) -> int:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    info = os.fstat(descriptor)
    if (
        info.st_dev,
        info.st_ino,
        stat.S_IMODE(info.st_mode),
    ) != (
        manifest["source_view_device"],
        manifest["source_view_inode"],
        manifest["source_view_mode"],
    ):
        os.close(descriptor)
        raise RuntimeError(f"source view root identity changed: {path}")
    return descriptor


def _move_owned_materialization_view(
    source: Path,
    destination: Path,
    manifest: dict[str, Any],
) -> None:
    descriptor = _open_owned_materialization_view(source, manifest)
    try:
        path_info = source.stat(follow_symlinks=False)
        pinned_info = os.fstat(descriptor)
        if (path_info.st_dev, path_info.st_ino) != (
            pinned_info.st_dev,
            pinned_info.st_ino,
        ):
            raise RuntimeError(
                "source view identity changed before transaction rename"
            )
        os.replace(source, destination)
        _fsync_directory(source.parent)
        installed_info = destination.stat(follow_symlinks=False)
        if (installed_info.st_dev, installed_info.st_ino) != (
            pinned_info.st_dev,
            pinned_info.st_ino,
        ):
            raise RuntimeError(
                "source view identity changed during transaction rename"
            )
        if _classify_materialization_view(destination, manifest) != "view":
            raise RuntimeError(
                "renamed source view does not match the materialization manifest"
            )
    finally:
        os.close(descriptor)


def _classify_materialization_tree(
    path: Path,
    manifest: dict[str, Any],
    *,
    require_staging_identity: bool = False,
) -> str:
    if not path.exists() and not path.is_symlink():
        return "missing"
    if not _plain_directory(path) or path.is_symlink():
        return "foreign"
    if require_staging_identity:
        root_info = path.stat(follow_symlinks=False)
        if (
            root_info.st_dev,
            root_info.st_ino,
            stat.S_IMODE(root_info.st_mode),
        ) != (
            manifest["staging_device"],
            manifest["staging_inode"],
            manifest["staging_mode"],
        ):
            return "foreign"
    try:
        _validate_published_staging_marker(path, manifest)
    except (OSError, RuntimeError):
        return "foreign"
    expected_recordings = {
        recording["serial"]: recording for recording in manifest["recordings"]
    }
    complete = True
    try:
        marker_name = _staging_reservation_marker(manifest)
        actual_entries = {entry.name: entry for entry in path.iterdir()}
        if marker_name not in actual_entries:
            return "foreign"
        actual_entries.pop(marker_name)
        actual_recordings = actual_entries
        if not set(actual_recordings).issubset(expected_recordings):
            return "foreign"
        if set(actual_recordings) != set(expected_recordings):
            complete = False
        for serial, target_recording in actual_recordings.items():
            recording = expected_recordings[serial]
            if (
                not _plain_directory(target_recording)
                or target_recording.is_symlink()
                or stat.S_IMODE(target_recording.stat().st_mode)
                != recording["source_mode"]
            ):
                return "foreign"
            expected_files = {
                source_file["name"]: source_file
                for source_file in recording["files"]
            }
            actual_files = {entry.name: entry for entry in target_recording.iterdir()}
            if not set(actual_files).issubset(expected_files):
                return "foreign"
            if set(actual_files) != set(expected_files):
                complete = False
            for name, target_file in actual_files.items():
                source_file = expected_files[name]
                if not _plain_regular_file(target_file):
                    return "foreign"
                info = target_file.stat(follow_symlinks=False)
                if (
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                    stat.S_IMODE(info.st_mode),
                ) != (
                    source_file["device"],
                    source_file["inode"],
                    source_file["size"],
                    source_file["mode"],
                ):
                    return "foreign"
        _validate_published_staging_marker(path, manifest)
    except (OSError, RuntimeError):
        return "foreign"
    return "hardlink" if complete else "partial"


def _materialization_states(
    source_task: Path,
    staging: Path,
    rollback_view: Path,
    manifest: dict[str, Any],
) -> tuple[str, str, str]:
    source_view = _classify_materialization_view(source_task, manifest)
    source_tree = _classify_materialization_tree(
        source_task,
        manifest,
        require_staging_identity=True,
    )
    source_state = source_view if source_view == "view" else source_tree
    staging_state = _classify_materialization_tree(
        staging,
        manifest,
        require_staging_identity=True,
    )
    rollback_state = _classify_materialization_view(rollback_view, manifest)
    return source_state, staging_state, rollback_state


def _build_materialization_staging(
    staging: Path,
    manifest: dict[str, Any],
) -> None:
    staging_descriptor = _open_owned_staging(staging, manifest)
    try:
        os.fchmod(staging_descriptor, manifest["staging_mode"])
        for recording in manifest["recordings"]:
            serial = recording["serial"]
            os.mkdir(
                serial,
                mode=recording["source_mode"],
                dir_fd=staging_descriptor,
            )
            recording_descriptor = os.open(
                serial,
                os.O_RDONLY
                | os.O_DIRECTORY
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=staging_descriptor,
            )
            try:
                os.fchmod(recording_descriptor, recording["source_mode"])
                source_descriptor = os.open(
                    recording["source"],
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | getattr(os, "O_NOFOLLOW", 0),
                )
                try:
                    for source_file in recording["files"]:
                        source_info = os.stat(
                            source_file["name"],
                            dir_fd=source_descriptor,
                            follow_symlinks=False,
                        )
                        if (
                            not stat.S_ISREG(source_info.st_mode)
                            or (source_info.st_dev, source_info.st_ino)
                            != (source_file["device"], source_file["inode"])
                        ):
                            raise RuntimeError(
                                "materialization backing changed during staging"
                            )
                        os.link(
                            source_file["name"],
                            source_file["name"],
                            src_dir_fd=source_descriptor,
                            dst_dir_fd=recording_descriptor,
                            follow_symlinks=False,
                        )
                        target_descriptor = os.open(
                            source_file["name"],
                            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                            dir_fd=recording_descriptor,
                        )
                        try:
                            target_info = os.fstat(target_descriptor)
                            if (
                                target_info.st_dev,
                                target_info.st_ino,
                            ) != (
                                source_file["device"],
                                source_file["inode"],
                            ):
                                raise RuntimeError(
                                    "hardlink target identity verification failed"
                                )
                            os.fsync(target_descriptor)
                        finally:
                            os.close(target_descriptor)
                finally:
                    os.close(source_descriptor)
                os.fsync(recording_descriptor)
            finally:
                os.close(recording_descriptor)
        os.fsync(staging_descriptor)
    finally:
        os.close(staging_descriptor)
    _fsync_directory(staging.parent)
    if (
        _classify_materialization_tree(
            staging,
            manifest,
            require_staging_identity=True,
        )
        != "hardlink"
    ):
        raise RuntimeError("hardlink staging verification failed")


def _write_materialization_phase(
    manifest_path: Path,
    manifest: dict[str, Any],
    phase: str,
) -> None:
    manifest["phase"] = phase
    if phase not in {"rolled_back", "rollback_failed", "recovery_failed"}:
        manifest.pop("error", None)
        manifest.pop("rollback_error", None)
        manifest.pop("rolled_back_at", None)
    if phase == "committed":
        manifest.setdefault("committed_at", datetime.now(timezone.utc).isoformat())
    write_manifest(manifest_path, manifest)


def _commit_materialization_with_pinned_source(
    source_task: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
    *,
    descriptor: int | None = None,
) -> None:
    owned_descriptor = descriptor is None
    if descriptor is None:
        descriptor = os.open(
            source_task,
            os.O_RDONLY
            | os.O_DIRECTORY
            | getattr(os, "O_NOFOLLOW", 0),
        )
    try:
        pinned_info = os.fstat(descriptor)
        path_info = source_task.stat(follow_symlinks=False)
        if (
            path_info.st_dev,
            path_info.st_ino,
        ) != (
            pinned_info.st_dev,
            pinned_info.st_ino,
        ):
            raise RuntimeError(
                "installed source identity changed before committed manifest"
            )
        if (
            _classify_materialization_tree(
                source_task,
                manifest,
                require_staging_identity=True,
            )
            != "hardlink"
        ):
            raise RuntimeError(
                "installed materialization does not match the manifest"
            )
        _write_materialization_phase(
            manifest_path,
            manifest,
            "committed",
        )
        durable_path_info = source_task.stat(follow_symlinks=False)
        if (
            durable_path_info.st_dev,
            durable_path_info.st_ino,
        ) != (
            pinned_info.st_dev,
            pinned_info.st_ino,
        ):
            message = (
                "installed source identity changed during committed manifest write"
            )
            manifest["error"] = message
            _write_materialization_phase(
                manifest_path,
                manifest,
                "recovery_failed",
            )
            raise RuntimeError(message)
    finally:
        if owned_descriptor:
            os.close(descriptor)


def _rollback_materialization_after_error(
    *,
    source_task: Path,
    staging: Path,
    rollback_view: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
    original_error: BaseException,
) -> bool:
    if manifest.get("phase") == "staging_quarantining":
        raise RuntimeError(
            "materialization quarantine transition interrupted; "
            "durable state remains resumable"
        ) from original_error
    source_state, staging_state, rollback_state = _materialization_states(
        source_task,
        staging,
        rollback_view,
        manifest,
    )
    if source_state == "hardlink" and rollback_state == "view":
        try:
            _write_materialization_phase(manifest_path, manifest, "committed")
        except BaseException:
            pass
        return True

    rollback_error: BaseException | None = None
    try:
        if (
            source_state == "missing"
            and rollback_state == "view"
            and staging_state in {"missing", "partial", "hardlink"}
        ):
            _move_owned_materialization_view(
                rollback_view,
                source_task,
                manifest,
            )
            source_state = "view"
            rollback_state = "missing"
        if (
            source_state == "view"
            and rollback_state == "missing"
            and staging_state in {"partial", "hardlink"}
        ):
            _quarantine_materialization_staging(
                staging,
                manifest_path,
                manifest,
            )
            staging_state = "missing"
        if not (
            source_state == "view"
            and staging_state == "missing"
            and rollback_state == "missing"
        ):
            raise RuntimeError("filesystem state is not safely rollbackable")
    except BaseException as exc:
        rollback_error = exc

    manifest["rolled_back_at"] = datetime.now(timezone.utc).isoformat()
    manifest["error"] = str(original_error)
    if rollback_error is not None:
        manifest["rollback_error"] = str(rollback_error)
    else:
        manifest.pop("rollback_error", None)
    try:
        _write_materialization_phase(
            manifest_path,
            manifest,
            "rollback_failed" if rollback_error else "rolled_back",
        )
    except BaseException:
        pass
    if rollback_error is not None:
        raise RuntimeError(
            f"materialization rollback failed: {rollback_error}"
        ) from original_error
    return False


def _roll_forward_materialization(
    *,
    source_task: Path,
    staging: Path,
    rollback_view: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    _validate_materialization_quarantines(manifest)
    _ensure_materialization_staging_identity(
        staging,
        manifest_path,
        manifest,
    )
    source_state, staging_state, rollback_state = _materialization_states(
        source_task,
        staging,
        rollback_view,
        manifest,
    )
    if "foreign" in {source_state, staging_state, rollback_state}:
        raise RuntimeError("materialization recovery found foreign filesystem state")
    if rollback_state == "missing":
        if source_state != "view":
            raise RuntimeError(
                "materialization recovery is missing the original rollback view"
            )
    elif rollback_state == "view":
        if source_state == "view":
            raise RuntimeError(
                "materialization recovery found an ambiguous duplicate original view"
            )
        if source_state not in {"missing", "hardlink"}:
            raise RuntimeError("materialization recovery found an invalid source state")

    if source_state == "hardlink":
        if staging_state in {"partial", "hardlink"}:
            raise RuntimeError(
                "materialization recovery found an unexpected public staging root"
            )
        if manifest["phase"] == "committed" and staging_state == "missing":
            return manifest
        _commit_materialization_with_pinned_source(
            source_task,
            manifest_path,
            manifest,
        )
        return manifest

    try:
        if (
            rollback_state == "view"
            and source_state == "missing"
            and staging_state in {"missing", "partial"}
        ):
            _move_owned_materialization_view(
                rollback_view,
                source_task,
                manifest,
            )
            source_state = "view"
            rollback_state = "missing"

        if source_state == "view" and rollback_state == "missing":
            if staging_state == "missing":
                raise RuntimeError(
                    "owned staging root disappeared before population"
                )
            if staging_state == "partial":
                staging_descriptor = _open_owned_staging(staging, manifest)
                try:
                    staging_is_empty = os.listdir(staging_descriptor) == [
                        _staging_reservation_marker(manifest)
                    ]
                finally:
                    os.close(staging_descriptor)
                if not staging_is_empty:
                    _reserve_materialization_staging_replacement(
                        staging,
                        manifest_path,
                        manifest,
                    )
                _build_materialization_staging(staging, manifest)
                staging_state = "hardlink"
            elif staging_state != "hardlink":
                _reserve_materialization_staging_replacement(
                    staging,
                    manifest_path,
                    manifest,
                )
                _build_materialization_staging(staging, manifest)
                staging_state = "hardlink"
            _write_materialization_phase(manifest_path, manifest, "prepared")
            _move_owned_materialization_view(
                source_task,
                rollback_view,
                manifest,
            )
            source_state = "missing"
            rollback_state = "view"

        if (
            source_state == "missing"
            and staging_state == "hardlink"
            and rollback_state == "view"
        ):
            _write_materialization_phase(
                manifest_path,
                manifest,
                "view_archived",
            )
            staging_descriptor = _open_owned_staging(staging, manifest)
            try:
                staging_info = staging.stat(follow_symlinks=False)
                if (
                    staging_info.st_dev,
                    staging_info.st_ino,
                ) != _staging_identity(manifest):
                    raise RuntimeError(
                        "staging root identity changed before commit rename"
                    )
                os.replace(staging, source_task)
                _fsync_directory(source_task.parent)
                installed_info = source_task.stat(follow_symlinks=False)
                pinned_info = os.fstat(staging_descriptor)
                if (
                    installed_info.st_dev,
                    installed_info.st_ino,
                ) != (
                    pinned_info.st_dev,
                    pinned_info.st_ino,
                ):
                    raise RuntimeError(
                        "installed staging root identity does not match the pinned root"
                    )
                source_state = _classify_materialization_tree(
                    source_task,
                    manifest,
                    require_staging_identity=True,
                )
                if source_state != "hardlink":
                    raise RuntimeError(
                        "installed materialization does not match the manifest"
                    )
                _commit_materialization_with_pinned_source(
                    source_task,
                    manifest_path,
                    manifest,
                    descriptor=staging_descriptor,
                )
            finally:
                os.close(staging_descriptor)
            staging_state = "missing"
            return manifest
        raise RuntimeError("materialization recovery could not reach a safe state")
    except BaseException as original_error:
        if manifest.get("phase") == "recovery_failed":
            raise
        _rollback_materialization_after_error(
            source_task=source_task,
            staging=staging,
            rollback_view=rollback_view,
            manifest_path=manifest_path,
            manifest=manifest,
            original_error=original_error,
        )
        raise


def materialize_symlink_view_as_hardlinks(
    source_task: Path,
    *,
    backing_source: Path,
    manifest_path: Path,
) -> dict[str, Any]:
    """Replace one symlink recording view with plain directories and hardlinks.

    The old symlink view and hidden backing directory remain as rollback
    artifacts after a successful commit. Superseded staging roots are moved
    intact to manifest-tracked quarantine paths for rollback and audit; this
    operation never recursively deletes them.
    """
    source_task = source_task.parent.resolve() / source_task.name
    backing_source = backing_source.parent.resolve() / backing_source.name
    manifest_path = Path(os.path.abspath(manifest_path))

    manifest_parent_descriptor = _open_directory_chain_nofollow(
        manifest_path.parent
    )
    manifest_parent_identities = _directory_chain_identities(
        manifest_path.parent
    )
    os.close(manifest_parent_descriptor)

    if not _plain_directory(backing_source) or backing_source.is_symlink():
        raise ValueError(f"backing source must be a plain directory: {backing_source}")
    if source_task.parent != backing_source.parent:
        raise ValueError("backing source must be a sibling of the source task")
    if not backing_source.name.startswith("."):
        raise ValueError("backing source must remain hidden")
    if source_task.parent.stat().st_dev != backing_source.stat().st_dev:
        raise RuntimeError("source task and backing source are on different filesystems")
    protected_identities = _directory_tree_identities(backing_source)
    if _plain_directory(source_task):
        protected_identities.update(_directory_tree_identities(source_task))
    if protected_identities & manifest_parent_identities:
        raise ValueError(
            "materialization manifest parent resolves inside a protected directory"
        )
    for protected_root in (source_task, backing_source):
        try:
            manifest_path.relative_to(protected_root)
        except ValueError:
            continue
        raise ValueError(
            "materialization manifest must be outside source and backing directories"
        )

    with _materialization_lock(source_task):
        if manifest_path.exists() or manifest_path.is_symlink():
            manifest = _load_materialization_manifest(
                manifest_path,
                source_task=source_task,
                backing_source=backing_source,
            )
        else:
            orphaned = [
                *source_task.parent.glob(
                    f".{source_task.name}.hardlink-staging-*"
                ),
                *source_task.parent.glob(
                    f".{source_task.name}.symlink-view-rollback-*"
                ),
                *source_task.parent.glob(
                    f"..{source_task.name}.hardlink-staging-*"
                    ".hardlink-construction-*"
                ),
                *source_task.parent.glob(
                    f"..{source_task.name}.hardlink-staging-*"
                    ".hardlink-quarantine-*"
                ),
            ]
            if orphaned:
                raise RuntimeError(
                    "materialization recovery found artifacts without a manifest"
                )
            if not _plain_directory(source_task) or source_task.is_symlink():
                raise ValueError(f"source must be a plain task directory: {source_task}")
            planned = _plan_materialization_view(source_task, backing_source)
            token = _materialization_token()
            staging = source_task.with_name(
                f".{source_task.name}.hardlink-staging-{token}"
            )
            rollback_view = source_task.with_name(
                f".{source_task.name}.symlink-view-rollback-{token}"
            )
            source_view_info = source_task.stat(follow_symlinks=False)
            staging_mode = stat.S_IMODE(source_view_info.st_mode)
            staging_reservation = secrets.token_hex(32)
            manifest = {
                "version": 1,
                "operation": "materialize_symlink_view_as_hardlinks",
                "phase": "reserved",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source_task": str(source_task),
                "backing_source": str(backing_source),
                "staging": str(staging),
                "staging_device": None,
                "staging_inode": None,
                "staging_marker_device": None,
                "staging_marker_inode": None,
                "staging_mode": staging_mode,
                "staging_reservation": staging_reservation,
                "staging_construction": str(
                    staging.with_name(
                        f".{staging.name}.hardlink-construction-"
                        f"{staging_reservation}"
                    )
                ),
                "staging_quarantines": [],
                "rollback_view": str(rollback_view),
                "source_view_device": source_view_info.st_dev,
                "source_view_inode": source_view_info.st_ino,
                "source_view_mode": stat.S_IMODE(source_view_info.st_mode),
                "recordings": planned,
            }
            write_manifest(manifest_path, manifest)

        _validate_materialization_backing(manifest, backing_source)
        staging = Path(manifest["staging"])
        rollback_view = Path(manifest["rollback_view"])
        return _roll_forward_materialization(
            source_task=source_task,
            staging=staging,
            rollback_view=rollback_view,
            manifest_path=manifest_path,
            manifest=manifest,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_task", type=Path)
    parser.add_argument("--keep-signature")
    parser.add_argument("--manifest", type=Path, required=True)
    apply_mode = parser.add_mutually_exclusive_group()
    apply_mode.add_argument("--apply", action="store_true")
    apply_mode.add_argument("--link-view", action="store_true")
    apply_mode.add_argument("--materialize-link-view", action="store_true")
    parser.add_argument("--backing-source", type=Path)
    args = parser.parse_args()

    if args.materialize_link_view:
        if args.backing_source is None:
            parser.error("--materialize-link-view requires --backing-source")
        manifest = materialize_symlink_view_as_hardlinks(
            args.source_task,
            backing_source=args.backing_source,
            manifest_path=args.manifest,
        )
        print(
            f"materialized recordings: {len(manifest['recordings'])}; "
            f"rollback view: {manifest['rollback_view']}"
        )
        return 0

    plan = build_split_plan(args.source_task, args.keep_signature)
    write_manifest(args.manifest, plan)
    for group in plan["groups"]:
        marker = "keep" if group["keep_in_source"] else "move"
        print(
            f"{marker:4} {group['count']:5d} {group['signature_id']} "
            f"{group['signature']['robot_type']} -> {group['destination']}"
        )
    if plan["invalid"]:
        print(f"invalid metacards: {len(plan['invalid'])}")
    if args.apply:
        moved = apply_split(plan)
        print(f"moved recordings: {moved}")
    elif args.link_view:
        linked = apply_split_as_symlink_view(plan, args.backing_source)
        print(f"linked recordings: {linked}")
    else:
        print("dry-run only; pass --apply or --link-view to split recordings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
