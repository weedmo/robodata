"""Offline, preservation-first recovery for interrupted converter transactions.

The v1 converter left adjacent finalization/rebuild markers, but the producer
and recovery implementation were lost.  This module deliberately treats those
legacy files as untrusted evidence.  A recovery is authorized only after
no-follow path checks, a fresh dataset validation, durable-serial proof, and a
durable intent have all succeeded.

Recovery never removes output, archives, markers, state backups, or receipts.
Namespace changes use no-replace renames and are replayed from the fixed intent
path after a crash.
"""

from __future__ import annotations

import copy
import ctypes
import errno
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

import pyarrow as pa
import pyarrow.parquet as pq

from backend.converter.validation_service import (
    _conversion_schema_mismatch_class,
    run_full_validation_for_path_sync,
    run_quick_validation_for_path_sync,
)


RECOVERY_SCHEMA = "robodata-conversion-recovery/v1"
RECOVERY_MODES = frozenset(
    {
        "rollback",
        "adopt-finalization",
        "quarantine-restart",
        "commit-verified",
    }
)
_SERIAL_RE = re.compile(r"^\d{8}_\d{6}(?:_\d+)?$")
_FINALIZATION_SUFFIX = ".finalization-pending.json"
_REBUILD_SUFFIX = ".rebuild-journal.json"
_INTENT_SUFFIX = ".recovery-intent.json"
_RECEIPT_TOKEN = ".recovery-receipt-"
_MAX_JSON_BYTES = 8 * 1024 * 1024
_RENAME_NOREPLACE = 1
_NFS_SUPER_MAGIC = 0x6969
_INTENT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RECOVERY_PHASES = frozenset(
    {
        "output_quarantine_pending",
        "archive_restore_pending",
        "candidate_verify_pending",
        "state_replacement_pending",
        "state_backup_pending",
        "state_install_pending",
        "marker_audit_pending",
        "receipt_pending",
        "receipt_durable",
    }
)
_TERMINAL_STATUS = {
    "rollback": "rolled_back",
    "adopt-finalization": "adopted",
    "quarantine-restart": "restart_ready",
    "commit-verified": "committed",
}

ValidationRunner = Callable[[Path], Mapping[str, Any]]
CrashHook = Callable[[str], None]


class RecoveryError(RuntimeError):
    """Fail-closed recovery error with a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _raise(code: str, message: str) -> None:
    raise RecoveryError(code, message)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json_bytes(payload: Any, *, compact: bool = False) -> bytes:
    separators = (",", ":") if compact else None
    text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=separators,
        indent=None if compact else 2,
    )
    return (text if compact else f"{text}\n").encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _serials_sha256(serials: list[str] | set[str] | tuple[str, ...]) -> str:
    """Legacy-compatible digest: sorted serials, one per line, trailing LF."""
    ordered = sorted(serials)
    payload = "".join(f"{serial}\n" for serial in ordered).encode("utf-8")
    return _sha256(payload)


def _resolved_contract_class():
    try:
        from conversion.recording_contract import ResolvedRecordingContract
    except ModuleNotFoundError:
        submodule = Path(__file__).resolve().parents[2] / "rosbag2lerobot-svt"
        if submodule.is_dir() and str(submodule) not in sys.path:
            sys.path.insert(0, str(submodule))
        from conversion.recording_contract import ResolvedRecordingContract
    return ResolvedRecordingContract


def _partition_manifest_builder() -> Callable[..., dict[str, Any]]:
    """Load the canonical partition builder from the bundled converter source."""
    module_name = "_robodata_contract_partition_recordings"
    module = sys.modules.get(module_name)
    if module is None:
        module_path = (
            Path(__file__).resolve().parents[2]
            / "rosbag2lerobot-svt"
            / "scripts"
            / "partition_recordings.py"
        )
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            _raise(
                "raw_contract_probe_failed",
                "canonical raw contract probe could not be loaded",
            )
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(module_name, None)
            raise
    builder = getattr(module, "build_partition_manifest", None)
    if not callable(builder):
        _raise(
            "raw_contract_probe_failed",
            "canonical raw contract probe has no manifest builder",
        )
    return builder


def _current_raw_contract_manifest(
    *,
    raw_root: Path,
    raw_root_fd: int,
    task: str,
    target_fps: int,
) -> dict[str, Any]:
    """Resolve the current raw task through the canonical partition probe."""
    builder = _partition_manifest_builder()
    result = builder(
        raw_root=raw_root,
        raw_root_fd=raw_root_fd,
        task=task,
        target_fps=target_fps,
    )
    if not isinstance(result, dict):
        _raise(
            "raw_contract_probe_failed",
            "canonical raw contract probe returned an invalid manifest",
        )
    return result


def _identity(info: os.stat_result) -> tuple[int, int]:
    return (info.st_dev, info.st_ino)


def _identity_dict(info: os.stat_result) -> dict[str, int]:
    return {
        "dev": info.st_dev,
        "ino": info.st_ino,
        "mode": stat.S_IMODE(info.st_mode),
        "uid": info.st_uid,
        "gid": info.st_gid,
        "nlink": info.st_nlink,
    }


def _same_identity(info: os.stat_result, expected: Mapping[str, Any]) -> bool:
    return (
        info.st_dev == expected.get("dev")
        and info.st_ino == expected.get("ino")
    )


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _regular_read_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _open_directory_chain_nofollow(path: Path) -> int:
    absolute = Path(os.path.abspath(path))
    if not absolute.is_absolute() or ".." in absolute.parts:
        _raise("unsafe_path", f"directory path is not absolute and lexical: {path}")
    descriptor = os.open(absolute.anchor, _directory_flags())
    try:
        for component in absolute.parts[1:]:
            next_descriptor = os.open(
                component,
                _directory_flags(),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            _raise("unsafe_path", f"path is not a plain directory: {path}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_relative_directory_nofollow(root_fd: int, parts: tuple[str, ...]) -> int:
    descriptor = os.dup(root_fd)
    try:
        for component in parts:
            next_descriptor = os.open(
                component,
                _directory_flags(),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _stat_at(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _read_all_fd(fd: int, *, limit: int = _MAX_JSON_BYTES) -> bytes:
    info = os.fstat(fd)
    if info.st_size > limit:
        _raise("oversized_file", f"refusing to read {info.st_size} bytes")
    chunks: list[bytes] = []
    offset = 0
    while True:
        chunk = os.pread(fd, min(1024 * 1024, limit + 1 - offset), offset)
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
        if offset > limit:
            _raise("oversized_file", "file exceeded safe read limit")
    return b"".join(chunks)


def _read_regular_bytes_at(
    parent_fd: int,
    name: str,
    *,
    limit: int = _MAX_JSON_BYTES,
) -> tuple[bytes, dict[str, Any]]:
    try:
        fd = os.open(name, _regular_read_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise RecoveryError(
            "unsafe_file",
            f"cannot open {name!r} without following links",
        ) from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            _raise("unsafe_file", f"{name!r} is not a regular file")
        payload = _read_all_fd(fd, limit=limit)
        after = os.fstat(fd)
        path_info = _stat_at(parent_fd, name)
        if (
            path_info is None
            or _identity(before) != _identity(after)
            or _identity(after) != _identity(path_info)
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
        ):
            _raise("file_changed", f"{name!r} changed while it was read")
        fingerprint = {
            "kind": "regular",
            **_identity_dict(after),
            "size": after.st_size,
            "mtime_ns": after.st_mtime_ns,
            "ctime_ns": after.st_ctime_ns,
            "sha256": _sha256(payload),
        }
        return payload, fingerprint
    finally:
        os.close(fd)


def _read_json_at(
    parent_fd: int,
    name: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload, fingerprint = _read_regular_bytes_at(parent_fd, name)
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RecoveryError(
            "invalid_json",
            f"{name!r} is not valid UTF-8 JSON",
        ) from exc
    if not isinstance(decoded, dict):
        _raise("invalid_json", f"{name!r} must contain a JSON object")
    return decoded, fingerprint


def _read_dataset_info_at(
    dataset_fd: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    meta_fd = _open_relative_directory_nofollow(dataset_fd, ("meta",))
    try:
        payload, fingerprint = _read_regular_bytes_at(meta_fd, "info.json")
    finally:
        os.close(meta_fd)
    try:
        decoded = json.loads(payload.rstrip(b"\x00").decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RecoveryError(
            "invalid_dataset_info",
            "dataset meta/info.json is not valid UTF-8 JSON",
        ) from exc
    if not isinstance(decoded, dict):
        _raise("invalid_dataset_info", "dataset meta/info.json must be an object")
    return decoded, fingerprint


def _fingerprint_file_fd(fd: int, relative: str) -> dict[str, Any]:
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode):
        _raise("unsafe_tree", f"dataset entry is not regular: {relative}")

    digest = hashlib.sha256()
    offset = 0
    while offset < before.st_size:
        chunk = os.pread(fd, min(1024 * 1024, before.st_size - offset), offset)
        if not chunk:
            break
        digest.update(chunk)
        offset += len(chunk)

    after = os.fstat(fd)
    if (
        _identity(before) != _identity(after)
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ctime_ns != after.st_ctime_ns
    ):
        _raise("tree_changed", f"dataset file changed while hashing: {relative}")
    return {
        "type": "file",
        "path": relative,
        **_identity_dict(after),
        "size": after.st_size,
        "mtime_ns": after.st_mtime_ns,
        "ctime_ns": after.st_ctime_ns,
        "digest_kind": "full",
        "content_sha256": digest.hexdigest(),
    }


def _fingerprint_tree_fd(root_fd: int) -> tuple[str, int]:
    entries: list[dict[str, Any]] = []

    def visit(directory_fd: int, prefix: str) -> None:
        directory_before = os.fstat(directory_fd)
        try:
            names = sorted(os.listdir(directory_fd))
        except OSError as exc:
            raise RecoveryError(
                "unsafe_tree",
                f"cannot list dataset directory {prefix or '.'}",
            ) from exc
        for name in names:
            if "/" in name or name in {".", ".."}:
                _raise("unsafe_tree", f"invalid dataset entry name: {name!r}")
            relative = f"{prefix}/{name}" if prefix else name
            entry_info = os.stat(
                name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(entry_info.st_mode):
                _raise("unsafe_tree", f"dataset symlink is not allowed: {relative}")
            if stat.S_ISDIR(entry_info.st_mode):
                child_fd = os.open(name, _directory_flags(), dir_fd=directory_fd)
                try:
                    child_info = os.fstat(child_fd)
                    if _identity(child_info) != _identity(entry_info):
                        _raise(
                            "tree_changed",
                            f"dataset directory changed while opening: {relative}",
                        )
                    entries.append(
                        {
                            "type": "directory",
                            "path": relative,
                            **_identity_dict(child_info),
                            "size": child_info.st_size,
                            "mtime_ns": child_info.st_mtime_ns,
                            "ctime_ns": child_info.st_ctime_ns,
                        }
                    )
                    visit(child_fd, relative)
                    child_after = os.fstat(child_fd)
                    if _identity(child_after) != _identity(child_info):
                        _raise(
                            "tree_changed",
                            f"dataset directory changed while hashing: {relative}",
                        )
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(entry_info.st_mode):
                file_fd = os.open(name, _regular_read_flags(), dir_fd=directory_fd)
                try:
                    opened = os.fstat(file_fd)
                    if _identity(opened) != _identity(entry_info):
                        _raise(
                            "tree_changed",
                            f"dataset file changed while opening: {relative}",
                        )
                    entries.append(_fingerprint_file_fd(file_fd, relative))
                finally:
                    os.close(file_fd)
            else:
                _raise(
                    "unsafe_tree",
                    f"special dataset entry is not allowed: {relative}",
                )
        directory_after = os.fstat(directory_fd)
        if _identity(directory_before) != _identity(directory_after):
            _raise(
                "tree_changed",
                f"dataset directory changed while traversing: {prefix or '.'}",
            )

    visit(root_fd, "")
    return _sha256(_canonical_json_bytes(entries, compact=True)), len(entries)


def _fingerprint_metadata_tree_fd(root_fd: int) -> tuple[str, int]:
    """Fingerprint a potentially large raw tree without reading MCAP payloads."""
    entries: list[dict[str, Any]] = []

    def visit(directory_fd: int, prefix: str) -> None:
        directory_before = os.fstat(directory_fd)
        names = sorted(os.listdir(directory_fd))
        for name in names:
            if "/" in name or name in {".", ".."}:
                _raise("unsafe_raw_task", f"invalid raw entry name: {name!r}")
            relative = f"{prefix}/{name}" if prefix else name
            entry_info = os.stat(
                name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(entry_info.st_mode):
                _raise(
                    "unsafe_raw_task",
                    f"raw task symlink is not allowed: {relative}",
                )
            if stat.S_ISDIR(entry_info.st_mode):
                child_fd = os.open(name, _directory_flags(), dir_fd=directory_fd)
                try:
                    child_info = os.fstat(child_fd)
                    if _identity(child_info) != _identity(entry_info):
                        _raise(
                            "raw_task_changed",
                            f"raw directory changed while opening: {relative}",
                        )
                    entries.append(
                        {
                            "type": "directory",
                            "path": relative,
                            **_identity_dict(child_info),
                            "size": child_info.st_size,
                            "mtime_ns": child_info.st_mtime_ns,
                            "ctime_ns": child_info.st_ctime_ns,
                        }
                    )
                    visit(child_fd, relative)
                    child_after = os.fstat(child_fd)
                    if (
                        _identity(child_after) != _identity(child_info)
                        or child_after.st_mtime_ns != child_info.st_mtime_ns
                        or child_after.st_ctime_ns != child_info.st_ctime_ns
                    ):
                        _raise(
                            "raw_task_changed",
                            f"raw directory changed while scanning: {relative}",
                        )
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(entry_info.st_mode):
                file_fd = os.open(
                    name,
                    _regular_read_flags(),
                    dir_fd=directory_fd,
                )
                try:
                    opened = os.fstat(file_fd)
                    if _identity(opened) != _identity(entry_info):
                        _raise(
                            "raw_task_changed",
                            f"raw file changed while opening: {relative}",
                        )
                    entries.append(
                        {
                            "type": "file",
                            "path": relative,
                            **_identity_dict(opened),
                            "size": opened.st_size,
                            "mtime_ns": opened.st_mtime_ns,
                            "ctime_ns": opened.st_ctime_ns,
                        }
                    )
                finally:
                    os.close(file_fd)
            else:
                _raise(
                    "unsafe_raw_task",
                    f"special raw entry is not allowed: {relative}",
                )
        directory_after = os.fstat(directory_fd)
        if (
            _identity(directory_before) != _identity(directory_after)
            or directory_before.st_mtime_ns != directory_after.st_mtime_ns
            or directory_before.st_ctime_ns != directory_after.st_ctime_ns
        ):
            _raise(
                "raw_task_changed",
                f"raw directory changed while traversing: {prefix or '.'}",
            )

    visit(root_fd, "")
    return _sha256(_canonical_json_bytes(entries, compact=True)), len(entries)


def _fingerprint_directory_at(
    parent_fd: int,
    name: str,
) -> dict[str, Any] | None:
    path_info = _stat_at(parent_fd, name)
    if path_info is None:
        return None
    if not stat.S_ISDIR(path_info.st_mode):
        _raise("unsafe_tree", f"{name!r} is not a plain directory")
    try:
        root_fd = os.open(name, _directory_flags(), dir_fd=parent_fd)
    except OSError as exc:
        raise RecoveryError(
            "unsafe_tree",
            f"cannot open dataset directory {name!r} safely",
        ) from exc
    try:
        root_before = os.fstat(root_fd)
        if _identity(root_before) != _identity(path_info):
            _raise("tree_changed", f"{name!r} changed while it was opened")
        tree_sha256, entry_count = _fingerprint_tree_fd(root_fd)
        root_after = os.fstat(root_fd)
        final_path_info = _stat_at(parent_fd, name)
        if (
            final_path_info is None
            or _identity(root_before) != _identity(root_after)
            or _identity(root_after) != _identity(final_path_info)
        ):
            _raise("tree_changed", f"{name!r} changed while it was hashed")
        return {
            "kind": "directory",
            **_identity_dict(root_after),
            "size": root_after.st_size,
            "mtime_ns": root_after.st_mtime_ns,
            "ctime_ns": root_after.st_ctime_ns,
            "entry_count": entry_count,
            "tree_sha256": tree_sha256,
        }
    finally:
        os.close(root_fd)


def _fingerprint_raw_task_at(
    raw_root_fd: int,
    parts: tuple[str, ...],
) -> dict[str, Any]:
    parent_fd = _open_relative_directory_nofollow(raw_root_fd, parts[:-1])
    try:
        name = parts[-1]
        path_info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(path_info.st_mode):
            _raise("unsafe_raw_task", "raw task is not a plain directory")
        task_fd = os.open(name, _directory_flags(), dir_fd=parent_fd)
        try:
            before = os.fstat(task_fd)
            if _identity(before) != _identity(path_info):
                _raise("raw_task_changed", "raw task changed while opening")
            tree_sha256, entry_count = _fingerprint_metadata_tree_fd(task_fd)
            after = os.fstat(task_fd)
            final_path_info = _stat_at(parent_fd, name)
            if (
                final_path_info is None
                or _identity(before) != _identity(after)
                or _identity(after) != _identity(final_path_info)
                or before.st_mtime_ns != after.st_mtime_ns
                or before.st_ctime_ns != after.st_ctime_ns
            ):
                _raise("raw_task_changed", "raw task changed while fingerprinting")
            return {
                "kind": "directory",
                **_identity_dict(after),
                "size": after.st_size,
                "mtime_ns": after.st_mtime_ns,
                "ctime_ns": after.st_ctime_ns,
                "entry_count": entry_count,
                "tree_sha256": tree_sha256,
            }
        finally:
            os.close(task_fd)
    finally:
        os.close(parent_fd)


def _fingerprints_equal(
    actual: Mapping[str, Any] | None,
    expected: Mapping[str, Any] | None,
) -> bool:
    if actual is None or expected is None:
        return actual is expected
    keys = {
        "kind",
        "dev",
        "ino",
        "mode",
        "uid",
        "gid",
        "nlink",
        "size",
        "mtime_ns",
        "entry_count",
        "tree_sha256",
        "sha256",
    }
    return all(actual.get(key) == expected.get(key) for key in keys)


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


def _filesystem_type(fd: int) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    fstatfs = getattr(libc, "fstatfs", None)
    if fstatfs is None:
        _raise("filesystem_probe_unsupported", "fstatfs is unavailable")
    fstatfs.argtypes = [ctypes.c_int, ctypes.POINTER(_LinuxStatFs)]
    fstatfs.restype = ctypes.c_int
    result = _LinuxStatFs()
    if fstatfs(fd, ctypes.byref(result)) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return int(result.f_type)


def _rename_noreplace(
    src_dir_fd: int,
    src_name: str,
    dst_dir_fd: int,
    dst_name: str,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        _raise("rename_unsupported", "renameat2(RENAME_NOREPLACE) is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        src_dir_fd,
        os.fsencode(src_name),
        dst_dir_fd,
        os.fsencode(dst_name),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error = ctypes.get_errno()
        unsupported = {errno.EINVAL, errno.EOPNOTSUPP}
        if (
            error in unsupported
            and _filesystem_type(src_dir_fd) == _NFS_SUPER_MAGIC
            and _filesystem_type(dst_dir_fd) == _NFS_SUPER_MAGIC
            and os.fstat(src_dir_fd).st_dev == os.fstat(dst_dir_fd).st_dev
        ):
            if os.environ.get("CURATION_RECOVERY_ISOLATED") != "true":
                _raise(
                    "nfs_isolation_required",
                    "NFS rename fallback requires the recovery-only isolated "
                    "container",
                )
            if _stat_at(dst_dir_fd, dst_name) is not None:
                raise FileExistsError(
                    errno.EEXIST,
                    os.strerror(errno.EEXIST),
                    f"{src_name} -> {dst_name}",
                )
            os.rename(
                src_name,
                dst_name,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )
            return
        raise OSError(error, os.strerror(error), f"{src_name} -> {dst_name}")


def _atomic_write_json(
    parent_fd: int,
    name: str,
    payload: Mapping[str, Any],
    *,
    replace: bool,
    mode: int = 0o600,
    uid: int | None = None,
    gid: int | None = None,
    bind_created_owner: bool = False,
) -> dict[str, Any]:
    temporary_name = f".{name}.tmp-{uuid.uuid4().hex}"
    fd = os.open(
        temporary_name,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        mode,
        dir_fd=parent_fd,
    )
    try:
        current = os.fstat(fd)
        if bind_created_owner:
            if uid is not None or gid is not None:
                _raise(
                    "invalid_intent",
                    "intent owner binding cannot use fchown targets",
                )
            if not isinstance(payload, dict):
                _raise(
                    "invalid_intent",
                    "intent must be mutable to bind its file identity",
                )
            owner = payload.get("intent_owner")
            if owner is None:
                payload["intent_owner"] = {
                    "uid": current.st_uid,
                    "gid": current.st_gid,
                }
            elif (
                not isinstance(owner, Mapping)
                or isinstance(owner.get("uid"), bool)
                or not isinstance(owner.get("uid"), int)
                or owner.get("uid") < 0
                or isinstance(owner.get("gid"), bool)
                or not isinstance(owner.get("gid"), int)
                or owner.get("gid") < 0
            ):
                _raise(
                    "invalid_intent",
                    "intent_owner must contain non-negative uid and gid",
                )
            elif (
                owner.get("uid") != current.st_uid
                or owner.get("gid") != current.st_gid
            ):
                _raise(
                    "intent_owner_changed",
                    "new intent inode owner differs from its durable binding",
                )
            payload["intent_file"] = {
                "dev": current.st_dev,
                "ino": current.st_ino,
            }
        encoded = _canonical_json_bytes(payload)
        target_uid = current.st_uid if uid is None else uid
        target_gid = current.st_gid if gid is None else gid
        if (current.st_uid, current.st_gid) != (target_uid, target_gid):
            try:
                os.fchown(fd, target_uid, target_gid)
            except PermissionError:
                after_chown = os.fstat(fd)
                if (after_chown.st_uid, after_chown.st_gid) != (
                    target_uid,
                    target_gid,
                ):
                    raise
        os.fchmod(fd, mode)
        written = 0
        while written < len(encoded):
            written += os.write(fd, encoded[written:])
        os.fsync(fd)
        temporary_info = os.fstat(fd)
        if not stat.S_ISREG(temporary_info.st_mode) or temporary_info.st_nlink != 1:
            _raise("unsafe_intent", "intent temporary is not private")
    finally:
        os.close(fd)

    if replace:
        os.replace(
            temporary_name,
            name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
    else:
        _rename_noreplace(parent_fd, temporary_name, parent_fd, name)
    os.fsync(parent_fd)
    installed, fingerprint = _read_regular_bytes_at(parent_fd, name)
    if installed != encoded:
        _raise("intent_changed", f"installed JSON changed unexpectedly: {name}")
    return fingerprint


def _parse_cell_task(cell_task: str) -> tuple[str, ...]:
    if not isinstance(cell_task, str):
        _raise("invalid_cell_task", "cell_task must be a string")
    pure = PurePosixPath(cell_task)
    parts = pure.parts
    if (
        pure.is_absolute()
        or len(parts) < 2
        or any(part in {"", ".", ".."} for part in parts)
        or any(part != part.strip() for part in parts)
        or any("\x00" in part for part in parts)
    ):
        _raise("invalid_cell_task", f"unsafe cell_task: {cell_task!r}")
    normalized = "/".join(parts)
    if normalized != cell_task:
        _raise("invalid_cell_task", f"non-canonical cell_task: {cell_task!r}")
    return parts


def _require_plain_basename(value: Any, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\x00" in value
    ):
        _raise("invalid_intent", f"{label} must be one plain basename")
    return value


def _require_int(value: Any, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _raise("invalid_intent", f"{label} must be an integer >= {minimum}")
    return value


def _require_sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        _raise("invalid_intent", f"{label} must be a lowercase SHA-256 digest")
    return value


def _require_identity_payload(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _raise("invalid_intent", f"{label} must be an identity object")
    for key in ("dev", "ino", "mode", "uid", "gid", "nlink"):
        _require_int(value.get(key), label=f"{label}.{key}")
    return value


def _require_fingerprint_payload(
    value: Any,
    *,
    label: str,
    kind: str,
) -> Mapping[str, Any]:
    identity = _require_identity_payload(value, label=label)
    if identity.get("kind") != kind:
        _raise("invalid_intent", f"{label}.kind must be {kind!r}")
    _require_int(identity.get("size"), label=f"{label}.size")
    _require_int(identity.get("mtime_ns"), label=f"{label}.mtime_ns")
    _require_int(identity.get("ctime_ns"), label=f"{label}.ctime_ns")
    if kind == "regular":
        _require_sha256(identity.get("sha256"), label=f"{label}.sha256")
    else:
        _require_int(identity.get("entry_count"), label=f"{label}.entry_count")
        _require_sha256(
            identity.get("tree_sha256"),
            label=f"{label}.tree_sha256",
        )
    return identity


def _marker_names(task_name: str) -> dict[str, str]:
    prefix = f".{task_name}"
    return {
        "finalization": f"{prefix}{_FINALIZATION_SUFFIX}",
        "rebuild": f"{prefix}{_REBUILD_SUFFIX}",
        "intent": f"{prefix}{_INTENT_SUFFIX}",
    }


def _dataset_quick_valid_at(parent_fd: int, output_name: str) -> bool:
    output_info = _stat_at(parent_fd, output_name)
    if output_info is None:
        return True
    if not stat.S_ISDIR(output_info.st_mode):
        _raise("unsafe_output", "converter output is not a plain directory")
    output_fd = os.open(output_name, _directory_flags(), dir_fd=parent_fd)
    try:
        if _identity(os.fstat(output_fd)) != _identity(output_info):
            _raise("output_changed", "converter output changed while opening")
        data_info = _stat_at(output_fd, "data")
        meta_info = _stat_at(output_fd, "meta")
        if (
            data_info is None
            or not stat.S_ISDIR(data_info.st_mode)
            or meta_info is None
            or not stat.S_ISDIR(meta_info.st_mode)
        ):
            return False
        meta_fd = os.open("meta", _directory_flags(), dir_fd=output_fd)
        try:
            required = {
                "info.json": stat.S_ISREG,
                "tasks.parquet": stat.S_ISREG,
                "episodes": stat.S_ISDIR,
            }
            for name, predicate in required.items():
                info = _stat_at(meta_fd, name)
                if info is None or not predicate(info.st_mode):
                    return False
        finally:
            os.close(meta_fd)
        try:
            result = run_quick_validation_for_path_sync(
                Path(f"/proc/self/fd/{output_fd}")
            )
        except Exception:
            return False
        return result.get("status") == "passed"
    finally:
        os.close(output_fd)


def recovery_blockers(
    cell_task: str,
    *,
    lerobot_root: Path,
) -> list[str]:
    """Return active recovery artifacts without trusting marker phase strings."""
    parts = _parse_cell_task(cell_task)
    root_fd = _open_directory_chain_nofollow(lerobot_root)
    try:
        try:
            parent_fd = _open_relative_directory_nofollow(root_fd, parts[:-1])
        except FileNotFoundError:
            return []
        try:
            blockers: list[str] = []
            for name in _marker_names(parts[-1]).values():
                info = _stat_at(parent_fd, name)
                if info is not None:
                    blockers.append(name)
            if not _dataset_quick_valid_at(parent_fd, parts[-1]):
                blockers.append("incomplete-output")
            return blockers
        finally:
            os.close(parent_fd)
    finally:
        os.close(root_fd)


class RecoveryService:
    """One-task offline recovery coordinator."""

    def __init__(
        self,
        raw_root: Path,
        lerobot_root: Path,
        state_file: Path | None = None,
        *,
        validation_runner: ValidationRunner | None = None,
        crash_hook: CrashHook | None = None,
        authorized_legacy_marker_sha256s: set[str] | frozenset[str] | None = None,
        contract_manifest_path: Path | None = None,
        authorized_contract_manifest_sha256: str | None = None,
    ):
        self.raw_root = Path(raw_root)
        self.lerobot_root = Path(lerobot_root)
        self.state_file = (
            Path(state_file)
            if state_file is not None
            else self.lerobot_root / "convert_state.json"
        )
        if (
            self.state_file.parent != self.lerobot_root
            or self.state_file.name != "convert_state.json"
        ):
            _raise(
                "unsafe_state_path",
                "state_file must be the canonical convert_state.json under lerobot_root",
            )
        self.validation_runner = validation_runner or run_full_validation_for_path_sync
        self.crash_hook = crash_hook or (lambda _window: None)
        authorized = frozenset(authorized_legacy_marker_sha256s or ())
        for digest in authorized:
            if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
                _raise(
                    "invalid_authorization",
                    "legacy marker authorization must be a lowercase SHA-256 digest",
                )
        self.authorized_legacy_marker_sha256s = authorized
        if (contract_manifest_path is None) != (
            authorized_contract_manifest_sha256 is None
        ):
            _raise(
                "invalid_authorization",
                "contract manifest path and SHA-256 authorization must be paired",
            )
        self.contract_manifest_path = (
            None
            if contract_manifest_path is None
            else Path(os.path.abspath(contract_manifest_path))
        )
        if self.contract_manifest_path is not None:
            protected_roots = (
                Path(os.path.abspath(self.raw_root)),
                Path(os.path.abspath(self.lerobot_root)),
            )
            if any(
                self.contract_manifest_path == root
                or root in self.contract_manifest_path.parents
                for root in protected_roots
            ):
                _raise(
                    "unsafe_contract_manifest_path",
                    "contract manifest must be outside raw and lerobot roots",
                )
        self.authorized_contract_manifest_sha256 = (
            authorized_contract_manifest_sha256
        )
        self._contract_manifest_fingerprint: dict[str, Any] | None = None
        self._expected_recording_contract: Any | None = None
        self._raw_contract_probe_cache: dict[str, dict[str, Any]] = {}
        if authorized_contract_manifest_sha256 is not None:
            _require_sha256(
                authorized_contract_manifest_sha256,
                label="authorized_contract_manifest_sha256",
            )
            _, fingerprint = self._read_contract_manifest_file()
            self._contract_manifest_fingerprint = fingerprint

    def _read_contract_manifest_file(
        self,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        path = self.contract_manifest_path
        if path is None:
            _raise("invalid_authorization", "contract manifest is not configured")
        parent_fd = _open_directory_chain_nofollow(path.parent)
        try:
            payload_bytes, fingerprint = _read_regular_bytes_at(
                parent_fd,
                path.name,
            )
        finally:
            os.close(parent_fd)
        if fingerprint["mode"] != 0o600 or fingerprint["nlink"] != 1:
            _raise(
                "unsafe_file",
                "contract manifest must be a private mode 0600 single-link regular file",
            )
        if fingerprint["sha256"] != self.authorized_contract_manifest_sha256:
            _raise(
                "manifest_tampered",
                "contract manifest full-file SHA-256 is not authorized",
            )
        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RecoveryError(
                "invalid_contract_manifest",
                "contract manifest is not valid UTF-8 JSON",
            ) from exc
        if not isinstance(payload, dict):
            _raise("invalid_contract_manifest", "contract manifest must be an object")
        return payload, fingerprint

    def _contract_binding(
        self,
        *,
        cell_task: str,
        raw_serials: list[str],
        raw_fd: int,
    ) -> dict[str, Any] | None:
        if self.contract_manifest_path is None:
            return None
        payload, fingerprint = self._read_contract_manifest_file()
        if (
            self._contract_manifest_fingerprint is not None
            and not _fingerprints_equal(
                fingerprint,
                self._contract_manifest_fingerprint,
            )
        ):
            _raise(
                "manifest_tampered",
                "authorized contract manifest inode or bytes changed",
            )
        expected_manifest_fields = {
            "version",
            "contract_version",
            "digest_algorithm",
            "task",
            "target_fps",
            "recordings",
            "partitions",
            "invalid",
            "summary",
            "invariants",
        }
        required_invariants = {
            "partition_intersections_empty": True,
            "raw_mutation_performed": False,
            "recorded_exactly_once": True,
            "resolved_invalid_intersection_empty": True,
        }
        if (
            set(payload) != expected_manifest_fields
            or type(payload.get("version")) is not int
            or payload.get("version") != 1
            or type(payload.get("contract_version")) is not int
            or payload.get("contract_version") != 1
            or payload.get("digest_algorithm") != "sha256"
            or payload.get("task") != cell_task
            or payload.get("invalid") != []
            or _canonical_json_bytes(
                payload.get("invariants"),
                compact=True,
            )
            != _canonical_json_bytes(required_invariants, compact=True)
        ):
            _raise(
                "invalid_contract_manifest",
                "contract manifest version, task, invalid set, or invariants mismatch",
            )
        partitions = payload.get("partitions")
        if not isinstance(partitions, list) or len(partitions) != 1:
            _raise(
                "invalid_contract_manifest",
                "contract manifest must contain exactly one partition",
            )
        partition = partitions[0]
        if not isinstance(partition, dict) or set(partition) != {
            "digest",
            "contract",
            "serials",
        }:
            _raise(
                "invalid_contract_manifest",
                "contract partition must contain exactly digest, contract, and serials",
            )
        digest = partition.get("digest")
        contract_payload = partition.get("contract")
        serials = partition.get("serials")
        if (
            not isinstance(digest, str)
            or _SHA256_RE.fullmatch(digest) is None
            or not isinstance(contract_payload, dict)
            or not isinstance(serials, list)
        ):
            _raise(
                "invalid_contract_manifest",
                "contract partition digest or serial set is invalid",
            )
        if (
            any(
                not isinstance(serial, str) or _SERIAL_RE.fullmatch(serial) is None
                for serial in serials
            )
            or serials != sorted(serials)
            or len(serials) != len(set(serials))
            or serials != raw_serials
        ):
            _raise(
                "invalid_contract_manifest",
                "contract partition serial set is invalid",
            )
        contract_class = _resolved_contract_class()
        try:
            contract = contract_class.from_dict(contract_payload)
        except Exception as exc:
            raise RecoveryError(
                "invalid_contract_manifest",
                f"embedded resolved recording contract is invalid: {exc}",
            ) from exc
        if (
            contract.digest != digest
            or type(payload.get("target_fps")) is not int
            or payload.get("target_fps") != contract.conversion_schema.fps
        ):
            _raise(
                "invalid_contract_manifest",
                "contract digest or target_fps does not match embedded contract",
            )
        recordings = payload.get("recordings")
        expected_recordings = [
            {"digest": digest, "serial": serial, "status": "resolved"}
            for serial in serials
        ]
        expected_summary = {
            "invalid": 0,
            "partition_count": 1,
            "resolved": len(serials),
            "total": len(serials),
        }
        if (
            recordings != expected_recordings
            or _canonical_json_bytes(payload.get("summary"), compact=True)
            != _canonical_json_bytes(expected_summary, compact=True)
        ):
            _raise(
                "invalid_contract_manifest",
                "manifest recordings or summary do not exactly match the partition",
            )
        parts = _parse_cell_task(cell_task)
        raw_before = _fingerprint_raw_task_at(raw_fd, parts)
        cached_probe = self._raw_contract_probe_cache.get(cell_task)
        if cached_probe is None or not _fingerprints_equal(
            raw_before,
            cached_probe.get("raw_fingerprint"),
        ):
            try:
                current_manifest = _current_raw_contract_manifest(
                    raw_root=self.raw_root,
                    raw_root_fd=raw_fd,
                    task=cell_task,
                    target_fps=contract.conversion_schema.fps,
                )
            except RecoveryError:
                raise
            except Exception as exc:
                raise RecoveryError(
                    "raw_contract_probe_failed",
                    "canonical contract probe failed for current raw recordings",
                ) from exc
            raw_after = _fingerprint_raw_task_at(raw_fd, parts)
            if not _fingerprints_equal(raw_after, raw_before):
                _raise(
                    "raw_task_changed",
                    "raw task changed during canonical contract probing",
                )
            if _canonical_json_bytes(
                current_manifest,
                compact=True,
            ) != _canonical_json_bytes(payload, compact=True):
                _raise(
                    "raw_contract_mismatch",
                    "authorized contract manifest does not match current raw recordings",
                )
            cached_probe = {
                "raw_fingerprint": raw_after,
                "manifest_sha256": _sha256(
                    _canonical_json_bytes(current_manifest, compact=True)
                ),
            }
            self._raw_contract_probe_cache[cell_task] = cached_probe
        self._expected_recording_contract = contract
        return {
            "path": str(self.contract_manifest_path),
            "authorized_sha256": self.authorized_contract_manifest_sha256,
            "fingerprint": fingerprint,
            "contract_digest": digest,
            "raw_serials_sha256": _serials_sha256(raw_serials),
            "raw_contract_probe_sha256": cached_probe["manifest_sha256"],
            "target_fps": contract.conversion_schema.fps,
        }

    def _hook(self, window: str) -> None:
        self.crash_hook(window)

    def _open_context(
        self,
        cell_task: str,
    ) -> tuple[tuple[str, ...], int, int, int]:
        parts = _parse_cell_task(cell_task)
        lerobot_fd = _open_directory_chain_nofollow(self.lerobot_root)
        try:
            parent_fd = _open_relative_directory_nofollow(
                lerobot_fd,
                parts[:-1],
            )
        except BaseException:
            os.close(lerobot_fd)
            raise
        try:
            raw_fd = _open_directory_chain_nofollow(self.raw_root)
        except BaseException:
            os.close(parent_fd)
            os.close(lerobot_fd)
            raise
        return parts, lerobot_fd, parent_fd, raw_fd

    def _raw_serials(self, raw_root_fd: int, parts: tuple[str, ...]) -> list[str]:
        try:
            task_fd = _open_relative_directory_nofollow(raw_root_fd, parts)
        except OSError as exc:
            raise RecoveryError(
                "raw_task_unavailable",
                f"cannot open raw task safely: {'/'.join(parts)}",
            ) from exc
        try:
            serials: list[str] = []
            for name in sorted(os.listdir(task_fd)):
                if not _SERIAL_RE.fullmatch(name):
                    continue
                info = os.stat(name, dir_fd=task_fd, follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode):
                    _raise(
                        "unsafe_raw_task",
                        f"raw serial is not a plain directory: {name}",
                    )
                child_fd = os.open(name, _directory_flags(), dir_fd=task_fd)
                try:
                    opened = os.fstat(child_fd)
                    if _identity(opened) != _identity(info):
                        _raise(
                            "raw_task_changed",
                            f"raw serial changed while opening: {name}",
                        )
                finally:
                    os.close(child_fd)
                serials.append(name)
            return serials
        finally:
            os.close(task_fd)

    def _read_marker(
        self,
        parent_fd: int,
        *,
        kind: str,
        basename: str,
        cell_task: str,
        output_path: Path,
    ) -> dict[str, Any] | None:
        if _stat_at(parent_fd, basename) is None:
            return None
        payload, fingerprint = _read_json_at(parent_fd, basename)
        if payload.get("version") != 1:
            _raise("unsupported_marker", f"{basename} has unsupported version")
        if payload.get("cell_task") != cell_task:
            _raise("marker_mismatch", f"{basename} cell_task does not match")
        if payload.get("output_root") != str(output_path):
            _raise("marker_mismatch", f"{basename} output_root does not match")
        phase = payload.get("phase")
        if not isinstance(phase, str) or not phase.strip():
            _raise("marker_mismatch", f"{basename} has no valid phase")
        snapshot_key = (
            "raw_snapshot_before"
            if kind == "finalization"
            else "expected_snapshot_sha256"
        )
        snapshot = payload.get(snapshot_key)
        if (
            not isinstance(snapshot, str)
            or _SHA256_RE.fullmatch(snapshot) is None
            or snapshot == "0" * 64
        ):
            _raise(
                "marker_mismatch",
                f"{basename} has no credible {snapshot_key}",
            )
        return {
            "kind": kind,
            "basename": basename,
            "payload": payload,
            "fingerprint": fingerprint,
        }

    def _archive_basename(
        self,
        rebuild_marker: Mapping[str, Any],
        output_name: str,
        output_parent: Path,
    ) -> str | None:
        archive_raw = rebuild_marker["payload"].get("archive_path")
        if archive_raw is None:
            return None
        if not isinstance(archive_raw, str):
            _raise("marker_mismatch", "rebuild journal has no archive_path")
        archive = Path(archive_raw)
        expected_prefix = f".{output_name}.rebuild-output-"
        if (
            not archive.is_absolute()
            or archive.parent != output_parent
            or not archive.name.startswith(expected_prefix)
            or "/" in archive.name
        ):
            _raise(
                "marker_mismatch",
                "rebuild archive is not the expected adjacent plain path",
            )
        return archive.name

    def _read_markers(
        self,
        parent_fd: int,
        *,
        cell_task: str,
        output_path: Path,
        output_name: str,
    ) -> dict[str, dict[str, Any] | None]:
        names = _marker_names(output_name)
        finalization = self._read_marker(
            parent_fd,
            kind="finalization",
            basename=names["finalization"],
            cell_task=cell_task,
            output_path=output_path,
        )
        rebuild = self._read_marker(
            parent_fd,
            kind="rebuild",
            basename=names["rebuild"],
            cell_task=cell_task,
            output_path=output_path,
        )
        self._validate_paired_markers(finalization, rebuild)
        return {"finalization": finalization, "rebuild": rebuild}

    def _validate_paired_markers(
        self,
        finalization: Mapping[str, Any] | None,
        rebuild: Mapping[str, Any] | None,
    ) -> None:
        """Validate the cross-marker fields that bind one interrupted rebuild."""
        if finalization is not None and rebuild is not None:
            final_payload = finalization["payload"]
            rebuild_payload = rebuild["payload"]
            if (
                final_payload.get("rebuild_token")
                != rebuild_payload.get("rebuild_token")
                or final_payload.get("build_fingerprint")
                != rebuild_payload.get("build_fingerprint")
                or final_payload.get("raw_snapshot_before")
                != rebuild_payload.get("expected_snapshot_sha256")
            ):
                _raise(
                    "marker_mismatch",
                    "paired finalization/rebuild markers disagree",
                )

    def _validate_marker_raw_bindings(
        self,
        markers: Mapping[str, Mapping[str, Any] | None],
        raw_serials: list[str],
    ) -> None:
        """Bind every marker snapshot to current raw or explicit legacy evidence."""
        current_digest = _serials_sha256(raw_serials)
        for kind in ("finalization", "rebuild"):
            marker = markers.get(kind)
            if marker is None:
                continue
            payload = marker["payload"]
            snapshot_key = (
                "raw_snapshot_before"
                if kind == "finalization"
                else "expected_snapshot_sha256"
            )
            if payload[snapshot_key] == current_digest:
                continue

            marker_sha256 = marker["fingerprint"]["sha256"]
            if marker_sha256 not in self.authorized_legacy_marker_sha256s:
                _raise(
                    "marker_snapshot_mismatch",
                    f"{kind} marker snapshot is not bound to current raw; "
                    "an independently verified full-marker SHA-256 authorization "
                    "is required for this legacy marker",
                )

            embedded = payload.get("raw_serials_before")
            if embedded is None:
                continue
            if (
                not isinstance(embedded, list)
                or any(
                    not isinstance(serial, str)
                    or _SERIAL_RE.fullmatch(serial) is None
                    for serial in embedded
                )
                or embedded != sorted(embedded)
                or len(embedded) != len(set(embedded))
                or embedded != raw_serials
            ):
                _raise(
                    "marker_snapshot_mismatch",
                    f"{kind} marker embedded raw serials do not match current raw",
                )

    def _episode_parquet_fds(self, root_fd: int) -> list[tuple[str, int]]:
        meta_fd = -1
        episodes_fd = -1
        try:
            meta_fd = os.open("meta", _directory_flags(), dir_fd=root_fd)
            episodes_fd = os.open(
                "episodes",
                _directory_flags(),
                dir_fd=meta_fd,
            )
        except OSError as exc:
            if episodes_fd >= 0:
                os.close(episodes_fd)
            if meta_fd >= 0:
                os.close(meta_fd)
            raise RecoveryError(
                "serial_validation_failed",
                "dataset has no safe meta/episodes directory",
            ) from exc
        os.close(meta_fd)

        opened: list[tuple[str, int]] = []

        def visit(directory_fd: int, prefix: str) -> None:
            for name in sorted(os.listdir(directory_fd)):
                info = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                relative = f"{prefix}/{name}" if prefix else name
                if stat.S_ISDIR(info.st_mode):
                    child_fd = os.open(
                        name,
                        _directory_flags(),
                        dir_fd=directory_fd,
                    )
                    try:
                        if _identity(os.fstat(child_fd)) != _identity(info):
                            _raise(
                                "tree_changed",
                                f"episode directory changed: {relative}",
                            )
                        visit(child_fd, relative)
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(info.st_mode) and name.endswith(".parquet"):
                    file_fd = os.open(
                        name,
                        _regular_read_flags(),
                        dir_fd=directory_fd,
                    )
                    if _identity(os.fstat(file_fd)) != _identity(info):
                        os.close(file_fd)
                        _raise(
                            "tree_changed",
                            f"episode parquet changed: {relative}",
                        )
                    opened.append((relative, file_fd))
                else:
                    _raise(
                        "unsafe_tree",
                        f"unexpected episode metadata entry: {relative}",
                    )

        try:
            visit(episodes_fd, "")
            return opened
        except BaseException:
            for _, descriptor in opened:
                os.close(descriptor)
            raise
        finally:
            os.close(episodes_fd)

    def _durable_serials_from_fd(self, root_fd: int) -> list[str]:
        parquet_fds = self._episode_parquet_fds(root_fd)
        serials: list[str] = []
        try:
            for relative, descriptor in parquet_fds:
                before = os.fstat(descriptor)
                with os.fdopen(os.dup(descriptor), "rb") as handle:
                    try:
                        table = pq.read_table(
                            pa.PythonFile(handle),
                            columns=["Serial_number"],
                        )
                    except Exception as exc:
                        raise RecoveryError(
                            "serial_validation_failed",
                            f"cannot read Serial_number from {relative}",
                        ) from exc
                after = os.fstat(descriptor)
                if (
                    _identity(before) != _identity(after)
                    or before.st_size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns
                ):
                    _raise(
                        "tree_changed",
                        f"episode parquet changed while reading: {relative}",
                    )
                values = table.column("Serial_number").to_pylist()
                for value in values:
                    if not isinstance(value, str) or not value:
                        _raise(
                            "serial_validation_failed",
                            f"null/invalid Serial_number in {relative}",
                        )
                    serials.append(value)
        finally:
            for _, descriptor in parquet_fds:
                os.close(descriptor)

        if len(serials) != len(set(serials)):
            _raise(
                "duplicate_serial",
                "dataset contains duplicate durable Serial_number values",
            )
        return sorted(serials)

    def _validation_proof(
        self,
        *,
        parent_fd: int,
        output_name: str,
        output_path: Path,
        raw_serials: list[str],
        require_passed: bool,
    ) -> dict[str, Any]:
        before = _fingerprint_directory_at(parent_fd, output_name)
        if before is None:
            _raise("missing_output", f"dataset output is missing: {output_path}")
        dataset_fd = os.open(output_name, _directory_flags(), dir_fd=parent_fd)
        try:
            if not _same_identity(os.fstat(dataset_fd), before):
                _raise("tree_changed", f"dataset changed while opening: {output_path}")
            anchored_path = Path("/proc/self/fd") / str(dataset_fd)
            contract_info: dict[str, Any] | None = None
            contract_info_fingerprint: dict[str, Any] | None = None
            if self._expected_recording_contract is not None:
                contract_info, contract_info_fingerprint = _read_dataset_info_at(
                    dataset_fd
                )
            try:
                validation = dict(self.validation_runner(anchored_path))
            except RecoveryError:
                raise
            except Exception as exc:
                raise RecoveryError(
                    "validation_failed",
                    f"full validation raised for {output_path}",
                ) from exc
            contract_validation: str | None = None
            if self._expected_recording_contract is not None:
                current_info, current_info_fingerprint = _read_dataset_info_at(
                    dataset_fd
                )
                if (
                    current_info != contract_info
                    or current_info_fingerprint != contract_info_fingerprint
                ):
                    _raise(
                        "tree_changed",
                        "dataset info changed during contract validation",
                    )
                mismatch_class = _conversion_schema_mismatch_class()
                try:
                    self._expected_recording_contract.assert_dataset_info_compatible(
                        contract_info,
                        context=str(output_path),
                    )
                except mismatch_class as exc:
                    contract_validation = "mismatch"
                    validation = {
                        "status": "failed",
                        "summary": (
                            "Full failed: resolved recording contract mismatch "
                            f"({exc})"
                        ),
                        "checked_at": _utc_now(),
                    }
                else:
                    contract_validation = "passed"
            after = _fingerprint_directory_at(parent_fd, output_name)
            if not _fingerprints_equal(after, before):
                _raise(
                    "tree_changed",
                    f"dataset changed during validation: {output_path}",
                )
            status = validation.get("status")
            if require_passed and status != "passed":
                _raise(
                    "validation_not_passed",
                    f"fresh full validation is {status!r}, not 'passed'",
                )
            serials: list[str] = []
            if status == "passed":
                serials = self._durable_serials_from_fd(dataset_fd)
        finally:
            os.close(dataset_fd)
        if status == "passed":
            final = _fingerprint_directory_at(parent_fd, output_name)
            if not _fingerprints_equal(final, before):
                _raise(
                    "tree_changed",
                    f"dataset changed during serial validation: {output_path}",
                )
        raw_set = set(raw_serials)
        durable_set = set(serials)
        raw_subset = durable_set.issubset(raw_set)
        if require_passed and not raw_subset:
            _raise(
                "raw_subset_mismatch",
                "durable dataset serials are not a subset of raw serials",
            )
        return {
            "validation": validation,
            "contract_validation": contract_validation,
            "tree": before,
            "durable_serials": serials,
            "durable_count": len(serials),
            "durable_serials_sha256": _serials_sha256(serials),
            "raw_count": len(raw_serials),
            "raw_serials_sha256": _serials_sha256(raw_serials),
            "raw_subset": raw_subset,
            "duplicate_count": 0,
        }

    def _compatible_adoption_proof(
        self,
        *,
        parent_fd: int,
        rebuild_marker: Mapping[str, Any],
        proof: Mapping[str, Any],
        output_parent: Path,
        output_name: str,
        cell_task: str,
    ) -> dict[str, Any] | None:
        journal = rebuild_marker["payload"]
        compatible = journal.get("compatible_build_adoption")
        if compatible is None:
            return None
        if not isinstance(compatible, dict) or compatible.get("version") != 1:
            _raise("marker_mismatch", "invalid compatible build adoption proof")
        audit_raw = compatible.get("audit_path")
        if not isinstance(audit_raw, str):
            _raise("marker_mismatch", "compatible adoption has no audit_path")
        audit_path = Path(audit_raw)
        prefix = f".{output_name}.rebuild-build-adoption-"
        if (
            audit_path.parent != output_parent
            or not audit_path.name.startswith(prefix)
            or not audit_path.name.endswith(".json")
        ):
            _raise("marker_mismatch", "compatible adoption audit path is unsafe")
        audit, fingerprint = _read_json_at(parent_fd, audit_path.name)
        audit_digest = _sha256(_canonical_json_bytes(audit, compact=True))
        if audit_digest != compatible.get("audit_sha256"):
            _raise("marker_tampered", "compatible adoption audit digest changed")
        if (
            audit.get("version") != 1
            or audit.get("kind") != "compatible-partial-rebuild-build-adoption"
            or audit.get("cell_task") != cell_task
            or audit.get("new_build_fingerprint")
            != journal.get("build_fingerprint")
            or audit.get("raw_snapshot_sha256")
            != journal.get("expected_snapshot_sha256")
            or audit.get("durable_count") != proof.get("durable_count")
            or audit.get("durable_serials_sha256")
            != proof.get("durable_serials_sha256")
        ):
            _raise("marker_mismatch", "compatible adoption proof does not match output")
        previous = audit.get("previous_journal")
        if (
            not isinstance(previous, dict)
            or previous.get("cell_task") != cell_task
            or previous.get("output_root") != journal.get("output_root")
            or previous.get("archive_path") != journal.get("archive_path")
            or previous.get("rebuild_token") != journal.get("rebuild_token")
            or previous.get("expected_snapshot_sha256")
            != journal.get("expected_snapshot_sha256")
        ):
            _raise("marker_mismatch", "compatible adoption previous journal mismatch")
        return {
            "basename": audit_path.name,
            "fingerprint": fingerprint,
            "canonical_sha256": audit_digest,
        }

    def _inspection_locked(
        self,
        *,
        cell_task: str,
        parts: tuple[str, ...],
        parent_fd: int,
        raw_fd: int,
    ) -> dict[str, Any]:
        output_name = parts[-1]
        output_parent = self.lerobot_root.joinpath(*parts[:-1])
        output_path = output_parent / output_name
        markers = self._read_markers(
            parent_fd,
            cell_task=cell_task,
            output_path=output_path,
            output_name=output_name,
        )
        raw_serials = self._raw_serials(raw_fd, parts)
        contract_binding = self._contract_binding(
            cell_task=cell_task,
            raw_serials=raw_serials,
            raw_fd=raw_fd,
        )
        self._validate_marker_raw_bindings(markers, raw_serials)
        raw_fingerprint = _fingerprint_raw_task_at(raw_fd, parts)
        output_fingerprint = _fingerprint_directory_at(parent_fd, output_name)
        output_proof: dict[str, Any] | None = None
        if output_fingerprint is not None:
            output_proof = self._validation_proof(
                parent_fd=parent_fd,
                output_name=output_name,
                output_path=output_path,
                raw_serials=raw_serials,
                require_passed=False,
            )

        archive_name: str | None = None
        archive_proof: dict[str, Any] | None = None
        rebuild = markers["rebuild"]
        if rebuild is not None:
            archive_name = self._archive_basename(
                rebuild,
                output_name,
                output_parent,
            )
            if (
                archive_name is not None
                and _fingerprint_directory_at(parent_fd, archive_name) is not None
            ):
                archive_proof = self._validation_proof(
                    parent_fd=parent_fd,
                    output_name=archive_name,
                    output_path=output_parent / archive_name,
                    raw_serials=raw_serials,
                    require_passed=False,
                )

        recommendations: list[str] = []
        finalization = markers["finalization"]
        rebuild_phase = (
            rebuild["payload"]["phase"].strip().lower()
            if rebuild is not None
            else None
        )
        finalization_phase = (
            finalization["payload"]["phase"].strip().lower()
            if finalization is not None
            else None
        )
        output_status = (
            output_proof["validation"].get("status")
            if output_proof is not None
            else None
        )
        archive_status = (
            archive_proof["validation"].get("status")
            if archive_proof is not None
            else None
        )
        if rebuild_phase == "prepared" and archive_status == "passed":
            recommendations.append("rollback")
        if (
            finalization_phase == "armed"
            and rebuild is None
            and output_status == "passed"
        ):
            recommendations.append("adopt-finalization")
        if rebuild_phase == "verified" and output_status == "passed":
            recommendations.append("commit-verified")
        if rebuild_phase == "prepared" and output_status == "passed":
            compatible = self._compatible_adoption_proof(
                parent_fd=parent_fd,
                rebuild_marker=rebuild,
                proof=output_proof,
                output_parent=output_parent,
                output_name=output_name,
                cell_task=cell_task,
            )
            if compatible is not None:
                recommendations.insert(0, "commit-verified")
        if (
            output_proof is not None
            and output_status == "failed"
            and (
                contract_binding is None
                or output_proof.get("contract_validation") == "mismatch"
            )
        ):
            recommendations.append("quarantine-restart")

        return {
            "schema": RECOVERY_SCHEMA,
            "cell_task": cell_task,
            "output_path": str(output_path),
            "raw_count": len(raw_serials),
            "raw_serials_sha256": _serials_sha256(raw_serials),
            "raw_fingerprint": raw_fingerprint,
            "contract_manifest": contract_binding,
            "markers": {
                kind: (
                    {
                        "basename": marker["basename"],
                        "phase": marker["payload"]["phase"],
                        "fingerprint": marker["fingerprint"],
                    }
                    if marker is not None
                    else None
                )
                for kind, marker in markers.items()
            },
            "output": output_proof,
            "archive_path": (
                str(output_parent / archive_name)
                if archive_name is not None
                else None
            ),
            "archive": archive_proof,
            "recommended_modes": list(dict.fromkeys(recommendations)),
        }

    def inspect(self, cell_task: str) -> dict[str, Any]:
        parts, lerobot_fd, parent_fd, raw_fd = self._open_context(cell_task)
        try:
            fcntl.flock(lerobot_fd, fcntl.LOCK_SH)
            fcntl.flock(parent_fd, fcntl.LOCK_SH)
            return self._inspection_locked(
                cell_task=cell_task,
                parts=parts,
                parent_fd=parent_fd,
                raw_fd=raw_fd,
            )
        finally:
            os.close(raw_fd)
            os.close(parent_fd)
            os.close(lerobot_fd)

    def _existing_receipt(
        self,
        parent_fd: int,
        *,
        lerobot_fd: int,
        raw_fd: int,
        parts: tuple[str, ...],
        output_name: str,
        cell_task: str,
        mode: str,
    ) -> dict[str, Any] | None:
        names = _marker_names(output_name)
        if any(
            _stat_at(parent_fd, names[kind]) is not None
            for kind in ("finalization", "rebuild")
        ):
            return None
        prefix = f".{output_name}{_RECEIPT_TOKEN}"
        candidates = [
            name
            for name in sorted(os.listdir(parent_fd), reverse=True)
            if name.startswith(prefix) and name.endswith(".json")
        ]
        for name in candidates:
            receipt, fingerprint = _read_json_at(parent_fd, name)
            if (
                receipt.get("schema") == RECOVERY_SCHEMA
                and receipt.get("cell_task") == cell_task
                and receipt.get("mode") == mode
                and receipt.get("phase") == "receipt_durable"
            ):
                self._validate_intent_payload(
                    receipt,
                    intent_fingerprint=fingerprint,
                    cell_task=cell_task,
                    mode=mode,
                    output_name=output_name,
                    lerobot_fd=lerobot_fd,
                    parent_fd=parent_fd,
                    raw_fd=raw_fd,
                )
                if receipt["paths"]["receipt"] != name:
                    _raise(
                        "invalid_receipt",
                        "receipt filename does not match its durable intent",
                    )
                try:
                    raw_serials = self._verify_raw_snapshot(
                        raw_fd=raw_fd,
                        parts=parts,
                        intent=receipt,
                    )
                except RecoveryError as exc:
                    if exc.code == "raw_task_changed":
                        continue
                    raise
                self._authorize_marker_evidence(
                    parent_fd=parent_fd,
                    intent=receipt,
                    parts=parts,
                    raw_serials=raw_serials,
                )

                output_actual = _fingerprint_directory_at(parent_fd, output_name)
                quarantine_name = receipt["paths"]["quarantine"]
                quarantine_actual = (
                    _fingerprint_directory_at(parent_fd, quarantine_name)
                    if isinstance(quarantine_name, str)
                    else None
                )
                if mode == "quarantine-restart":
                    if (
                        output_actual is not None
                        or not _fingerprints_equal(
                            quarantine_actual,
                            receipt["inputs"]["output"],
                        )
                    ):
                        continue
                else:
                    expected_output = (
                        receipt["inputs"]["archive"]
                        if mode == "rollback"
                        else receipt["inputs"]["output"]
                    )
                    if not _fingerprints_equal(output_actual, expected_output):
                        continue
                    if mode == "rollback" and not _fingerprints_equal(
                        quarantine_actual,
                        receipt["inputs"]["output"],
                    ):
                        continue

                archive_name = receipt["paths"]["archive"]
                if isinstance(archive_name, str):
                    archive_actual = _fingerprint_directory_at(
                        parent_fd,
                        archive_name,
                    )
                    if mode == "rollback":
                        if archive_actual is not None:
                            continue
                    elif not _fingerprints_equal(
                        archive_actual,
                        receipt["inputs"]["archive"],
                    ):
                        continue

                try:
                    self._verify_state_artifacts(
                        lerobot_fd=lerobot_fd,
                        intent=receipt,
                    )
                except RecoveryError as exc:
                    if exc.code in {
                        "state_artifact_tampered",
                        "state_namespace_conflict",
                        "missing_state_backup",
                        "missing_state_replacement",
                    }:
                        continue
                    raise
                os.fsync(parent_fd)
                receipt = dict(receipt)
                receipt["receipt_path"] = name
                return receipt
        return None

    def _validate_mode_preconditions(
        self,
        *,
        mode: str,
        inspection: Mapping[str, Any],
        markers: Mapping[str, dict[str, Any] | None],
        parent_fd: int,
        output_parent: Path,
        output_name: str,
        cell_task: str,
    ) -> dict[str, Any] | None:
        output = inspection.get("output")
        archive = inspection.get("archive")
        finalization = markers.get("finalization")
        rebuild = markers.get("rebuild")
        output_status = (
            output.get("validation", {}).get("status")
            if isinstance(output, dict)
            else None
        )
        archive_status = (
            archive.get("validation", {}).get("status")
            if isinstance(archive, dict)
            else None
        )
        if mode == "rollback":
            if (
                rebuild is None
                or rebuild["payload"]["phase"].strip().lower() != "prepared"
            ):
                _raise("wrong_mode", "rollback requires a prepared rebuild journal")
            if output is None:
                _raise("missing_output", "rollback requires a current output to preserve")
            if archive_status != "passed":
                _raise(
                    "validation_not_passed",
                    "rollback archive requires fresh full validation 'passed'",
                )
            if archive.get("raw_subset") is not True:
                _raise(
                    "raw_subset_mismatch",
                    "rollback archive serials are not a subset of raw serials",
                )
            return None

        if mode == "adopt-finalization":
            if rebuild is not None:
                _raise(
                    "wrong_mode",
                    "finalization adoption is forbidden when a rebuild journal exists",
                )
            if (
                finalization is None
                or finalization["payload"]["phase"].strip().lower() != "armed"
            ):
                _raise(
                    "wrong_mode",
                    "adopt-finalization requires an armed finalization marker",
                )
            if output_status != "passed":
                _raise(
                    "validation_not_passed",
                    "finalization adoption requires fresh full validation 'passed'",
                )
            if output.get("raw_subset") is not True:
                _raise(
                    "raw_subset_mismatch",
                    "finalization output serials are not a subset of raw serials",
                )
            return None

        if mode == "quarantine-restart":
            if output is None:
                _raise("missing_output", "quarantine-restart requires an output")
            if output_status != "failed":
                _raise(
                    "validation_not_failed",
                    "quarantine-restart requires a freshly failed full validation",
                )
            if (
                inspection.get("contract_manifest") is not None
                and output.get("contract_validation") != "mismatch"
            ):
                _raise(
                    "contract_mismatch_not_proven",
                    "authorized quarantine-restart requires a stable contract mismatch",
                )
            return None

        if mode == "commit-verified":
            if rebuild is None:
                _raise("wrong_mode", "commit-verified requires a rebuild journal")
            phase = rebuild["payload"]["phase"].strip().lower()
            if output_status != "passed":
                _raise(
                    "validation_not_passed",
                    "commit-verified requires fresh full validation 'passed'",
                )
            if output.get("raw_subset") is not True:
                _raise(
                    "raw_subset_mismatch",
                    "verified output serials are not a subset of raw serials",
                )
            compatible = None
            if phase == "prepared":
                compatible = self._compatible_adoption_proof(
                    parent_fd=parent_fd,
                    rebuild_marker=rebuild,
                    proof=output,
                    output_parent=output_parent,
                    output_name=output_name,
                    cell_task=cell_task,
                )
                if compatible is None:
                    _raise(
                        "wrong_mode",
                        "prepared journal has no verifiable compatible adoption",
                    )
            elif phase != "verified":
                _raise(
                    "wrong_mode",
                    "commit-verified requires verified or compatible prepared journal",
                )
            return compatible
        _raise("invalid_mode", f"unsupported recovery mode: {mode}")

    def _create_intent(
        self,
        *,
        mode: str,
        cell_task: str,
        parts: tuple[str, ...],
        lerobot_fd: int,
        parent_fd: int,
        raw_fd: int,
        inspection: Mapping[str, Any],
    ) -> dict[str, Any]:
        output_name = parts[-1]
        output_parent = self.lerobot_root.joinpath(*parts[:-1])
        output_path = output_parent / output_name
        markers = self._read_markers(
            parent_fd,
            cell_task=cell_task,
            output_path=output_path,
            output_name=output_name,
        )
        raw_serials = self._raw_serials(raw_fd, parts)
        contract_binding = self._contract_binding(
            cell_task=cell_task,
            raw_serials=raw_serials,
            raw_fd=raw_fd,
        )
        self._validate_marker_raw_bindings(markers, raw_serials)
        compatible = self._validate_mode_preconditions(
            mode=mode,
            inspection=inspection,
            markers=markers,
            parent_fd=parent_fd,
            output_parent=output_parent,
            output_name=output_name,
            cell_task=cell_task,
        )
        raw_fingerprint = _fingerprint_raw_task_at(raw_fd, parts)
        if (
            inspection.get("raw_serials_sha256")
            != _serials_sha256(raw_serials)
            or inspection.get("contract_manifest") != contract_binding
            or not _fingerprints_equal(
                raw_fingerprint,
                inspection.get("raw_fingerprint"),
            )
        ):
            _raise("raw_task_changed", "raw task changed after inspection")

        intent_id = uuid.uuid4().hex
        marker_entries: list[dict[str, Any]] = []
        terminal_status = _TERMINAL_STATUS[mode]
        for kind in ("finalization", "rebuild"):
            marker = markers[kind]
            if marker is None:
                continue
            source = marker["basename"]
            audit = (
                f"{source[:-5]}.recovery-{terminal_status}-{intent_id}.json"
            )
            marker_entries.append(
                {
                    "kind": kind,
                    "source": source,
                    "audit": audit,
                    "fingerprint": marker["fingerprint"],
                }
            )

        archive_name: str | None = None
        if markers["rebuild"] is not None:
            archive_name = self._archive_basename(
                markers["rebuild"],
                output_name,
                output_parent,
            )

        output_proof = inspection.get("output")
        archive_proof = inspection.get("archive")
        quarantine_name = (
            f".{output_name}.recovery-quarantine-{intent_id}"
            if mode in {"rollback", "quarantine-restart"}
            else None
        )
        first_phase = (
            "output_quarantine_pending"
            if quarantine_name is not None
            else "candidate_verify_pending"
        )
        created_at = _utc_now()
        intent = {
            "schema": RECOVERY_SCHEMA,
            "intent_id": intent_id,
            "cell_task": cell_task,
            "mode": mode,
            "phase": first_phase,
            "created_at": created_at,
            "terminal_status": terminal_status,
            "roots": {
                "raw": _identity_dict(os.fstat(raw_fd)),
                "lerobot": _identity_dict(os.fstat(lerobot_fd)),
                "task_parent": _identity_dict(os.fstat(parent_fd)),
            },
            "paths": {
                "output": output_name,
                "archive": archive_name,
                "quarantine": quarantine_name,
                "state": self.state_file.name,
                "state_backup": (
                    f".convert_state.recovery-backup-{intent_id}.json"
                ),
                "state_replacement": (
                    f".convert_state.recovery-replacement-{intent_id}.json"
                ),
                "receipt": (
                    f".{output_name}{_RECEIPT_TOKEN}{intent_id}.json"
                ),
            },
            "inputs": {
                "output": (
                    output_proof.get("tree")
                    if isinstance(output_proof, dict)
                    else None
                ),
                "archive": (
                    archive_proof.get("tree")
                    if isinstance(archive_proof, dict)
                    else None
                ),
                "markers": marker_entries,
                "compatible_adoption": compatible,
            },
            "raw": {
                "count": len(raw_serials),
                "serials_sha256": _serials_sha256(raw_serials),
                "fingerprint": raw_fingerprint,
            },
            "contract_manifest": contract_binding,
            "validation": (
                archive_proof
                if mode == "rollback"
                else output_proof
                if mode in {"adopt-finalization", "commit-verified"}
                else {
                    "validation": output_proof.get("validation"),
                    "tree": output_proof.get("tree"),
                    "durable_serials": [],
                    "durable_count": 0,
                    "durable_serials_sha256": _serials_sha256([]),
                    "raw_count": len(raw_serials),
                    "raw_serials_sha256": _serials_sha256(raw_serials),
                    "raw_subset": True,
                    "duplicate_count": 0,
                }
            ),
            "state": None,
            "receipts": [],
        }
        return intent

    def _validate_intent_payload(
        self,
        intent: Mapping[str, Any],
        *,
        intent_fingerprint: Mapping[str, Any],
        cell_task: str,
        mode: str,
        output_name: str,
        lerobot_fd: int,
        parent_fd: int,
        raw_fd: int,
    ) -> None:
        """Validate every executable intent field before replaying a syscall."""
        if (
            intent.get("schema") != RECOVERY_SCHEMA
            or intent.get("cell_task") != cell_task
            or intent.get("mode") != mode
        ):
            _raise("intent_conflict", "active recovery intent does not match request")
        intent_owner = intent.get("intent_owner")
        intent_file = intent.get("intent_file")
        if (
            not isinstance(intent_owner, Mapping)
            or isinstance(intent_owner.get("uid"), bool)
            or not isinstance(intent_owner.get("uid"), int)
            or intent_owner.get("uid") < 0
            or isinstance(intent_owner.get("gid"), bool)
            or not isinstance(intent_owner.get("gid"), int)
            or intent_owner.get("gid") < 0
            or not isinstance(intent_file, Mapping)
            or isinstance(intent_file.get("dev"), bool)
            or not isinstance(intent_file.get("dev"), int)
            or intent_file.get("dev") < 0
            or isinstance(intent_file.get("ino"), bool)
            or not isinstance(intent_file.get("ino"), int)
            or intent_file.get("ino") < 0
            or intent_fingerprint.get("mode") != 0o600
            or intent_fingerprint.get("nlink") != 1
            or intent_fingerprint.get("uid") != intent_owner.get("uid")
            or intent_fingerprint.get("gid") != intent_owner.get("gid")
            or intent_fingerprint.get("dev") != intent_file.get("dev")
            or intent_fingerprint.get("ino") != intent_file.get("ino")
        ):
            _raise(
                "invalid_intent",
                "active recovery intent does not match its private owner binding",
            )

        intent_id = intent.get("intent_id")
        if not isinstance(intent_id, str) or _INTENT_ID_RE.fullmatch(intent_id) is None:
            _raise("invalid_intent", "intent_id must be 32 lowercase hex characters")
        phase = intent.get("phase")
        if phase not in _RECOVERY_PHASES:
            _raise("invalid_intent", f"unsupported recovery phase: {phase!r}")
        expected_terminal = _TERMINAL_STATUS[mode]
        if intent.get("terminal_status") != expected_terminal:
            _raise("invalid_intent", "terminal_status does not match recovery mode")
        if not isinstance(intent.get("created_at"), str):
            _raise("invalid_intent", "intent has no deterministic created_at")

        phases_by_mode = {
            "rollback": _RECOVERY_PHASES,
            "quarantine-restart": _RECOVERY_PHASES
            - {"archive_restore_pending", "candidate_verify_pending"},
            "adopt-finalization": _RECOVERY_PHASES
            - {"output_quarantine_pending", "archive_restore_pending"},
            "commit-verified": _RECOVERY_PHASES
            - {"output_quarantine_pending", "archive_restore_pending"},
        }
        if phase not in phases_by_mode[mode]:
            _raise("invalid_intent", f"phase {phase!r} is impossible for {mode}")

        roots = intent.get("roots")
        if not isinstance(roots, Mapping):
            _raise("invalid_intent", "intent roots must be an object")
        current_roots = {
            "raw": os.fstat(raw_fd),
            "lerobot": os.fstat(lerobot_fd),
            "task_parent": os.fstat(parent_fd),
        }
        for root_name, current in current_roots.items():
            expected = _require_identity_payload(
                roots.get(root_name),
                label=f"roots.{root_name}",
            )
            if not _same_identity(current, expected):
                _raise(
                    "root_identity_changed",
                    f"{root_name} root identity changed after intent creation",
                )

        paths = intent.get("paths")
        if not isinstance(paths, Mapping):
            _raise("invalid_intent", "intent paths must be an object")
        expected_paths = {
            "output": output_name,
            "state": self.state_file.name,
            "state_backup": f".convert_state.recovery-backup-{intent_id}.json",
            "state_replacement": (
                f".convert_state.recovery-replacement-{intent_id}.json"
            ),
            "receipt": f".{output_name}{_RECEIPT_TOKEN}{intent_id}.json",
            "quarantine": (
                f".{output_name}.recovery-quarantine-{intent_id}"
                if mode in {"rollback", "quarantine-restart"}
                else None
            ),
        }
        for key, expected in expected_paths.items():
            actual = paths.get(key)
            if actual != expected:
                _raise("invalid_intent", f"paths.{key} does not match its derivation")
            if isinstance(actual, str):
                _require_plain_basename(actual, label=f"paths.{key}")

        archive = paths.get("archive")
        if archive is not None:
            archive = _require_plain_basename(archive, label="paths.archive")
            if not archive.startswith(f".{output_name}.rebuild-output-"):
                _raise("invalid_intent", "paths.archive has an invalid prefix")

        inputs = intent.get("inputs")
        if not isinstance(inputs, Mapping):
            _raise("invalid_intent", "intent inputs must be an object")
        output_fingerprint = _require_fingerprint_payload(
            inputs.get("output"),
            label="inputs.output",
            kind="directory",
        )
        archive_fingerprint = inputs.get("archive")
        if archive is None:
            if archive_fingerprint is not None:
                _raise("invalid_intent", "archive fingerprint has no archive path")
        else:
            archive_fingerprint = _require_fingerprint_payload(
                archive_fingerprint,
                label="inputs.archive",
                kind="directory",
            )
        if mode == "rollback" and archive_fingerprint is None:
            _raise("invalid_intent", "rollback intent requires an archive fingerprint")

        markers = inputs.get("markers")
        if not isinstance(markers, list) or len(markers) > 2:
            _raise("invalid_intent", "intent markers must be a list of at most two")
        canonical_markers = _marker_names(output_name)
        seen_kinds: set[str] = set()
        for index, marker in enumerate(markers):
            if not isinstance(marker, Mapping):
                _raise("invalid_intent", f"inputs.markers[{index}] is not an object")
            kind = marker.get("kind")
            if kind not in {"finalization", "rebuild"} or kind in seen_kinds:
                _raise("invalid_intent", "intent marker kinds are invalid or duplicate")
            seen_kinds.add(kind)
            source = canonical_markers[kind]
            audit = f"{source[:-5]}.recovery-{expected_terminal}-{intent_id}.json"
            if marker.get("source") != source or marker.get("audit") != audit:
                _raise("invalid_intent", f"{kind} marker path was not derived safely")
            expected_fingerprint = _require_fingerprint_payload(
                marker.get("fingerprint"),
                label=f"inputs.markers[{index}].fingerprint",
                kind="regular",
            )
            source_info = _stat_at(parent_fd, source)
            audit_info = _stat_at(parent_fd, audit)
            if (source_info is None) == (audit_info is None):
                _raise(
                    "marker_evidence_conflict",
                    f"exactly one of {source!r} and {audit!r} must exist",
                )
            evidence_name = source if source_info is not None else audit
            _, actual_fingerprint = _read_regular_bytes_at(
                parent_fd,
                evidence_name,
            )
            if not _fingerprints_equal(actual_fingerprint, expected_fingerprint):
                _raise(
                    "marker_tampered",
                    f"recovery marker changed after intent: {evidence_name}",
                )

        required_markers = {
            "rollback": {"rebuild"},
            "adopt-finalization": {"finalization"},
            "commit-verified": {"rebuild"},
        }
        if not required_markers.get(mode, set()).issubset(seen_kinds):
            _raise("invalid_intent", f"{mode} intent lacks its required marker")
        if mode == "adopt-finalization" and "rebuild" in seen_kinds:
            _raise("invalid_intent", "adopt-finalization cannot include rebuild")
        if archive is not None and "rebuild" not in seen_kinds:
            _raise(
                "invalid_intent",
                "archive path requires a rebuild marker",
            )

        compatible = inputs.get("compatible_adoption")
        if compatible is not None:
            if mode != "commit-verified" or not isinstance(compatible, Mapping):
                _raise("invalid_intent", "compatible adoption is not applicable")
            basename = _require_plain_basename(
                compatible.get("basename"),
                label="inputs.compatible_adoption.basename",
            )
            prefix = f".{output_name}.rebuild-build-adoption-"
            if not basename.startswith(prefix) or not basename.endswith(".json"):
                _raise("invalid_intent", "compatible adoption basename is invalid")
            expected_fingerprint = _require_fingerprint_payload(
                compatible.get("fingerprint"),
                label="inputs.compatible_adoption.fingerprint",
                kind="regular",
            )
            expected_digest = _require_sha256(
                compatible.get("canonical_sha256"),
                label="inputs.compatible_adoption.canonical_sha256",
            )
            audit, actual_fingerprint = _read_json_at(parent_fd, basename)
            if (
                not _fingerprints_equal(actual_fingerprint, expected_fingerprint)
                or _sha256(_canonical_json_bytes(audit, compact=True))
                != expected_digest
            ):
                _raise(
                    "marker_tampered",
                    "compatible adoption evidence changed after intent",
                )

        raw = intent.get("raw")
        if not isinstance(raw, Mapping):
            _raise("invalid_intent", "intent raw snapshot must be an object")
        _require_int(raw.get("count"), label="raw.count")
        _require_sha256(raw.get("serials_sha256"), label="raw.serials_sha256")
        _require_fingerprint_payload(
            raw.get("fingerprint"),
            label="raw.fingerprint",
            kind="directory",
        )
        current_raw_serials = self._raw_serials(
            raw_fd,
            _parse_cell_task(cell_task),
        )
        current_contract_binding = self._contract_binding(
            cell_task=cell_task,
            raw_serials=current_raw_serials,
            raw_fd=raw_fd,
        )
        if intent.get("contract_manifest") != current_contract_binding:
            _raise(
                "intent_conflict",
                "recovery intent contract manifest authorization does not match",
            )

        validation = intent.get("validation")
        if not isinstance(validation, Mapping):
            _raise("invalid_intent", "intent validation proof must be an object")
        result = validation.get("validation")
        expected_status = "failed" if mode == "quarantine-restart" else "passed"
        if not isinstance(result, Mapping) or result.get("status") != expected_status:
            _raise(
                "invalid_intent",
                f"{mode} intent requires validation status {expected_status!r}",
            )
        contract_manifest = intent.get("contract_manifest")
        contract_validation = validation.get("contract_validation")
        if contract_manifest is None:
            if contract_validation is not None:
                _raise(
                    "invalid_intent",
                    "manifest-free intent cannot contain contract validation proof",
                )
        elif contract_validation != (
            "mismatch" if mode == "quarantine-restart" else "passed"
        ):
            _raise(
                "invalid_intent",
                "intent contract validation proof does not match recovery mode",
            )
        validation_tree = _require_fingerprint_payload(
            validation.get("tree"),
            label="validation.tree",
            kind="directory",
        )
        expected_tree = (
            archive_fingerprint if mode == "rollback" else output_fingerprint
        )
        if not _fingerprints_equal(validation_tree, expected_tree):
            _raise("invalid_intent", "validation tree does not match intent input")
        durable_serials = validation.get("durable_serials")
        if not isinstance(durable_serials, list) or any(
            not isinstance(serial, str)
            or _SERIAL_RE.fullmatch(serial) is None
            for serial in durable_serials
        ):
            _raise("invalid_intent", "validation durable serials are invalid")
        if len(durable_serials) != len(set(durable_serials)):
            _raise("invalid_intent", "validation durable serials are duplicated")
        if (
            validation.get("durable_count") != len(durable_serials)
            or validation.get("durable_serials_sha256")
            != _serials_sha256(durable_serials)
            or validation.get("raw_count") != raw.get("count")
            or validation.get("raw_serials_sha256") != raw.get("serials_sha256")
            or validation.get("raw_subset") is not True
            or validation.get("duplicate_count") != 0
        ):
            _raise("invalid_intent", "validation serial invariants do not match")
        if mode == "quarantine-restart" and durable_serials:
            _raise("invalid_intent", "quarantine-restart must reconcile zero serials")

        state_plan = intent.get("state")
        if state_plan is None:
            if phase not in {"state_replacement_pending"} | (
                {"output_quarantine_pending", "archive_restore_pending",
                 "candidate_verify_pending"}
            ):
                _raise("invalid_intent", "recovery phase requires a durable state plan")
        else:
            if not isinstance(state_plan, Mapping):
                _raise("invalid_intent", "intent state plan must be an object")
            for key in ("original_fingerprint", "replacement_fingerprint"):
                _require_fingerprint_payload(
                    state_plan.get(key),
                    label=f"state.{key}",
                    kind="regular",
                )
            for key in (
                "original_sha256",
                "replacement_sha256",
                "target_entry_before_sha256",
                "target_entry_after_sha256",
                "non_target_before_sha256",
                "non_target_after_sha256",
                "durable_serials_sha256",
            ):
                _require_sha256(state_plan.get(key), label=f"state.{key}")
            if (
                state_plan.get("durable_count") != len(durable_serials)
                or state_plan.get("durable_serials_sha256")
                != _serials_sha256(durable_serials)
                or state_plan.get("non_target_before_sha256")
                != state_plan.get("non_target_after_sha256")
            ):
                _raise("invalid_intent", "state plan invariants do not match")

        receipts = intent.get("receipts")
        if not isinstance(receipts, list) or any(
            not isinstance(receipt, Mapping) for receipt in receipts
        ):
            _raise("invalid_intent", "intent receipts must be a list of objects")

    def _load_intent(
        self,
        parent_fd: int,
        intent_name: str,
        *,
        cell_task: str,
        mode: str,
        output_name: str,
        lerobot_fd: int,
        raw_fd: int,
    ) -> dict[str, Any]:
        intent, fingerprint = _read_json_at(parent_fd, intent_name)
        self._validate_intent_payload(
            intent,
            intent_fingerprint=fingerprint,
            cell_task=cell_task,
            mode=mode,
            output_name=output_name,
            lerobot_fd=lerobot_fd,
            parent_fd=parent_fd,
            raw_fd=raw_fd,
        )
        return intent

    def _write_intent(
        self,
        parent_fd: int,
        intent_name: str,
        intent: dict[str, Any],
        *,
        replace: bool,
    ) -> None:
        self._hook("before_intent_write")
        _atomic_write_json(
            parent_fd,
            intent_name,
            intent,
            replace=replace,
            bind_created_owner=True,
        )
        self._hook("after_intent_write")

    def _authorize_marker_evidence(
        self,
        *,
        parent_fd: int,
        intent: Mapping[str, Any],
        parts: tuple[str, ...],
        raw_serials: list[str],
    ) -> None:
        """Re-derive recovery authorization from the marker payloads on disk."""
        output_name = parts[-1]
        output_parent = self.lerobot_root.joinpath(*parts[:-1])
        output_path = output_parent / output_name
        markers: dict[str, dict[str, Any] | None] = {
            "finalization": None,
            "rebuild": None,
        }
        for marker_plan in intent["inputs"]["markers"]:
            source = marker_plan["source"]
            audit = marker_plan["audit"]
            source_exists = _stat_at(parent_fd, source) is not None
            audit_exists = _stat_at(parent_fd, audit) is not None
            if source_exists == audit_exists:
                _raise(
                    "marker_evidence_conflict",
                    f"exactly one of {source!r} and {audit!r} must exist",
                )
            evidence_name = source if source_exists else audit
            marker = self._read_marker(
                parent_fd,
                kind=marker_plan["kind"],
                basename=evidence_name,
                cell_task=intent["cell_task"],
                output_path=output_path,
            )
            if marker is None:
                _raise("missing_marker", f"marker evidence is missing: {evidence_name}")
            if not _fingerprints_equal(
                marker["fingerprint"],
                marker_plan["fingerprint"],
            ):
                _raise(
                    "marker_tampered",
                    f"recovery marker changed after intent: {evidence_name}",
                )
            markers[marker_plan["kind"]] = marker

        finalization = markers["finalization"]
        rebuild = markers["rebuild"]
        self._validate_paired_markers(finalization, rebuild)
        self._validate_marker_raw_bindings(markers, raw_serials)
        actual_archive = (
            self._archive_basename(rebuild, output_name, output_parent)
            if rebuild is not None
            else None
        )
        if actual_archive != intent["paths"]["archive"]:
            _raise(
                "marker_mismatch",
                "rebuild marker archive_path does not match recovery intent",
            )

        mode = intent["mode"]
        if mode == "rollback":
            if (
                rebuild is None
                or rebuild["payload"]["phase"].strip().lower() != "prepared"
            ):
                _raise("wrong_mode", "rollback requires a prepared rebuild journal")
        elif mode == "adopt-finalization":
            if (
                rebuild is not None
                or finalization is None
                or finalization["payload"]["phase"].strip().lower() != "armed"
            ):
                _raise(
                    "wrong_mode",
                    "adopt-finalization requires only an armed finalization marker",
                )
        elif mode == "commit-verified":
            if rebuild is None:
                _raise("wrong_mode", "commit-verified requires a rebuild journal")
            phase = rebuild["payload"]["phase"].strip().lower()
            if phase == "prepared":
                compatible = self._compatible_adoption_proof(
                    parent_fd=parent_fd,
                    rebuild_marker=rebuild,
                    proof=intent["validation"],
                    output_parent=output_parent,
                    output_name=output_name,
                    cell_task=intent["cell_task"],
                )
                if (
                    compatible is None
                    or compatible != intent["inputs"]["compatible_adoption"]
                ):
                    _raise(
                        "marker_mismatch",
                        "prepared rebuild adoption proof does not match recovery intent",
                    )
            elif phase != "verified":
                _raise(
                    "wrong_mode",
                    "commit-verified requires verified or compatible prepared journal",
                )

    def _verify_raw_snapshot(
        self,
        *,
        raw_fd: int,
        parts: tuple[str, ...],
        intent: Mapping[str, Any],
    ) -> list[str]:
        raw_serials = self._raw_serials(raw_fd, parts)
        raw_fingerprint = _fingerprint_raw_task_at(raw_fd, parts)
        if (
            len(raw_serials) != intent["raw"]["count"]
            or _serials_sha256(raw_serials) != intent["raw"]["serials_sha256"]
            or not _fingerprints_equal(
                raw_fingerprint,
                intent["raw"]["fingerprint"],
            )
        ):
            _raise("raw_task_changed", "raw task changed during recovery")
        if intent.get("contract_manifest") != self._contract_binding(
            cell_task=intent["cell_task"],
            raw_serials=raw_serials,
            raw_fd=raw_fd,
        ):
            _raise(
                "manifest_tampered",
                "contract manifest authorization changed during recovery",
            )
        return raw_serials

    def _preflight_output_quarantine(
        self,
        *,
        parent_fd: int,
        intent: Mapping[str, Any],
        parts: tuple[str, ...],
        raw_serials: list[str],
    ) -> None:
        """Prove every first-move dependency before preserving current output."""
        output_name = intent["paths"]["output"]
        quarantine_name = intent["paths"]["quarantine"]
        if not isinstance(quarantine_name, str):
            _raise("invalid_intent", "output quarantine phase has no quarantine path")
        output_exists = _stat_at(parent_fd, output_name) is not None
        quarantine_exists = _stat_at(parent_fd, quarantine_name) is not None
        if output_exists == quarantine_exists:
            _raise(
                "ambiguous_namespace",
                "current output must exist at exactly one recovery location",
            )
        candidate_name = output_name if output_exists else quarantine_name
        candidate_fp = _fingerprint_directory_at(parent_fd, candidate_name)
        if not _fingerprints_equal(candidate_fp, intent["inputs"]["output"]):
            _raise(
                "fingerprint_mismatch",
                "current output changed after recovery intent",
            )

        output_parent = self.lerobot_root.joinpath(*parts[:-1])
        if intent["mode"] == "rollback":
            archive_name = intent["paths"]["archive"]
            if not isinstance(archive_name, str):
                _raise("invalid_intent", "rollback intent has no archive")
            archive_proof = self._validation_proof(
                parent_fd=parent_fd,
                output_name=archive_name,
                output_path=output_parent / archive_name,
                raw_serials=raw_serials,
                require_passed=True,
            )
            expected = intent["validation"]
            if (
                not _fingerprints_equal(
                    archive_proof["tree"],
                    intent["inputs"]["archive"],
                )
                or archive_proof["durable_count"] != expected["durable_count"]
                or archive_proof["durable_serials_sha256"]
                != expected["durable_serials_sha256"]
            ):
                _raise(
                    "candidate_changed",
                    "rollback archive changed after recovery intent",
                )
            return

        proof = self._validation_proof(
            parent_fd=parent_fd,
            output_name=candidate_name,
            output_path=output_parent / candidate_name,
            raw_serials=raw_serials,
            require_passed=False,
        )
        if (
            proof["validation"].get("status") != "failed"
            or (
                intent.get("contract_manifest") is not None
                and proof.get("contract_validation") != "mismatch"
            )
            or not _fingerprints_equal(proof["tree"], intent["inputs"]["output"])
        ):
            _raise(
                "validation_not_failed",
                "quarantine-restart requires a freshly failed current output",
            )

    def _verify_regular_artifact(
        self,
        parent_fd: int,
        name: str,
        *,
        expected_fingerprint: Mapping[str, Any],
        expected_sha256: str,
        label: str,
    ) -> bool:
        if _stat_at(parent_fd, name) is None:
            return False
        payload, actual = _read_regular_bytes_at(parent_fd, name)
        if (
            not _fingerprints_equal(actual, expected_fingerprint)
            or _sha256(payload) != expected_sha256
        ):
            _raise("state_artifact_tampered", f"{label} changed after intent")
        return True

    def _verify_state_artifacts(
        self,
        *,
        lerobot_fd: int,
        intent: Mapping[str, Any],
    ) -> None:
        """Prove state replacement/install artifacts before any next mutation."""
        state_plan = intent.get("state")
        if state_plan is None:
            return
        phase = intent["phase"]
        paths = intent["paths"]
        canonical_exists = _stat_at(lerobot_fd, paths["state"]) is not None
        backup_exists = _stat_at(lerobot_fd, paths["state_backup"]) is not None
        replacement_exists = (
            _stat_at(lerobot_fd, paths["state_replacement"]) is not None
        )

        canonical_is_original = False
        canonical_is_replacement = False
        if canonical_exists:
            try:
                canonical_is_original = self._verify_regular_artifact(
                    lerobot_fd,
                    paths["state"],
                    expected_fingerprint=state_plan["original_fingerprint"],
                    expected_sha256=state_plan["original_sha256"],
                    label="canonical original state",
                )
            except RecoveryError as original_error:
                try:
                    canonical_is_replacement = self._verify_regular_artifact(
                        lerobot_fd,
                        paths["state"],
                        expected_fingerprint=state_plan["replacement_fingerprint"],
                        expected_sha256=state_plan["replacement_sha256"],
                        label="canonical replacement state",
                    )
                except RecoveryError:
                    raise original_error
        if backup_exists:
            self._verify_regular_artifact(
                lerobot_fd,
                paths["state_backup"],
                expected_fingerprint=state_plan["original_fingerprint"],
                expected_sha256=state_plan["original_sha256"],
                label="state backup",
            )
        if replacement_exists:
            self._verify_regular_artifact(
                lerobot_fd,
                paths["state_replacement"],
                expected_fingerprint=state_plan["replacement_fingerprint"],
                expected_sha256=state_plan["replacement_sha256"],
                label="state replacement",
            )

        if (
            phase in {"state_replacement_pending", "state_backup_pending"}
            and not replacement_exists
        ):
            _raise(
                "missing_state_replacement",
                "state replacement is missing before canonical state backup",
            )

        original_name = (
            paths["state_backup"]
            if backup_exists
            else paths["state"]
            if canonical_is_original
            else None
        )
        replacement_name = (
            paths["state_replacement"]
            if replacement_exists
            else paths["state"]
            if canonical_is_replacement
            else None
        )
        if original_name is None or replacement_name is None:
            _raise(
                "state_namespace_conflict",
                "state plan has no complete original/replacement evidence",
            )
        original_bytes, original_fp = _read_regular_bytes_at(
            lerobot_fd,
            original_name,
        )
        replacement_bytes, replacement_fp = _read_regular_bytes_at(
            lerobot_fd,
            replacement_name,
        )
        original = self._decode_state(
            original_bytes,
            label="original convert_state.json",
        )
        durable_serials = list(intent["validation"]["durable_serials"])
        if intent["mode"] == "quarantine-restart":
            durable_serials = []
        derived = self._derive_state_replacement(
            original=original,
            intent=intent,
            durable_serials=durable_serials,
        )
        expected_plan = {
            "original_sha256": _sha256(original_bytes),
            "replacement_sha256": _sha256(derived["replacement_bytes"]),
            "target_entry_before_sha256": derived[
                "target_entry_before_sha256"
            ],
            "target_entry_after_sha256": derived["target_entry_after_sha256"],
            "non_target_before_sha256": derived["non_target_before_sha256"],
            "non_target_after_sha256": derived["non_target_after_sha256"],
            "durable_count": derived["durable_count"],
            "durable_serials_sha256": derived["durable_serials_sha256"],
        }
        if replacement_bytes != derived["replacement_bytes"] or any(
            state_plan.get(key) != value
            for key, value in expected_plan.items()
        ):
            _raise(
                "state_scope_violation",
                "state replacement was not derived from the preserved original",
            )
        if any(
            original_fp[key] != replacement_fp[key]
            for key in ("mode", "uid", "gid")
        ):
            _raise(
                "state_metadata_changed",
                "state replacement does not preserve mode, uid, and gid",
            )

        if phase in {"state_replacement_pending", "state_backup_pending"}:
            if not replacement_exists:
                _raise(
                    "missing_state_replacement",
                    "state replacement is missing before canonical state backup",
                )
            if (canonical_is_original, backup_exists) not in {
                (True, False),
                (False, True),
            }:
                _raise(
                    "state_namespace_conflict",
                    "original state must exist at exactly one expected location",
                )
            return

        if phase == "state_install_pending":
            if not backup_exists:
                _raise("missing_state_backup", "state backup is not durable")
            if (replacement_exists, canonical_is_replacement) not in {
                (True, False),
                (False, True),
            }:
                _raise(
                    "state_namespace_conflict",
                    "replacement state must exist at exactly one expected location",
                )
            if canonical_is_original:
                _raise(
                    "state_namespace_conflict",
                    "canonical original state reappeared after backup",
                )
            return

        if phase in {"marker_audit_pending", "receipt_pending", "receipt_durable"}:
            if (
                not backup_exists
                or not canonical_is_replacement
                or replacement_exists
            ):
                _raise(
                    "state_namespace_conflict",
                    "installed state does not match the durable recovery plan",
                )

    def _advance(
        self,
        parent_fd: int,
        intent_name: str,
        intent: dict[str, Any],
        phase: str,
        *,
        receipt: Mapping[str, Any] | None = None,
    ) -> None:
        if receipt is not None:
            intent.setdefault("receipts", []).append(dict(receipt))
        intent["phase"] = phase
        intent["updated_at"] = _utc_now()
        self._write_intent(parent_fd, intent_name, intent, replace=True)

    def _resolve_directory_move(
        self,
        parent_fd: int,
        *,
        source: str,
        destination: str,
        expected: Mapping[str, Any],
        action: str,
    ) -> dict[str, Any]:
        source_fp = _fingerprint_directory_at(parent_fd, source)
        destination_fp = _fingerprint_directory_at(parent_fd, destination)
        if source_fp is not None and destination_fp is not None:
            _raise(
                "ambiguous_namespace",
                f"both {source!r} and {destination!r} exist",
            )
        if source_fp is None and destination_fp is None:
            _raise(
                "missing_recovery_object",
                f"neither {source!r} nor {destination!r} exists",
            )
        if source_fp is not None:
            if not _fingerprints_equal(source_fp, expected):
                _raise("fingerprint_mismatch", f"{source!r} fingerprint changed")
            self._hook(f"before_{action}_rename")
            try:
                _rename_noreplace(
                    parent_fd,
                    source,
                    parent_fd,
                    destination,
                )
            except OSError as exc:
                if exc.errno == errno.EEXIST:
                    _raise(
                        "foreign_destination",
                        f"destination appeared during {action}: {destination}",
                    )
                raise
            self._hook(f"after_{action}_rename")
            destination_fp = _fingerprint_directory_at(parent_fd, destination)
        if not _fingerprints_equal(destination_fp, expected):
            _raise(
                "fingerprint_mismatch",
                f"{destination!r} does not contain the expected object",
            )
        if _stat_at(parent_fd, source) is not None:
            _raise("ambiguous_namespace", f"{source!r} reappeared after {action}")
        os.fsync(parent_fd)
        self._hook(f"after_{action}_fsync")
        return {
            "action": action,
            "source": source,
            "destination": destination,
            "fingerprint": destination_fp,
            "parent_fsynced": True,
        }

    def _resolve_regular_move(
        self,
        parent_fd: int,
        *,
        source: str,
        destination: str,
        expected: Mapping[str, Any],
        action: str,
    ) -> dict[str, Any]:
        source_info = _stat_at(parent_fd, source)
        destination_info = _stat_at(parent_fd, destination)
        if source_info is not None and destination_info is not None:
            _raise(
                "ambiguous_namespace",
                f"both {source!r} and {destination!r} exist",
            )
        if source_info is None and destination_info is None:
            _raise(
                "missing_recovery_object",
                f"neither {source!r} nor {destination!r} exists",
            )
        if source_info is not None:
            _, source_fp = _read_regular_bytes_at(parent_fd, source)
            if not _fingerprints_equal(source_fp, expected):
                _raise("fingerprint_mismatch", f"{source!r} fingerprint changed")
            self._hook(f"before_{action}_rename")
            try:
                _rename_noreplace(
                    parent_fd,
                    source,
                    parent_fd,
                    destination,
                )
            except OSError as exc:
                if exc.errno == errno.EEXIST:
                    _raise(
                        "foreign_destination",
                        f"destination appeared during {action}: {destination}",
                    )
                raise
            self._hook(f"after_{action}_rename")
        _, destination_fp = _read_regular_bytes_at(parent_fd, destination)
        if not _fingerprints_equal(destination_fp, expected):
            _raise(
                "fingerprint_mismatch",
                f"{destination!r} fingerprint changed",
            )
        if _stat_at(parent_fd, source) is not None:
            _raise("ambiguous_namespace", f"{source!r} reappeared after {action}")
        os.fsync(parent_fd)
        self._hook(f"after_{action}_fsync")
        return {
            "action": action,
            "source": source,
            "destination": destination,
            "fingerprint": destination_fp,
            "parent_fsynced": True,
        }

    def _fresh_candidate_proof(
        self,
        *,
        intent: Mapping[str, Any],
        parent_fd: int,
        raw_fd: int,
        parts: tuple[str, ...],
    ) -> dict[str, Any]:
        raw_serials = self._raw_serials(raw_fd, parts)
        if (
            len(raw_serials) != intent["raw"]["count"]
            or _serials_sha256(raw_serials) != intent["raw"]["serials_sha256"]
        ):
            _raise("raw_task_changed", "raw serial set changed during recovery")
        output_name = intent["paths"]["output"]
        output_path = self.lerobot_root.joinpath(*parts)
        return self._validation_proof(
            parent_fd=parent_fd,
            output_name=output_name,
            output_path=output_path,
            raw_serials=raw_serials,
            require_passed=True,
        )

    def _decode_state(self, payload: bytes, *, label: str) -> dict[str, Any]:
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RecoveryError(
                "invalid_state",
                f"{label} is not valid UTF-8 JSON",
            ) from exc
        if not isinstance(decoded, dict):
            _raise("invalid_state", f"{label} must contain an object")
        return decoded

    def _derive_state_replacement(
        self,
        *,
        original: Mapping[str, Any],
        intent: Mapping[str, Any],
        durable_serials: list[str],
    ) -> dict[str, Any]:
        replacement = copy.deepcopy(original)
        before_entry = replacement.get(intent["cell_task"], {})
        if before_entry is None:
            before_entry = {}
        if not isinstance(before_entry, dict):
            _raise("invalid_state", "target state entry is not an object")
        after_entry = copy.deepcopy(before_entry)
        durable_set = set(durable_serials)
        failed = after_entry.get("failed_serials", [])
        if not isinstance(failed, list):
            _raise("invalid_state", "failed_serials is not a list")
        if any(not isinstance(serial, str) or not serial for serial in failed):
            _raise("invalid_state", "failed_serials contains a non-string value")
        after_entry["failed_serials"] = sorted(
            {
                serial
                for serial in failed
                if serial not in durable_set
            }
        )
        transient = after_entry.get("transient_failed", {})
        if transient is not None and not isinstance(transient, dict):
            _raise("invalid_state", "transient_failed is not an object")
        if any(
            not isinstance(serial, str) or not serial
            for serial in (transient or {})
        ):
            _raise("invalid_state", "transient_failed contains a non-string key")
        remaining_transient = {
            serial: value
            for serial, value in (transient or {}).items()
            if serial not in durable_set
        }
        if remaining_transient:
            after_entry["transient_failed"] = remaining_transient
        else:
            after_entry.pop("transient_failed", None)
        after_entry["converted_count"] = len(durable_serials)
        after_entry["last_serial"] = max(durable_serials) if durable_serials else ""
        after_entry["last_updated"] = intent["created_at"]
        replacement[intent["cell_task"]] = after_entry
        non_target_before = {
            key: value
            for key, value in original.items()
            if key != intent["cell_task"]
        }
        non_target_after = {
            key: value
            for key, value in replacement.items()
            if key != intent["cell_task"]
        }
        non_target_before_sha256 = _sha256(
            _canonical_json_bytes(non_target_before, compact=True)
        )
        non_target_after_sha256 = _sha256(
            _canonical_json_bytes(non_target_after, compact=True)
        )
        if non_target_before_sha256 != non_target_after_sha256:
            _raise("state_scope_violation", "non-target state entries changed")
        replacement_bytes = _canonical_json_bytes(replacement)
        return {
            "replacement": replacement,
            "replacement_bytes": replacement_bytes,
            "target_entry_before_sha256": _sha256(
                _canonical_json_bytes(before_entry, compact=True)
            ),
            "target_entry_after_sha256": _sha256(
                _canonical_json_bytes(after_entry, compact=True)
            ),
            "non_target_before_sha256": non_target_before_sha256,
            "non_target_after_sha256": non_target_after_sha256,
            "durable_count": len(durable_serials),
            "durable_serials_sha256": _serials_sha256(durable_serials),
        }

    def _build_state_replacement(
        self,
        *,
        lerobot_fd: int,
        intent: dict[str, Any],
        durable_serials: list[str],
    ) -> dict[str, Any]:
        state_name = intent["paths"]["state"]
        original_bytes, original_fp = _read_regular_bytes_at(
            lerobot_fd,
            state_name,
        )
        original = self._decode_state(
            original_bytes,
            label="convert_state.json",
        )
        derived = self._derive_state_replacement(
            original=original,
            intent=intent,
            durable_serials=durable_serials,
        )
        replacement = derived["replacement"]
        replacement_bytes = derived["replacement_bytes"]
        replacement_name = intent["paths"]["state_replacement"]
        existing_info = _stat_at(lerobot_fd, replacement_name)
        if existing_info is None:
            replacement_fp = _atomic_write_json(
                lerobot_fd,
                replacement_name,
                replacement,
                replace=False,
                mode=original_fp["mode"],
                uid=original_fp["uid"],
                gid=original_fp["gid"],
            )
            self._hook("after_state_replacement_write")
        else:
            existing_bytes, replacement_fp = _read_regular_bytes_at(
                lerobot_fd,
                replacement_name,
            )
            if existing_bytes != replacement_bytes:
                _raise(
                    "state_replacement_conflict",
                    "existing state replacement does not match intent",
                )
        return {
            "original_fingerprint": original_fp,
            "original_sha256": _sha256(original_bytes),
            "replacement_fingerprint": replacement_fp,
            "replacement_sha256": _sha256(replacement_bytes),
            "target_entry_before_sha256": derived[
                "target_entry_before_sha256"
            ],
            "target_entry_after_sha256": derived["target_entry_after_sha256"],
            "non_target_before_sha256": derived["non_target_before_sha256"],
            "non_target_after_sha256": derived["non_target_after_sha256"],
            "durable_count": derived["durable_count"],
            "durable_serials_sha256": derived["durable_serials_sha256"],
        }

    def _replay(
        self,
        *,
        intent: dict[str, Any],
        intent_name: str,
        parts: tuple[str, ...],
        lerobot_fd: int,
        parent_fd: int,
        raw_fd: int,
    ) -> dict[str, Any]:
        while True:
            persisted = self._load_intent(
                parent_fd,
                intent_name,
                cell_task=intent["cell_task"],
                mode=intent["mode"],
                output_name=parts[-1],
                lerobot_fd=lerobot_fd,
                raw_fd=raw_fd,
            )
            if persisted != intent:
                _raise("intent_changed", "active recovery intent changed in memory")
            raw_serials = self._verify_raw_snapshot(
                raw_fd=raw_fd,
                parts=parts,
                intent=intent,
            )
            self._authorize_marker_evidence(
                parent_fd=parent_fd,
                intent=intent,
                parts=parts,
                raw_serials=raw_serials,
            )
            self._verify_state_artifacts(
                lerobot_fd=lerobot_fd,
                intent=intent,
            )
            phase = intent.get("phase")
            if phase == "output_quarantine_pending":
                self._preflight_output_quarantine(
                    parent_fd=parent_fd,
                    intent=intent,
                    parts=parts,
                    raw_serials=raw_serials,
                )
                receipt = self._resolve_directory_move(
                    parent_fd,
                    source=intent["paths"]["output"],
                    destination=intent["paths"]["quarantine"],
                    expected=intent["inputs"]["output"],
                    action="output_quarantine",
                )
                next_phase = (
                    "archive_restore_pending"
                    if intent["mode"] == "rollback"
                    else "state_replacement_pending"
                )
                self._advance(
                    parent_fd,
                    intent_name,
                    intent,
                    next_phase,
                    receipt=receipt,
                )
                continue

            if phase == "archive_restore_pending":
                archive = intent["paths"]["archive"]
                if not isinstance(archive, str):
                    _raise("invalid_intent", "rollback intent has no archive")
                receipt = self._resolve_directory_move(
                    parent_fd,
                    source=archive,
                    destination=intent["paths"]["output"],
                    expected=intent["inputs"]["archive"],
                    action="archive_restore",
                )
                self._advance(
                    parent_fd,
                    intent_name,
                    intent,
                    "candidate_verify_pending",
                    receipt=receipt,
                )
                continue

            if phase == "candidate_verify_pending":
                proof = self._fresh_candidate_proof(
                    intent=intent,
                    parent_fd=parent_fd,
                    raw_fd=raw_fd,
                    parts=parts,
                )
                expected = intent["validation"]
                if (
                    proof["durable_serials_sha256"]
                    != expected["durable_serials_sha256"]
                    or proof["durable_count"] != expected["durable_count"]
                ):
                    _raise(
                        "candidate_changed",
                        "candidate durable serial set changed after intent",
                    )
                if intent["mode"] == "rollback":
                    if not _fingerprints_equal(
                        proof["tree"],
                        intent["inputs"]["archive"],
                    ):
                        _raise(
                            "candidate_changed",
                            "restored archive fingerprint changed",
                        )
                elif not _fingerprints_equal(
                    proof["tree"],
                    intent["inputs"]["output"],
                ):
                    _raise("candidate_changed", "output fingerprint changed")
                intent["validation"] = proof
                self._advance(
                    parent_fd,
                    intent_name,
                    intent,
                    "state_replacement_pending",
                )
                continue

            if phase == "state_replacement_pending":
                durable_serials = list(
                    intent.get("validation", {}).get("durable_serials", [])
                )
                if intent["mode"] == "quarantine-restart":
                    durable_serials = []
                if intent.get("state") is None:
                    intent["state"] = self._build_state_replacement(
                        lerobot_fd=lerobot_fd,
                        intent=intent,
                        durable_serials=durable_serials,
                    )
                    self._advance(
                        parent_fd,
                        intent_name,
                        intent,
                        "state_backup_pending",
                    )
                else:
                    self._advance(
                        parent_fd,
                        intent_name,
                        intent,
                        "state_backup_pending",
                    )
                continue

            if phase == "state_backup_pending":
                receipt = self._resolve_regular_move(
                    lerobot_fd,
                    source=intent["paths"]["state"],
                    destination=intent["paths"]["state_backup"],
                    expected=intent["state"]["original_fingerprint"],
                    action="state_backup",
                )
                self._advance(
                    parent_fd,
                    intent_name,
                    intent,
                    "state_install_pending",
                    receipt=receipt,
                )
                continue

            if phase == "state_install_pending":
                receipt = self._resolve_regular_move(
                    lerobot_fd,
                    source=intent["paths"]["state_replacement"],
                    destination=intent["paths"]["state"],
                    expected=intent["state"]["replacement_fingerprint"],
                    action="state_install",
                )
                installed, installed_fp = _read_regular_bytes_at(
                    lerobot_fd,
                    intent["paths"]["state"],
                )
                if (
                    _sha256(installed) != intent["state"]["replacement_sha256"]
                    or not _fingerprints_equal(
                        installed_fp,
                        intent["state"]["replacement_fingerprint"],
                    )
                ):
                    _raise("state_install_failed", "installed state is not replacement")
                self._advance(
                    parent_fd,
                    intent_name,
                    intent,
                    "marker_audit_pending",
                    receipt=receipt,
                )
                continue

            if phase == "marker_audit_pending":
                marker_receipts: list[dict[str, Any]] = []
                for marker in intent["inputs"]["markers"]:
                    marker_receipts.append(
                        self._resolve_regular_move(
                            parent_fd,
                            source=marker["source"],
                            destination=marker["audit"],
                            expected=marker["fingerprint"],
                            action=f"marker_audit_{marker['kind']}",
                        )
                    )
                self._advance(
                    parent_fd,
                    intent_name,
                    intent,
                    "receipt_pending",
                    receipt={
                        "action": "marker_audit",
                        "markers": marker_receipts,
                        "parent_fsynced": True,
                    },
                )
                continue

            if phase == "receipt_pending":
                intent["phase"] = "receipt_durable"
                intent["completed_at"] = _utc_now()
                self._write_intent(
                    parent_fd,
                    intent_name,
                    intent,
                    replace=True,
                )
                continue

            if phase == "receipt_durable":
                _, expected = _read_regular_bytes_at(parent_fd, intent_name)
                receipt = self._resolve_regular_move(
                    parent_fd,
                    source=intent_name,
                    destination=intent["paths"]["receipt"],
                    expected=expected,
                    action="receipt_publish",
                )
                result, _ = _read_json_at(
                    parent_fd,
                    intent["paths"]["receipt"],
                )
                result["receipt_path"] = intent["paths"]["receipt"]
                result["publication"] = receipt
                return result

            _raise("invalid_intent", f"unsupported recovery phase: {phase!r}")

    def recover(self, cell_task: str, mode: str) -> dict[str, Any]:
        if mode not in RECOVERY_MODES:
            _raise("invalid_mode", f"unsupported recovery mode: {mode!r}")
        parts, lerobot_fd, parent_fd, raw_fd = self._open_context(cell_task)
        intent_name = _marker_names(parts[-1])["intent"]
        try:
            fcntl.flock(lerobot_fd, fcntl.LOCK_EX)
            fcntl.flock(parent_fd, fcntl.LOCK_EX)
            if _stat_at(parent_fd, intent_name) is None:
                receipt = self._existing_receipt(
                    parent_fd,
                    lerobot_fd=lerobot_fd,
                    raw_fd=raw_fd,
                    parts=parts,
                    output_name=parts[-1],
                    cell_task=cell_task,
                    mode=mode,
                )
                if receipt is not None:
                    return receipt
                inspection = self._inspection_locked(
                    cell_task=cell_task,
                    parts=parts,
                    parent_fd=parent_fd,
                    raw_fd=raw_fd,
                )
                intent = self._create_intent(
                    mode=mode,
                    cell_task=cell_task,
                    parts=parts,
                    lerobot_fd=lerobot_fd,
                    parent_fd=parent_fd,
                    raw_fd=raw_fd,
                    inspection=inspection,
                )
                self._write_intent(
                    parent_fd,
                    intent_name,
                    intent,
                    replace=False,
                )
            else:
                intent = self._load_intent(
                    parent_fd,
                    intent_name,
                    cell_task=cell_task,
                    mode=mode,
                    output_name=parts[-1],
                    lerobot_fd=lerobot_fd,
                    raw_fd=raw_fd,
                )
            return self._replay(
                intent=intent,
                intent_name=intent_name,
                parts=parts,
                lerobot_fd=lerobot_fd,
                parent_fd=parent_fd,
                raw_fd=raw_fd,
            )
        finally:
            os.close(raw_fd)
            os.close(parent_fd)
            os.close(lerobot_fd)
