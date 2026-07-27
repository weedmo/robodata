#!/usr/bin/env python3
"""Split one raw task directory into conversion-compatible metadata groups."""

from __future__ import annotations

import argparse
import ctypes
import errno
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
_RENAME_NOREPLACE = 1
_NFS_SUPER_MAGIC = 0x6969


class _LinuxStatFs(ctypes.Structure):
    _fields_ = [
        ("f_type", ctypes.c_long),
        ("f_bsize", ctypes.c_long),
        ("f_blocks", ctypes.c_ulong),
        ("f_bfree", ctypes.c_ulong),
        ("f_bavail", ctypes.c_ulong),
        ("f_files", ctypes.c_ulong),
        ("f_ffree", ctypes.c_ulong),
        ("f_fsid", ctypes.c_int * 2),
        ("f_namelen", ctypes.c_long),
        ("f_frsize", ctypes.c_long),
        ("f_flags", ctypes.c_long),
        ("f_spare", ctypes.c_long * 4),
    ]


def _filesystem_type(descriptor: int) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    fstatfs = getattr(libc, "fstatfs", None)
    if fstatfs is None:
        raise RuntimeError("fstatfs is unavailable")
    fstatfs.argtypes = [ctypes.c_int, ctypes.POINTER(_LinuxStatFs)]
    fstatfs.restype = ctypes.c_int
    result = _LinuxStatFs()
    if fstatfs(descriptor, ctypes.byref(result)) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return int(result.f_type)


def _stat_at(parent_descriptor: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None


def _rename_noreplace(
    source_parent_descriptor: int,
    source_name: str,
    destination_parent_descriptor: int,
    destination_name: str,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise RuntimeError("renameat2(RENAME_NOREPLACE) is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_parent_descriptor,
        os.fsencode(source_name),
        destination_parent_descriptor,
        os.fsencode(destination_name),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error in {errno.EINVAL, errno.EOPNOTSUPP}:
        raise RuntimeError(
            "filesystem does not support atomic RENAME_NOREPLACE; "
            "plain rename fallback is forbidden"
        )
    raise OSError(
        error,
        os.strerror(error),
        f"{source_name} -> {destination_name}",
    )


def _require_rename_noreplace_support(parent: Path) -> None:
    descriptor = _open_directory_chain_nofollow(parent)
    probe = f".robodata-rename-probe-{secrets.token_hex(16)}"
    try:
        try:
            _rename_noreplace(descriptor, probe, descriptor, f"{probe}-dst")
        except FileNotFoundError:
            return
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return
            raise RuntimeError(
                "cannot prove atomic RENAME_NOREPLACE support"
            ) from exc
        raise RuntimeError("rename support probe unexpectedly mutated the namespace")
    finally:
        os.close(descriptor)


def _rename_materialization_noreplace(source: Path, destination: Path) -> None:
    source_parent_descriptor = _open_directory_chain_nofollow(source.parent)
    destination_parent_descriptor = _open_directory_chain_nofollow(
        destination.parent
    )
    try:
        source_info = _stat_at(source_parent_descriptor, source.name)
        if source_info is None:
            raise FileNotFoundError(source)
        if _stat_at(destination_parent_descriptor, destination.name) is not None:
            raise FileExistsError(destination)
        _rename_noreplace(
            source_parent_descriptor,
            source.name,
            destination_parent_descriptor,
            destination.name,
        )
        installed_info = _stat_at(
            destination_parent_descriptor,
            destination.name,
        )
        if (
            installed_info is None
            or (installed_info.st_dev, installed_info.st_ino)
            != (source_info.st_dev, source_info.st_ino)
            or _stat_at(source_parent_descriptor, source.name) is not None
        ):
            raise RuntimeError(
                "no-replace rename identity verification failed: "
                f"{source} -> {destination}"
            )
        os.fsync(source_parent_descriptor)
        if destination_parent_descriptor != source_parent_descriptor:
            os.fsync(destination_parent_descriptor)
    finally:
        os.close(destination_parent_descriptor)
        os.close(source_parent_descriptor)


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
        _rename_materialization_noreplace(construction, staging)
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
            _rename_materialization_noreplace(staging, quarantine_path)
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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        view_entries = sorted(source_task.iterdir())
    except OSError as exc:
        raise ValueError(f"cannot list symlink view: {source_task}") from exc
    if not view_entries:
        raise ValueError("symlink view has no recordings")

    planned: list[dict[str, Any]] = []
    preserved_entries: list[dict[str, Any]] = []
    seen_serials: set[str] = set()
    for view_entry in view_entries:
        if view_entry.name.startswith(".conversion-quarantine-"):
            preserved_entries.append(
                _inventory_preserved_materialization_directory(view_entry)
            )
            continue
        if not _SERIAL_RE.fullmatch(view_entry.name):
            raise ValueError(f"unexpected entry in symlink view: {view_entry.name}")
        if view_entry.name in seen_serials:
            raise RuntimeError(f"duplicate recording in symlink view: {view_entry.name}")
        seen_serials.add(view_entry.name)
        view_info = view_entry.lstat()
        if stat.S_ISLNK(view_info.st_mode):
            source_kind = "directory_symlink"
            raw_target = os.readlink(view_entry)
            target = view_entry.resolve(strict=True)
            if target != backing_source / view_entry.name:
                raise ValueError(
                    f"recording symlink resolves outside backing source: {view_entry}"
                )
            if not _plain_directory(target):
                raise ValueError(
                    f"recording target is not a matching plain directory: {target}"
                )
        elif stat.S_ISDIR(view_info.st_mode):
            source_kind = "plain_directory"
            raw_target = None
            target = view_entry
        else:
            raise ValueError(
                f"recording view entry is not a directory or symlink: {view_entry}"
            )

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
            visible_info = source_file.lstat()
            if stat.S_ISREG(visible_info.st_mode):
                actual_source = source_file
                file_source_kind = "regular_file"
                view_target = None
            elif (
                source_kind == "plain_directory"
                and stat.S_ISLNK(visible_info.st_mode)
            ):
                actual_source = source_file.resolve(strict=True)
                expected_source = backing_source / view_entry.name / source_file.name
                if actual_source != expected_source:
                    raise ValueError(
                        "recording file symlink resolves outside its matching "
                        f"backing recording: {source_file}"
                    )
                file_source_kind = "file_symlink"
                view_target = os.readlink(source_file)
            else:
                raise ValueError(
                    f"recording contains a non-regular entry: {source_file}"
                )
            if not _plain_regular_file(actual_source):
                raise ValueError(
                    f"recording source is not a regular file: {actual_source}"
                )
            info = actual_source.stat(follow_symlinks=False)
            parent_info = actual_source.parent.stat(follow_symlinks=False)
            files.append(
                {
                    "name": source_file.name,
                    "source_kind": file_source_kind,
                    "source": str(actual_source),
                    "device": info.st_dev,
                    "inode": info.st_ino,
                    "size": info.st_size,
                    "mode": stat.S_IMODE(info.st_mode),
                    "source_parent_device": parent_info.st_dev,
                    "source_parent_inode": parent_info.st_ino,
                    "view_device": visible_info.st_dev,
                    "view_inode": visible_info.st_ino,
                    "view_mode": stat.S_IMODE(visible_info.st_mode),
                    **(
                        {"view_target": view_target}
                        if view_target is not None
                        else {}
                    ),
                }
            )
        planned.append(
            {
                "serial": view_entry.name,
                "source_kind": source_kind,
                "view_device": view_info.st_dev,
                "view_inode": view_info.st_ino,
                "view_mode": stat.S_IMODE(view_info.st_mode),
                **({"view_target": raw_target} if raw_target is not None else {}),
                "source": str(target),
                "source_device": target.stat(follow_symlinks=False).st_dev,
                "source_inode": target.stat(follow_symlinks=False).st_ino,
                "source_mode": stat.S_IMODE(target.stat().st_mode),
                "files": files,
            }
        )
    if not planned:
        raise ValueError("symlink view has no recordings")
    return planned, preserved_entries


def _inventory_preserved_materialization_directory(path: Path) -> dict[str, Any]:
    root_info = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(root_info.st_mode):
        raise ValueError(f"preserved materialization entry is not a directory: {path}")
    tree: list[dict[str, Any]] = []

    def visit(descriptor: int, relative: Path) -> None:
        info = os.fstat(descriptor)
        tree.append(
            {
                "relative_path": relative.as_posix(),
                "kind": "directory",
                "device": info.st_dev,
                "inode": info.st_ino,
                "mode": stat.S_IMODE(info.st_mode),
            }
        )
        for name in sorted(os.listdir(descriptor)):
            entry_info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            child_relative = Path(name) if relative == Path(".") else relative / name
            if stat.S_ISDIR(entry_info.st_mode):
                child_descriptor = os.open(
                    name,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
                try:
                    pinned_info = os.fstat(child_descriptor)
                    if (pinned_info.st_dev, pinned_info.st_ino) != (
                        entry_info.st_dev,
                        entry_info.st_ino,
                    ):
                        raise RuntimeError(
                            "preserved directory identity changed during inventory"
                        )
                    visit(child_descriptor, child_relative)
                finally:
                    os.close(child_descriptor)
            elif stat.S_ISREG(entry_info.st_mode):
                tree.append(
                    {
                        "relative_path": child_relative.as_posix(),
                        "kind": "regular_file",
                        "device": entry_info.st_dev,
                        "inode": entry_info.st_ino,
                        "mode": stat.S_IMODE(entry_info.st_mode),
                        "size": entry_info.st_size,
                    }
                )
            else:
                raise ValueError(
                    f"preserved directory contains a symlink or special file: "
                    f"{path / child_relative}"
                )

    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        pinned_root = os.fstat(descriptor)
        if (pinned_root.st_dev, pinned_root.st_ino) != (
            root_info.st_dev,
            root_info.st_ino,
        ):
            raise RuntimeError(
                "preserved directory identity changed during inventory"
            )
        visit(descriptor, Path("."))
    finally:
        os.close(descriptor)
    return {
        "name": path.name,
        "kind": "directory",
        "device": root_info.st_dev,
        "inode": root_info.st_ino,
        "mode": stat.S_IMODE(root_info.st_mode),
        "tree": tree,
    }


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
            or manifest["version"] not in {1, 2}
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
            version = manifest["version"]
            source_kind = (
                recording["source_kind"]
                if version == 2
                else "directory_symlink"
            )
            expected_recording_source = (
                source_task / serial
                if source_kind == "plain_directory"
                else backing_source / serial
            )
            if (
                not isinstance(serial, str)
                or not _SERIAL_RE.fullmatch(serial)
                or serial in seen_serials
                or source_kind not in {"plain_directory", "directory_symlink"}
                or Path(recording["source"]) != expected_recording_source
                or not isinstance(recording["view_device"], int)
                or not isinstance(recording["view_inode"], int)
                or (
                    version == 2
                    and not isinstance(recording["view_mode"], int)
                )
                or (
                    source_kind == "directory_symlink"
                    and not isinstance(recording["view_target"], str)
                )
                or (
                    source_kind == "plain_directory"
                    and (
                        "view_target" in recording
                        or not isinstance(recording["view_mode"], int)
                    )
                )
            ):
                raise ValueError("manifest recording identity is invalid")
            seen_serials.add(serial)
            if not isinstance(recording["source_mode"], int):
                raise ValueError("manifest recording mode is invalid")
            if version == 2 and not all(
                isinstance(recording[field], int)
                for field in ("source_device", "source_inode")
            ):
                raise ValueError("manifest recording source identity is invalid")
            files = recording["files"]
            if not isinstance(files, list) or not files:
                raise ValueError("manifest recording has no files")
            seen_files: set[str] = set()
            for source_file in files:
                name = source_file["name"]
                file_source_kind = (
                    source_file["source_kind"]
                    if version == 2
                    else "regular_file"
                )
                expected_file_source = (
                    source_task / serial / name
                    if (
                        source_kind == "plain_directory"
                        and file_source_kind == "regular_file"
                    )
                    else backing_source / serial / name
                )
                if (
                    not isinstance(name, str)
                    or Path(name).name != name
                    or name in {"", ".", ".."}
                    or name in seen_files
                    or file_source_kind not in {"regular_file", "file_symlink"}
                    or (
                        source_kind == "directory_symlink"
                        and file_source_kind != "regular_file"
                    )
                    or Path(source_file["source"]) != expected_file_source
                    or not all(
                        isinstance(source_file[field], int)
                        for field in ("device", "inode", "size", "mode")
                    )
                    or (
                        version == 2
                        and not all(
                            isinstance(source_file[field], int)
                            for field in (
                                "view_device",
                                "view_inode",
                                "view_mode",
                                "source_parent_device",
                                "source_parent_inode",
                            )
                        )
                    )
                    or (
                        file_source_kind == "file_symlink"
                        and not isinstance(source_file["view_target"], str)
                    )
                    or (
                        file_source_kind == "regular_file"
                        and "view_target" in source_file
                    )
                ):
                    raise ValueError("manifest source file identity is invalid")
                seen_files.add(name)
            if not {"metacard.json", f"{serial}_0.mcap"}.issubset(seen_files):
                raise ValueError("manifest recording is missing required files")

        preserved_entries = (
            manifest["preserved_entries"]
            if manifest["version"] == 2
            else manifest.get("preserved_entries", [])
        )
        if manifest["version"] == 1:
            if preserved_entries:
                raise ValueError("version 1 manifest cannot preserve view entries")
        elif not isinstance(preserved_entries, list):
            raise ValueError("manifest preserved entries are invalid")
        seen_preserved: set[str] = set()
        for preserved in preserved_entries:
            name = preserved["name"]
            if (
                not isinstance(name, str)
                or not name.startswith(".conversion-quarantine-")
                or Path(name).name != name
                or name in seen_preserved
                or preserved["kind"] != "directory"
                or not all(
                    isinstance(preserved[field], int)
                    for field in ("device", "inode", "mode")
                )
            ):
                raise ValueError("manifest preserved entry identity is invalid")
            seen_preserved.add(name)
            tree = preserved["tree"]
            if not isinstance(tree, list) or not tree:
                raise ValueError("manifest preserved tree is invalid")
            seen_paths: set[str] = set()
            directory_paths: set[str] = set()
            for item in tree:
                relative_path = item["relative_path"]
                relative = Path(relative_path)
                kind = item["kind"]
                if (
                    not isinstance(relative_path, str)
                    or "\\" in relative_path
                    or relative.is_absolute()
                    or relative_path in seen_paths
                    or any(part in {"", ".."} for part in relative.parts)
                    or kind not in {"directory", "regular_file"}
                    or not all(
                        isinstance(item[field], int)
                        for field in ("device", "inode", "mode")
                    )
                    or (
                        kind == "regular_file"
                        and not isinstance(item["size"], int)
                    )
                    or (kind == "directory" and "size" in item)
                ):
                    raise ValueError("manifest preserved tree entry is invalid")
                normalized = relative.as_posix()
                if normalized != relative_path:
                    raise ValueError("manifest preserved path is not canonical")
                parent = relative.parent.as_posix()
                if relative_path != "." and parent not in directory_paths:
                    raise ValueError("manifest preserved tree parent is missing")
                seen_paths.add(relative_path)
                if kind == "directory":
                    directory_paths.add(relative_path)
            if "." not in directory_paths:
                raise ValueError("manifest preserved tree root is missing")
            tree_root = next(
                item for item in tree if item["relative_path"] == "."
            )
            if (
                tree_root["kind"] != "directory"
                or (
                    tree_root["device"],
                    tree_root["inode"],
                    tree_root["mode"],
                )
                != (
                    preserved["device"],
                    preserved["inode"],
                    preserved["mode"],
                )
            ):
                raise ValueError("manifest preserved root identity is inconsistent")
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
        source_kind = recording.get("source_kind", "directory_symlink")
        backing_recording = backing_source / recording["serial"]
        backing_files = {
            source_file["name"]
            for source_file in recording["files"]
            if source_file.get("source_kind", "regular_file") == "file_symlink"
            or source_kind == "directory_symlink"
        }
        if source_kind == "directory_symlink" or backing_files:
            if not _plain_directory(backing_recording):
                raise RuntimeError(
                    f"materialization backing changed: {backing_recording}"
                )
            backing_info = backing_recording.stat(follow_symlinks=False)
            backing_identity = (
                (
                    backing_info.st_dev,
                    backing_info.st_ino,
                    stat.S_IMODE(backing_info.st_mode),
                )
                if manifest["version"] == 2
                else (stat.S_IMODE(backing_info.st_mode),)
            )
            expected_backing_identity = (
                (
                    recording["source_device"],
                    recording["source_inode"],
                    recording["source_mode"],
                )
                if manifest["version"] == 2
                else (recording["source_mode"],)
            )
            if (
                source_kind == "directory_symlink"
                and backing_identity != expected_backing_identity
            ):
                raise RuntimeError(
                    f"materialization backing changed: {backing_recording}"
                )
            try:
                actual_files = {path.name for path in backing_recording.iterdir()}
            except OSError as exc:
                raise RuntimeError(
                    f"cannot inspect materialization backing: {backing_recording}"
                ) from exc
            if (
                source_kind == "directory_symlink"
                and actual_files != backing_files
            ) or not backing_files.issubset(actual_files):
                raise RuntimeError(
                    f"materialization backing changed: {backing_recording}"
                )
        for source_file in recording["files"]:
            path = _materialization_file_source_path(
                manifest,
                recording,
                source_file,
            )
            is_backing_source = (
                source_kind == "directory_symlink"
                or source_file.get("source_kind") == "file_symlink"
            )
            if manifest["version"] == 2 and is_backing_source:
                parent_info = path.parent.stat(follow_symlinks=False)
                if (parent_info.st_dev, parent_info.st_ino) != (
                    source_file["source_parent_device"],
                    source_file["source_parent_inode"],
                ):
                    raise RuntimeError(
                        f"materialization backing changed: {path.parent}"
                    )
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


def _materialization_file_source_path(
    manifest: dict[str, Any],
    recording: dict[str, Any],
    source_file: dict[str, Any],
) -> Path:
    path = Path(source_file["source"])
    if (
        recording.get("source_kind") == "plain_directory"
        and source_file.get("source_kind") == "regular_file"
        and not _plain_regular_file(path)
    ):
        rollback_path = (
            Path(manifest["rollback_view"])
            / recording["serial"]
            / source_file["name"]
        )
        if _plain_regular_file(rollback_path):
            return rollback_path
    return path


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
    recordings = {
        recording["serial"]: recording for recording in manifest["recordings"]
    }
    preserved_entries = {
        entry["name"]: entry for entry in manifest.get("preserved_entries", [])
    }
    try:
        entries = {entry.name: entry for entry in path.iterdir()}
        if set(entries) != set(recordings) | set(preserved_entries):
            return "foreign"
        for serial, recording in recordings.items():
            entry = entries[serial]
            info = entry.lstat()
            source_kind = recording.get("source_kind", "directory_symlink")
            if source_kind == "directory_symlink":
                if (
                    not stat.S_ISLNK(info.st_mode)
                    or (
                        info.st_dev,
                        info.st_ino,
                        stat.S_IMODE(info.st_mode),
                    )
                    != (
                        recording["view_device"],
                        recording["view_inode"],
                        recording.get(
                            "view_mode",
                            stat.S_IMODE(info.st_mode),
                        ),
                    )
                    or os.readlink(entry) != recording["view_target"]
                    or entry.resolve(strict=True) != Path(recording["source"])
                ):
                    return "foreign"
                continue
            if (
                not stat.S_ISDIR(info.st_mode)
                or (
                    info.st_dev,
                    info.st_ino,
                    stat.S_IMODE(info.st_mode),
                )
                != (
                    recording["view_device"],
                    recording["view_inode"],
                    recording["view_mode"],
                )
            ):
                return "foreign"
            descriptor = os.open(
                entry,
                os.O_RDONLY
                | os.O_DIRECTORY
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                pinned_info = os.fstat(descriptor)
                if (pinned_info.st_dev, pinned_info.st_ino) != (
                    info.st_dev,
                    info.st_ino,
                ):
                    return "foreign"
                expected_files = {
                    source_file["name"]: source_file
                    for source_file in recording["files"]
                }
                if set(os.listdir(descriptor)) != set(expected_files):
                    return "foreign"
                for name, source_file in expected_files.items():
                    visible_info = os.stat(
                        name,
                        dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                    if (
                        visible_info.st_dev,
                        visible_info.st_ino,
                        stat.S_IMODE(visible_info.st_mode),
                    ) != (
                        source_file["view_device"],
                        source_file["view_inode"],
                        source_file["view_mode"],
                    ):
                        return "foreign"
                    file_kind = source_file["source_kind"]
                    visible_path = entry / name
                    if file_kind == "file_symlink":
                        if (
                            not stat.S_ISLNK(visible_info.st_mode)
                            or os.readlink(visible_path)
                            != source_file["view_target"]
                            or visible_path.resolve(strict=True)
                            != Path(source_file["source"])
                        ):
                            return "foreign"
                        actual_info = Path(source_file["source"]).stat(
                            follow_symlinks=False
                        )
                    else:
                        if not stat.S_ISREG(visible_info.st_mode):
                            return "foreign"
                        actual_info = visible_info
                    if (
                        actual_info.st_dev,
                        actual_info.st_ino,
                        actual_info.st_size,
                        stat.S_IMODE(actual_info.st_mode),
                    ) != (
                        source_file["device"],
                        source_file["inode"],
                        source_file["size"],
                        source_file["mode"],
                    ):
                        return "foreign"
            finally:
                os.close(descriptor)
        for name, preserved in preserved_entries.items():
            if not _matches_preserved_materialization_directory(
                entries[name],
                preserved,
            ):
                return "foreign"
    except (OSError, RuntimeError, ValueError):
        return "foreign"
    return "view"


def _matches_preserved_materialization_directory(
    path: Path,
    preserved: dict[str, Any],
) -> bool:
    expected = {item["relative_path"]: item for item in preserved["tree"]}
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    actual_paths: set[str] = set()

    def visit(current_descriptor: int, relative: Path) -> bool:
        relative_path = relative.as_posix()
        item = expected.get(relative_path)
        info = os.fstat(current_descriptor)
        if (
            item is None
            or item["kind"] != "directory"
            or (
                info.st_dev,
                info.st_ino,
                stat.S_IMODE(info.st_mode),
            )
            != (item["device"], item["inode"], item["mode"])
        ):
            return False
        actual_paths.add(relative_path)
        for name in os.listdir(current_descriptor):
            entry_info = os.stat(
                name,
                dir_fd=current_descriptor,
                follow_symlinks=False,
            )
            child_relative = (
                Path(name) if relative == Path(".") else relative / name
            )
            child_path = child_relative.as_posix()
            child = expected.get(child_path)
            if child is None:
                return False
            if stat.S_ISDIR(entry_info.st_mode):
                child_descriptor = os.open(
                    name,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=current_descriptor,
                )
                try:
                    if not visit(child_descriptor, child_relative):
                        return False
                finally:
                    os.close(child_descriptor)
            elif stat.S_ISREG(entry_info.st_mode):
                if (
                    child["kind"] != "regular_file"
                    or (
                        entry_info.st_dev,
                        entry_info.st_ino,
                        stat.S_IMODE(entry_info.st_mode),
                        entry_info.st_size,
                    )
                    != (
                        child["device"],
                        child["inode"],
                        child["mode"],
                        child["size"],
                    )
                ):
                    return False
                actual_paths.add(child_path)
            else:
                return False
        return True

    try:
        root_info = os.fstat(descriptor)
        path_info = path.stat(follow_symlinks=False)
        if (
            root_info.st_dev,
            root_info.st_ino,
            stat.S_IMODE(root_info.st_mode),
        ) != (
            preserved["device"],
            preserved["inode"],
            preserved["mode"],
        ) or (path_info.st_dev, path_info.st_ino) != (
            root_info.st_dev,
            root_info.st_ino,
        ):
            return False
        return visit(descriptor, Path(".")) and actual_paths == set(expected)
    finally:
        os.close(descriptor)


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
        _rename_materialization_noreplace(source, destination)
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
                for source_file in recording["files"]:
                    source_path = _materialization_file_source_path(
                        manifest,
                        recording,
                        source_file,
                    )
                    source_descriptor = os.open(
                        source_path.parent,
                        os.O_RDONLY
                        | os.O_DIRECTORY
                        | getattr(os, "O_NOFOLLOW", 0),
                    )
                    try:
                        source_parent_info = os.fstat(source_descriptor)
                        source_parent_path_info = source_path.parent.stat(
                            follow_symlinks=False
                        )
                        if manifest["version"] == 2 and (
                            source_parent_info.st_dev,
                            source_parent_info.st_ino,
                        ) != (
                            source_file["source_parent_device"],
                            source_file["source_parent_inode"],
                        ):
                            raise RuntimeError(
                                "materialization source parent changed during staging"
                            )
                        if (
                            source_parent_path_info.st_dev,
                            source_parent_path_info.st_ino,
                        ) != (
                            source_parent_info.st_dev,
                            source_parent_info.st_ino,
                        ):
                            raise RuntimeError(
                                "materialization source parent path changed "
                                "during staging"
                            )
                        source_info = os.stat(
                            source_path.name,
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
                            source_path.name,
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
        try:
            _validate_materialization_commit_guards(
                source_task,
                manifest,
                pinned_info,
                identity_context="before committed manifest",
            )
        except BaseException as exc:
            _fail_materialization_commit(
                manifest_path,
                manifest,
                f"materialization pre-commit validation failed: {exc}",
                cause=exc,
            )
        _write_materialization_phase(
            manifest_path,
            manifest,
            "committed",
        )
        try:
            _validate_materialization_commit_guards(
                source_task,
                manifest,
                pinned_info,
                identity_context="during committed manifest write",
            )
        except BaseException as exc:
            _fail_materialization_commit(
                manifest_path,
                manifest,
                f"materialization post-commit validation failed: {exc}",
                cause=exc,
            )
    finally:
        if owned_descriptor:
            os.close(descriptor)


def _validate_materialization_commit_guards(
    source_task: Path,
    manifest: dict[str, Any],
    pinned_info: os.stat_result,
    *,
    identity_context: str,
) -> None:
    path_info = source_task.stat(follow_symlinks=False)
    if (path_info.st_dev, path_info.st_ino) != (
        pinned_info.st_dev,
        pinned_info.st_ino,
    ):
        raise RuntimeError(
            f"installed source identity changed {identity_context}"
        )
    if (
        _classify_materialization_tree(
            source_task,
            manifest,
            require_staging_identity=True,
        )
        != "hardlink"
    ):
        raise RuntimeError("installed materialization does not match the manifest")
    _validate_materialization_backing(
        manifest,
        Path(manifest["backing_source"]),
    )
    rollback_view = Path(manifest["rollback_view"])
    if _classify_materialization_view(rollback_view, manifest) != "view":
        raise RuntimeError(
            "rollback view does not exactly match the materialization manifest"
        )


def _fail_materialization_commit(
    manifest_path: Path,
    manifest: dict[str, Any],
    message: str,
    *,
    cause: BaseException,
) -> None:
    manifest["error"] = message
    _write_materialization_phase(
        manifest_path,
        manifest,
        "recovery_failed",
    )
    raise RuntimeError(message) from cause


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
        _commit_materialization_with_pinned_source(
            source_task,
            manifest_path,
            manifest,
        )
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
                _rename_materialization_noreplace(staging, source_task)
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

    _require_rename_noreplace_support(source_task.parent)
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
            planned, preserved_entries = _plan_materialization_view(
                source_task,
                backing_source,
            )
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
                "version": 2,
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
                "preserved_entries": preserved_entries,
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


_DETACHED_MATERIALIZATION_PHASES = frozenset(
    {"reserved", "preparing", "finalizing", "committed", "recovery_failed"}
)
_DETACHED_MATERIALIZATION_OPERATION = (
    "materialize_link_view_detached_as_hardlinks"
)


def _validate_detached_private_parent(
    parent: Path,
    *,
    destination_name: str,
    manifest: dict[str, Any] | None = None,
    require_empty: bool = False,
) -> os.stat_result:
    descriptor = _open_directory_chain_nofollow(parent)
    try:
        info = os.fstat(descriptor)
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError(
                "detached destination parent must have mode 0700"
            )
        if manifest is not None and (
            manifest["destination_parent"] != str(parent)
            or (
                info.st_dev,
                info.st_ino,
                stat.S_IMODE(info.st_mode),
                info.st_uid,
                info.st_gid,
            )
            != (
                manifest["destination_parent_device"],
                manifest["destination_parent_inode"],
                manifest["destination_parent_mode"],
                manifest["destination_parent_uid"],
                manifest["destination_parent_gid"],
            )
        ):
            raise RuntimeError(
                "detached destination parent identity changed"
            )
        entries = set(os.listdir(descriptor))
        if require_empty and entries:
            raise ValueError(
                "detached destination parent must initially be empty"
            )
        if not require_empty and not entries.issubset({destination_name}):
            raise RuntimeError(
                "detached destination parent contains foreign entries"
            )
        return info
    finally:
        os.close(descriptor)


def _load_detached_materialization_manifest(
    manifest_path: Path,
    *,
    source_task: Path,
    backing_source: Path,
    destination_task: Path,
) -> dict[str, Any]:
    try:
        manifest = _read_plain_json(manifest_path)
        if (
            manifest["version"] != 3
            or manifest["operation"] != _DETACHED_MATERIALIZATION_OPERATION
            or manifest["phase"] not in _DETACHED_MATERIALIZATION_PHASES
            or manifest["source_task"] != str(source_task)
            or manifest["backing_source"] != str(backing_source)
            or manifest["destination_task"] != str(destination_task)
            or manifest["destination_parent"] != str(destination_task.parent)
            or not all(
                isinstance(manifest[field], int)
                for field in (
                    "source_view_device",
                    "source_view_inode",
                    "source_view_mode",
                    "destination_parent_device",
                    "destination_parent_inode",
                    "destination_parent_mode",
                    "destination_parent_uid",
                    "destination_parent_gid",
                )
            )
            or manifest["destination_parent_mode"] != 0o700
            or not isinstance(manifest["recordings"], list)
            or not manifest["recordings"]
            or not isinstance(manifest["preserved_entries"], list)
        ):
            raise ValueError("detached manifest identity is invalid")
        destination_identity = (
            manifest.get("destination_device"),
            manifest.get("destination_inode"),
        )
        if manifest["phase"] == "reserved":
            if destination_identity != (None, None):
                raise ValueError("reserved destination is already bound")
        elif manifest["phase"] == "recovery_failed":
            if not (
                destination_identity == (None, None)
                or all(isinstance(value, int) for value in destination_identity)
            ):
                raise ValueError("detached destination identity is invalid")
        elif not all(isinstance(value, int) for value in destination_identity):
            raise ValueError("detached destination identity is invalid")
        if not isinstance(manifest["destination_mode"], int):
            raise ValueError("detached destination mode is invalid")
        marker_identity = (
            manifest.get("reservation_marker_device"),
            manifest.get("reservation_marker_inode"),
        )
        if manifest["phase"] == "reserved":
            if marker_identity != (None, None):
                raise ValueError("reserved marker is already bound")
        elif manifest["phase"] == "recovery_failed":
            if not (
                marker_identity == (None, None)
                or all(isinstance(value, int) for value in marker_identity)
            ):
                raise ValueError("detached marker identity is invalid")
        elif not all(isinstance(value, int) for value in marker_identity):
            raise ValueError("detached marker identity is invalid")
        if (
            not isinstance(manifest["reservation_marker"], str)
            or not manifest["reservation_marker"].startswith(
                ".robodata-detached-reservation-"
            )
        ):
            raise ValueError("detached reservation marker is invalid")
        seen_serials: set[str] = set()
        for recording in manifest["recordings"]:
            serial = recording["serial"]
            source_kind = recording["source_kind"]
            expected_recording = (
                source_task / serial
                if source_kind == "plain_directory"
                else backing_source / serial
            )
            if (
                not isinstance(serial, str)
                or not _SERIAL_RE.fullmatch(serial)
                or serial in seen_serials
                or source_kind not in {"plain_directory", "directory_symlink"}
                or Path(recording["source"]) != expected_recording
                or not all(
                    isinstance(recording[field], int)
                    for field in (
                        "view_device",
                        "view_inode",
                        "view_mode",
                        "source_device",
                        "source_inode",
                        "source_mode",
                    )
                )
            ):
                raise ValueError("detached recording inventory is invalid")
            destination_recording_identity = (
                recording.get("destination_device"),
                recording.get("destination_inode"),
            )
            if not (
                "destination_device" in recording
                and "destination_inode" in recording
                and (
                    destination_recording_identity == (None, None)
                or all(
                    isinstance(value, int)
                    for value in destination_recording_identity
                )
                )
            ):
                raise ValueError(
                    "detached recording destination identity is invalid"
                )
            if manifest["phase"] in {"finalizing", "committed"} and not all(
                isinstance(value, int)
                for value in destination_recording_identity
            ):
                raise ValueError(
                    "final detached recording is not durably bound"
                )
            seen_serials.add(serial)
            seen_files: set[str] = set()
            for source_file in recording["files"]:
                name = source_file["name"]
                source_kind_file = source_file["source_kind"]
                expected_source = (
                    source_task / serial / name
                    if (
                        source_kind == "plain_directory"
                        and source_kind_file == "regular_file"
                    )
                    else backing_source / serial / name
                )
                if (
                    not isinstance(name, str)
                    or name in {"", ".", ".."}
                    or Path(name).name != name
                    or name in seen_files
                    or source_kind_file not in {"regular_file", "file_symlink"}
                    or Path(source_file["source"]) != expected_source
                    or not all(
                        isinstance(source_file[field], int)
                        for field in (
                            "device",
                            "inode",
                            "size",
                            "mode",
                            "source_parent_device",
                            "source_parent_inode",
                            "view_device",
                            "view_inode",
                            "view_mode",
                        )
                    )
                ):
                    raise ValueError("detached file inventory is invalid")
                seen_files.add(name)
            if not {"metacard.json", f"{serial}_0.mcap"}.issubset(seen_files):
                raise ValueError("detached recording is incomplete")
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
            f"invalid detached materialization manifest: {manifest_path}"
        ) from exc
    return manifest


def _detached_destination_state(
    destination_task: Path,
    manifest: dict[str, Any],
    *,
    require_marker: bool,
) -> str:
    if not destination_task.exists() and not destination_task.is_symlink():
        return "missing"
    if not _plain_directory(destination_task) or destination_task.is_symlink():
        return "foreign"
    root_info = destination_task.stat(follow_symlinks=False)
    expected_identity = (
        manifest.get("destination_device"),
        manifest.get("destination_inode"),
    )
    if expected_identity == (None, None) or (
        root_info.st_dev,
        root_info.st_ino,
    ) != expected_identity or stat.S_IMODE(root_info.st_mode) != manifest[
        "destination_mode"
    ]:
        return "foreign"
    expected_recordings = {
        recording["serial"]: recording for recording in manifest["recordings"]
    }
    complete = True
    try:
        entries = {entry.name: entry for entry in destination_task.iterdir()}
        marker_name = manifest["reservation_marker"]
        marker = entries.pop(marker_name, None)
        if require_marker:
            if marker is None:
                return "foreign"
            marker_info = marker.stat(follow_symlinks=False)
            if (
                not stat.S_ISREG(marker_info.st_mode)
                or marker_info.st_size != 0
                or (marker_info.st_dev, marker_info.st_ino)
                != (
                    manifest["reservation_marker_device"],
                    manifest["reservation_marker_inode"],
                )
            ):
                return "foreign"
        elif marker is not None:
            return "foreign"
        if not set(entries).issubset(expected_recordings):
            return "foreign"
        if set(entries) != set(expected_recordings):
            complete = False
        for serial, entry in entries.items():
            recording = expected_recordings[serial]
            destination_identity = (
                recording.get("destination_device"),
                recording.get("destination_inode"),
            )
            entry_info = entry.stat(follow_symlinks=False)
            if (
                not _plain_directory(entry)
                or entry.is_symlink()
                or destination_identity == (None, None)
                or (entry_info.st_dev, entry_info.st_ino)
                != destination_identity
                or stat.S_IMODE(entry_info.st_mode)
                != recording["source_mode"]
            ):
                return "foreign"
            expected_files = {
                item["name"]: item for item in recording["files"]
            }
            actual_files = {item.name: item for item in entry.iterdir()}
            if not set(actual_files).issubset(expected_files):
                return "foreign"
            if set(actual_files) != set(expected_files):
                complete = False
            for name, path in actual_files.items():
                info = path.stat(follow_symlinks=False)
                expected = expected_files[name]
                if (
                    not stat.S_ISREG(info.st_mode)
                    or (
                        info.st_dev,
                        info.st_ino,
                        info.st_size,
                        stat.S_IMODE(info.st_mode),
                    )
                    != (
                        expected["device"],
                        expected["inode"],
                        expected["size"],
                        expected["mode"],
                    )
                ):
                    return "foreign"
    except OSError:
        return "foreign"
    return "complete" if complete else "partial"


def _validate_detached_sources(
    source_task: Path,
    backing_source: Path,
    manifest: dict[str, Any],
) -> None:
    if _classify_materialization_view(source_task, manifest) != "view":
        raise RuntimeError("source hybrid view changed")
    _validate_materialization_backing(manifest, backing_source)


def _mark_detached_recovery_failed(
    manifest_path: Path,
    manifest: dict[str, Any],
    error: BaseException,
) -> None:
    manifest["phase"] = "recovery_failed"
    manifest["error"] = str(error)
    write_manifest(manifest_path, manifest)


def _build_detached_destination(
    destination_task: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
) -> None:
    _validate_detached_private_parent(
        destination_task.parent,
        destination_name=destination_task.name,
        manifest=manifest,
    )
    root_descriptor = os.open(
        destination_task,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        root_info = os.fstat(root_descriptor)
        if (root_info.st_dev, root_info.st_ino) != (
            manifest["destination_device"],
            manifest["destination_inode"],
        ):
            raise RuntimeError("detached destination root identity changed")
        for recording in manifest["recordings"]:
            _validate_detached_private_parent(
                destination_task.parent,
                destination_name=destination_task.name,
                manifest=manifest,
            )
            serial = recording["serial"]
            expected_identity = (
                recording.get("destination_device"),
                recording.get("destination_inode"),
            )
            if expected_identity == (None, None):
                os.mkdir(
                    serial,
                    mode=recording["source_mode"],
                    dir_fd=root_descriptor,
                )
                created_info = os.stat(
                    serial,
                    dir_fd=root_descriptor,
                    follow_symlinks=False,
                )
            recording_descriptor = os.open(
                serial,
                os.O_RDONLY
                | os.O_DIRECTORY
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root_descriptor,
            )
            try:
                pinned_recording_info = os.fstat(recording_descriptor)
                path_recording_info = os.stat(
                    serial,
                    dir_fd=root_descriptor,
                    follow_symlinks=False,
                )
                if expected_identity == (None, None):
                    if (
                        pinned_recording_info.st_dev,
                        pinned_recording_info.st_ino,
                    ) != (
                        created_info.st_dev,
                        created_info.st_ino,
                    ):
                        raise RuntimeError(
                            "new detached recording identity changed before binding"
                        )
                    recording["destination_device"] = (
                        pinned_recording_info.st_dev
                    )
                    recording["destination_inode"] = (
                        pinned_recording_info.st_ino
                    )
                    write_manifest(manifest_path, manifest)
                    expected_identity = (
                        recording["destination_device"],
                        recording["destination_inode"],
                    )
                if (
                    pinned_recording_info.st_dev,
                    pinned_recording_info.st_ino,
                ) != expected_identity or (
                    path_recording_info.st_dev,
                    path_recording_info.st_ino,
                ) != expected_identity:
                    raise RuntimeError(
                        "detached recording identity changed"
                    )
                os.fchmod(recording_descriptor, recording["source_mode"])
                for source_file in recording["files"]:
                    source_path = Path(source_file["source"])
                    source_descriptor = os.open(
                        source_path.parent,
                        os.O_RDONLY
                        | os.O_DIRECTORY
                        | getattr(os, "O_NOFOLLOW", 0),
                    )
                    try:
                        parent_info = os.fstat(source_descriptor)
                        if (parent_info.st_dev, parent_info.st_ino) != (
                            source_file["source_parent_device"],
                            source_file["source_parent_inode"],
                        ):
                            raise RuntimeError(
                                "detached source parent identity changed"
                            )
                        source_info = os.stat(
                            source_path.name,
                            dir_fd=source_descriptor,
                            follow_symlinks=False,
                        )
                        if (
                            not stat.S_ISREG(source_info.st_mode)
                            or (source_info.st_dev, source_info.st_ino)
                            != (
                                source_file["device"],
                                source_file["inode"],
                            )
                        ):
                            raise RuntimeError(
                                "detached source file identity changed"
                            )
                        existing = _stat_at(
                            recording_descriptor,
                            source_file["name"],
                        )
                        if existing is None:
                            os.link(
                                source_path.name,
                                source_file["name"],
                                src_dir_fd=source_descriptor,
                                dst_dir_fd=recording_descriptor,
                                follow_symlinks=False,
                            )
                        elif (
                            existing.st_dev,
                            existing.st_ino,
                            existing.st_size,
                            stat.S_IMODE(existing.st_mode),
                        ) != (
                            source_file["device"],
                            source_file["inode"],
                            source_file["size"],
                            source_file["mode"],
                        ):
                            raise RuntimeError(
                                "detached partial destination contains foreign data"
                            )
                    finally:
                        os.close(source_descriptor)
                os.fsync(recording_descriptor)
            finally:
                os.close(recording_descriptor)
        os.fsync(root_descriptor)
    finally:
        os.close(root_descriptor)
    _fsync_directory(destination_task.parent)
    _validate_detached_private_parent(
        destination_task.parent,
        destination_name=destination_task.name,
        manifest=manifest,
    )


def materialize_link_view_detached_as_hardlinks(
    source_task: Path,
    *,
    backing_source: Path,
    destination_task: Path,
    manifest_path: Path,
) -> dict[str, Any]:
    source_task = source_task.parent.resolve() / source_task.name
    backing_source = backing_source.parent.resolve() / backing_source.name
    destination_task = (
        destination_task.parent.resolve() / destination_task.name
    )
    manifest_path = Path(os.path.abspath(manifest_path))
    if not _plain_directory(source_task) or source_task.is_symlink():
        raise ValueError(f"source must be a plain task directory: {source_task}")
    if not _plain_directory(backing_source) or backing_source.is_symlink():
        raise ValueError(f"backing source must be a plain directory: {backing_source}")
    if not _plain_directory(destination_task.parent):
        raise ValueError("detached destination parent must already exist")
    if destination_task in {source_task, backing_source}:
        raise ValueError("detached destination must be distinct")
    for protected_root in (source_task, backing_source):
        for candidate, label in (
            (destination_task, "destination"),
            (manifest_path, "manifest"),
        ):
            try:
                candidate.relative_to(protected_root)
            except ValueError:
                continue
            raise ValueError(
                f"detached {label} must be outside source and backing trees"
            )
    try:
        manifest_path.relative_to(destination_task)
    except ValueError:
        pass
    else:
        raise ValueError("detached manifest must be outside destination")
    try:
        manifest_path.relative_to(destination_task.parent)
    except ValueError:
        pass
    else:
        raise ValueError(
            "detached manifest must be outside the private destination parent"
        )
    source_device = source_task.stat(follow_symlinks=False).st_dev
    manifest_parent_descriptor = _open_directory_chain_nofollow(
        manifest_path.parent
    )
    try:
        manifest_parent_device = os.fstat(manifest_parent_descriptor).st_dev
    finally:
        os.close(manifest_parent_descriptor)
    if (
        backing_source.stat(follow_symlinks=False).st_dev != source_device
        or destination_task.parent.stat(follow_symlinks=False).st_dev
        != source_device
        or manifest_parent_device != source_device
    ):
        raise RuntimeError("detached materialization paths are on different filesystems")
    protected_identities = _directory_tree_identities(source_task)
    protected_identities.update(_directory_tree_identities(backing_source))
    destination_parent_identities = _directory_chain_identities(
        destination_task.parent
    )
    manifest_parent_identities = _directory_chain_identities(
        manifest_path.parent
    )
    if protected_identities & destination_parent_identities:
        raise ValueError(
            "detached destination parent resolves inside a protected tree"
        )
    if protected_identities & manifest_parent_identities:
        raise ValueError(
            "detached manifest parent resolves inside a protected tree"
        )
    if destination_task.exists() or destination_task.is_symlink():
        if not _plain_directory(destination_task):
            raise ValueError("existing detached destination is not plain")
        destination_identities = _directory_tree_identities(destination_task)
        if destination_identities & manifest_parent_identities:
            raise ValueError(
                "detached manifest parent resolves inside destination"
            )

    initial_manifest_exists = manifest_path.exists() or manifest_path.is_symlink()
    initial_parent_info = (
        None
        if initial_manifest_exists
        else _validate_detached_private_parent(
            destination_task.parent,
            destination_name=destination_task.name,
            require_empty=True,
        )
    )
    with _materialization_lock(source_task):
        if initial_manifest_exists:
            manifest = _load_detached_materialization_manifest(
                manifest_path,
                source_task=source_task,
                backing_source=backing_source,
                destination_task=destination_task,
            )
            try:
                _validate_detached_private_parent(
                    destination_task.parent,
                    destination_name=destination_task.name,
                    manifest=manifest,
                )
            except BaseException as exc:
                _mark_detached_recovery_failed(
                    manifest_path,
                    manifest,
                    exc,
                )
                raise
        else:
            assert initial_parent_info is not None
            if destination_task.exists() or destination_task.is_symlink():
                raise FileExistsError(
                    "detached destination exists without a durable manifest"
                )
            recordings, preserved_entries = _plan_materialization_view(
                source_task,
                backing_source,
            )
            for recording in recordings:
                recording["destination_device"] = None
                recording["destination_inode"] = None
            source_info = source_task.stat(follow_symlinks=False)
            reservation_marker = (
                f".robodata-detached-reservation-{secrets.token_hex(32)}"
            )
            manifest = {
                "version": 3,
                "operation": _DETACHED_MATERIALIZATION_OPERATION,
                "phase": "reserved",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source_task": str(source_task),
                "backing_source": str(backing_source),
                "destination_task": str(destination_task),
                "destination_parent": str(destination_task.parent),
                "destination_parent_device": initial_parent_info.st_dev,
                "destination_parent_inode": initial_parent_info.st_ino,
                "destination_parent_mode": stat.S_IMODE(
                    initial_parent_info.st_mode
                ),
                "destination_parent_uid": initial_parent_info.st_uid,
                "destination_parent_gid": initial_parent_info.st_gid,
                "destination_device": None,
                "destination_inode": None,
                "destination_mode": stat.S_IMODE(source_info.st_mode),
                "reservation_marker": reservation_marker,
                "reservation_marker_device": None,
                "reservation_marker_inode": None,
                "source_view_device": source_info.st_dev,
                "source_view_inode": source_info.st_ino,
                "source_view_mode": stat.S_IMODE(source_info.st_mode),
                "recordings": recordings,
                "preserved_entries": preserved_entries,
            }
            _validate_detached_private_parent(
                destination_task.parent,
                destination_name=destination_task.name,
                require_empty=True,
            )
            write_manifest(manifest_path, manifest)
            _validate_detached_private_parent(
                destination_task.parent,
                destination_name=destination_task.name,
                manifest=manifest,
            )

        if manifest["phase"] == "recovery_failed":
            raise RuntimeError("detached materialization is recovery_failed")
        try:
            _validate_detached_private_parent(
                destination_task.parent,
                destination_name=destination_task.name,
                manifest=manifest,
            )
            _validate_detached_sources(source_task, backing_source, manifest)
            if manifest["phase"] == "committed":
                if (
                    _detached_destination_state(
                        destination_task,
                        manifest,
                        require_marker=False,
                    )
                    != "complete"
                ):
                    raise RuntimeError(
                        "committed detached destination changed"
                    )
                return manifest
            if manifest["phase"] == "reserved":
                _validate_detached_private_parent(
                    destination_task.parent,
                    destination_name=destination_task.name,
                    manifest=manifest,
                )
                if destination_task.exists() or destination_task.is_symlink():
                    raise RuntimeError(
                        "unbound detached destination exists; refusing adoption"
                    )
                parent_descriptor = _open_directory_chain_nofollow(
                    destination_task.parent
                )
                try:
                    os.mkdir(
                        destination_task.name,
                        mode=manifest["destination_mode"],
                        dir_fd=parent_descriptor,
                    )
                    created_destination_info = os.stat(
                        destination_task.name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                    destination_descriptor = os.open(
                        destination_task.name,
                        os.O_RDONLY
                        | os.O_DIRECTORY
                        | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=parent_descriptor,
                    )
                    try:
                        pinned_destination_info = os.fstat(
                            destination_descriptor
                        )
                        path_destination_info = os.stat(
                            destination_task.name,
                            dir_fd=parent_descriptor,
                            follow_symlinks=False,
                        )
                        created_identity = (
                            created_destination_info.st_dev,
                            created_destination_info.st_ino,
                        )
                        if (
                            pinned_destination_info.st_dev,
                            pinned_destination_info.st_ino,
                        ) != created_identity or (
                            path_destination_info.st_dev,
                            path_destination_info.st_ino,
                        ) != created_identity:
                            raise RuntimeError(
                                "new detached destination identity changed "
                                "before binding"
                            )
                        os.fchmod(
                            destination_descriptor,
                            manifest["destination_mode"],
                        )
                        marker_descriptor = os.open(
                            manifest["reservation_marker"],
                            os.O_WRONLY
                            | os.O_CREAT
                            | os.O_EXCL
                            | getattr(os, "O_NOFOLLOW", 0),
                            0o600,
                            dir_fd=destination_descriptor,
                        )
                        try:
                            os.fsync(marker_descriptor)
                            marker_info = os.fstat(marker_descriptor)
                        finally:
                            os.close(marker_descriptor)
                        destination_info = os.fstat(destination_descriptor)
                        os.fsync(destination_descriptor)
                        os.fsync(parent_descriptor)
                    finally:
                        os.close(destination_descriptor)
                finally:
                    os.close(parent_descriptor)
                manifest["destination_device"] = destination_info.st_dev
                manifest["destination_inode"] = destination_info.st_ino
                manifest["reservation_marker_device"] = marker_info.st_dev
                manifest["reservation_marker_inode"] = marker_info.st_ino
                manifest["phase"] = "preparing"
                write_manifest(manifest_path, manifest)
            if manifest["phase"] == "preparing":
                _validate_detached_private_parent(
                    destination_task.parent,
                    destination_name=destination_task.name,
                    manifest=manifest,
                )
                state = _detached_destination_state(
                    destination_task,
                    manifest,
                    require_marker=True,
                )
                if state in {"foreign", "missing"}:
                    raise RuntimeError(
                        "detached destination identity or inventory changed"
                    )
                if state == "partial":
                    _build_detached_destination(
                        destination_task,
                        manifest_path,
                        manifest,
                    )
                if (
                    _detached_destination_state(
                        destination_task,
                        manifest,
                        require_marker=True,
                    )
                    != "complete"
                ):
                    raise RuntimeError("detached destination did not validate")
                _validate_detached_sources(
                    source_task,
                    backing_source,
                    manifest,
                )
                manifest["phase"] = "finalizing"
                write_manifest(manifest_path, manifest)
            if manifest["phase"] == "finalizing":
                _validate_detached_private_parent(
                    destination_task.parent,
                    destination_name=destination_task.name,
                    manifest=manifest,
                )
                marker_present_state = _detached_destination_state(
                    destination_task,
                    manifest,
                    require_marker=True,
                )
                if marker_present_state == "complete":
                    root_descriptor = os.open(
                        destination_task,
                        os.O_RDONLY
                        | os.O_DIRECTORY
                        | getattr(os, "O_NOFOLLOW", 0),
                    )
                    try:
                        marker_info = os.stat(
                            manifest["reservation_marker"],
                            dir_fd=root_descriptor,
                            follow_symlinks=False,
                        )
                        if (marker_info.st_dev, marker_info.st_ino) != (
                            manifest["reservation_marker_device"],
                            manifest["reservation_marker_inode"],
                        ):
                            raise RuntimeError(
                                "detached reservation marker identity changed"
                            )
                        os.unlink(
                            manifest["reservation_marker"],
                            dir_fd=root_descriptor,
                        )
                        os.fsync(root_descriptor)
                    finally:
                        os.close(root_descriptor)
                elif (
                    _detached_destination_state(
                        destination_task,
                        manifest,
                        require_marker=False,
                    )
                    != "complete"
                ):
                    raise RuntimeError(
                        "detached finalization state is not safely replayable"
                    )
            if (
                _detached_destination_state(
                    destination_task,
                    manifest,
                    require_marker=False,
                )
                != "complete"
            ):
                raise RuntimeError("detached canonical tree did not validate")
            _validate_detached_sources(source_task, backing_source, manifest)
            _validate_detached_private_parent(
                destination_task.parent,
                destination_name=destination_task.name,
                manifest=manifest,
            )
            manifest["phase"] = "committed"
            manifest["committed_at"] = datetime.now(timezone.utc).isoformat()
            manifest.pop("error", None)
            write_manifest(manifest_path, manifest)
            _validate_detached_sources(source_task, backing_source, manifest)
            _validate_detached_private_parent(
                destination_task.parent,
                destination_name=destination_task.name,
                manifest=manifest,
            )
            if (
                _detached_destination_state(
                    destination_task,
                    manifest,
                    require_marker=False,
                )
                != "complete"
            ):
                raise RuntimeError("detached destination changed during commit")
            return manifest
        except BaseException as exc:
            _mark_detached_recovery_failed(manifest_path, manifest, exc)
            raise


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
    parser.add_argument("--detached-destination", type=Path)
    args = parser.parse_args()

    if args.materialize_link_view:
        if args.backing_source is None:
            parser.error("--materialize-link-view requires --backing-source")
        if args.detached_destination is None:
            parser.error(
                "--materialize-link-view requires --detached-destination"
            )
        manifest = materialize_link_view_detached_as_hardlinks(
            args.source_task,
            backing_source=args.backing_source,
            destination_task=args.detached_destination,
            manifest_path=args.manifest,
        )
        print(
            f"materialized recordings: {len(manifest['recordings'])}; "
            f"detached destination: {manifest['destination_task']}"
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
