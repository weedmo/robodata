#!/usr/bin/env python3
"""Resumably partition one raw task by an exact recording-contract manifest.

The operation never rewrites recording contents.  It pins the source inventory
in a private durable journal, then uses same-filesystem no-clobber renames to
leave one contract in the existing task and expose every other contract as an
explicit sibling task.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from scripts.split_raw_task_by_metadata import (
    _NFS_SUPER_MAGIC,
    _SERIAL_RE,
    _filesystem_type,
    _open_directory_chain_nofollow,
    _rename_noreplace,
    _require_rename_noreplace_support,
    _rename_materialization_noreplace,
)


_JOURNAL_VERSION = 1
_JOURNAL_OPERATION = "partition_raw_task_by_recording_contract"
_JOURNAL_PHASES = frozenset(
    {
        "reserved",
        "applying",
        "finalizing",
        "committed",
        "rolling_back",
        "rolled_back",
    }
)
_DIGEST_LENGTH = 64
_RENAME_STRATEGIES = frozenset(
    {"renameat2_noreplace", "isolated_nfs_plain_rename"}
)


class _IncompleteJournalBootstrap(RuntimeError):
    pass


def _bootstrap_checkpoint(name: str) -> None:
    del name


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _stat_identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _mode(info: os.stat_result) -> int:
    return stat.S_IMODE(info.st_mode)


def _ensure_fd_owner(
    descriptor: int,
    *,
    expected_uid: int,
    expected_gid: int,
    label: str,
) -> os.stat_result:
    info = os.fstat(descriptor)
    if (info.st_uid, info.st_gid) != (expected_uid, expected_gid):
        try:
            os.fchown(descriptor, expected_uid, expected_gid)
        except PermissionError as exc:
            raise PermissionError(
                f"{label} owner cannot be mapped to the source NAS owner"
            ) from exc
        info = os.fstat(descriptor)
    if (info.st_uid, info.st_gid) != (expected_uid, expected_gid):
        raise PermissionError(
            f"{label} owner does not match the source NAS owner"
        )
    return info


def _plain_directory_info(path: Path) -> os.stat_result:
    parent_descriptor = _open_directory_chain_nofollow(path.parent)
    try:
        info = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    finally:
        os.close(parent_descriptor)
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"path must be a plain directory: {path}")
    return info


def _rename_strategy(source_task: Path) -> str:
    descriptor = _open_directory_chain_nofollow(source_task)
    try:
        filesystem_type = _filesystem_type(descriptor)
    finally:
        os.close(descriptor)
    if filesystem_type == _NFS_SUPER_MAGIC:
        if os.environ.get("CURATION_RECOVERY_ISOLATED") != "true":
            raise RuntimeError(
                "NFS partition rename requires the isolated recovery wrapper"
            )
        return "isolated_nfs_plain_rename"
    _require_rename_noreplace_support(source_task.parent)
    return "renameat2_noreplace"


def _stat_at(parent_descriptor: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None


def _rename_partition_noreplace(
    source: Path,
    destination: Path,
    *,
    strategy: str,
) -> None:
    if strategy not in _RENAME_STRATEGIES:
        raise RuntimeError("partition journal rename strategy is invalid")
    if strategy == "renameat2_noreplace":
        _rename_materialization_noreplace(source, destination)
        return
    if os.environ.get("CURATION_RECOVERY_ISOLATED") != "true":
        raise RuntimeError(
            "NFS plain rename fallback requires isolated recovery"
        )
    source_parent = _open_directory_chain_nofollow(source.parent)
    try:
        destination_parent = _open_directory_chain_nofollow(
            destination.parent
        )
        try:
            if (
                _filesystem_type(source_parent) != _NFS_SUPER_MAGIC
                or _filesystem_type(destination_parent) != _NFS_SUPER_MAGIC
                or os.fstat(source_parent).st_dev
                != os.fstat(destination_parent).st_dev
            ):
                raise RuntimeError(
                    "NFS rename fallback filesystem identity changed"
                )
            source_info = _stat_at(source_parent, source.name)
            if source_info is None:
                raise FileNotFoundError(source)
            if _stat_at(destination_parent, destination.name) is not None:
                raise FileExistsError(destination)
            os.rename(
                source.name,
                destination.name,
                src_dir_fd=source_parent,
                dst_dir_fd=destination_parent,
            )
            installed = _stat_at(destination_parent, destination.name)
            if (
                installed is None
                or _stat_identity(installed) != _stat_identity(source_info)
                or _stat_at(source_parent, source.name) is not None
            ):
                raise RuntimeError(
                    "isolated NFS rename identity verification failed: "
                    f"{source} -> {destination}"
                )
            os.fsync(source_parent)
            os.fsync(destination_parent)
        finally:
            os.close(destination_parent)
    finally:
        os.close(source_parent)


def _rename_recording_noreplace(
    *,
    source_root: dict[str, Any],
    destination_root: dict[str, Any],
    serial: str,
    recording: dict[str, Any],
    strategy: str,
) -> None:
    source_descriptor = _open_directory_chain_nofollow(
        Path(source_root["path"])
    )
    try:
        destination_descriptor = _open_directory_chain_nofollow(
            Path(destination_root["path"])
        )
        try:
            if not _matches_directory(os.fstat(source_descriptor), source_root):
                raise RuntimeError(
                    "recording rename source root identity changed"
                )
            if not _matches_directory(
                os.fstat(destination_descriptor),
                destination_root,
            ):
                raise RuntimeError(
                    "recording rename destination root identity changed"
                )
            source_info = _stat_at(source_descriptor, serial)
            if (
                source_info is None
                or not _matches_directory(
                    source_info,
                    recording["directory"],
                )
            ):
                raise RuntimeError(
                    f"recording identity changed before rename: {serial}"
                )
            if _stat_at(destination_descriptor, serial) is not None:
                raise FileExistsError(
                    f"recording destination already exists: {serial}"
                )
            if strategy == "renameat2_noreplace":
                _rename_noreplace(
                    source_descriptor,
                    serial,
                    destination_descriptor,
                    serial,
                )
            elif strategy == "isolated_nfs_plain_rename":
                if os.environ.get("CURATION_RECOVERY_ISOLATED") != "true":
                    raise RuntimeError(
                        "NFS plain rename fallback requires isolated recovery"
                    )
                if (
                    _filesystem_type(source_descriptor) != _NFS_SUPER_MAGIC
                    or _filesystem_type(destination_descriptor)
                    != _NFS_SUPER_MAGIC
                    or os.fstat(source_descriptor).st_dev
                    != os.fstat(destination_descriptor).st_dev
                ):
                    raise RuntimeError(
                        "NFS recording rename filesystem identity changed"
                    )
                os.rename(
                    serial,
                    serial,
                    src_dir_fd=source_descriptor,
                    dst_dir_fd=destination_descriptor,
                )
            else:
                raise RuntimeError(
                    "partition journal rename strategy is invalid"
                )
            installed = _stat_at(destination_descriptor, serial)
            if (
                installed is None
                or not _matches_directory(
                    installed,
                    recording["directory"],
                )
                or _stat_at(source_descriptor, serial) is not None
            ):
                raise RuntimeError(
                    f"recording rename identity verification failed: {serial}"
                )
            _validate_recording_at(
                destination_descriptor,
                serial,
                recording,
            )
            os.fsync(source_descriptor)
            os.fsync(destination_descriptor)
        finally:
            os.close(destination_descriptor)
    finally:
        os.close(source_descriptor)


def _open_plain_regular_at(
    parent_descriptor: int,
    name: str,
    *,
    require_private: bool,
    expected_uid: int | None,
    expected_gid: int | None = None,
    writable: bool = False,
) -> tuple[int, os.stat_result]:
    descriptor = os.open(
        name,
        (os.O_RDWR if writable else os.O_RDONLY)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_descriptor,
    )
    try:
        info = os.fstat(descriptor)
        path_info = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or _stat_identity(path_info) != _stat_identity(info)
        ):
            raise ValueError(f"file must be a single-link regular file: {name}")
        if require_private and _mode(info) != 0o600:
            raise PermissionError(
                f"file must be mode 0600 private data: {name}"
            )
        if expected_uid is not None and info.st_uid != expected_uid:
            raise PermissionError(
                f"private file owner does not match the pinned NAS uid: {name}"
            )
        if expected_gid is not None and info.st_gid != expected_gid:
            raise PermissionError(
                f"private file group does not match the pinned NAS gid: {name}"
            )
        return descriptor, info
    except BaseException:
        os.close(descriptor)
        raise


def _read_private_json(
    path: Path,
    *,
    expected_uid: int | None = None,
    expected_gid: int | None = None,
) -> tuple[dict[str, Any], dict[str, int], str]:
    path = _absolute(path)
    parent_descriptor = _open_directory_chain_nofollow(path.parent)
    descriptor = -1
    try:
        descriptor, info = _open_plain_regular_at(
            parent_descriptor,
            path.name,
            require_private=True,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
        )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        payload_bytes = b"".join(chunks)
        final_info = os.fstat(descriptor)
        path_info = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            _stat_identity(final_info) != _stat_identity(info)
            or _stat_identity(path_info) != _stat_identity(info)
            or final_info.st_size != len(payload_bytes)
        ):
            raise RuntimeError(f"private JSON changed while reading: {path}")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON document: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON document must be an object: {path}")
    identity = {
        "device": info.st_dev,
        "inode": info.st_ino,
        "size": info.st_size,
        "mode": _mode(info),
        "uid": info.st_uid,
        "gid": info.st_gid,
    }
    return payload, identity, hashlib.sha256(payload_bytes).hexdigest()


def _private_journal_parent(
    path: Path,
    *,
    expected_uid: int,
    expected_gid: int,
) -> tuple[int, os.stat_result]:
    descriptor = _open_directory_chain_nofollow(path)
    info = os.fstat(descriptor)
    if (
        _mode(info) != 0o700
        or info.st_uid != expected_uid
        or info.st_gid != expected_gid
    ):
        os.close(descriptor)
        raise PermissionError(
            "journal parent must be mode 0700 and owned by the source NAS owner"
        )
    return descriptor, info


def _encode_json(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _compact_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("short write while persisting partition journal")
        offset += written


def _read_fd_bytes(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _stable_identity(info: os.stat_result) -> dict[str, int]:
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "mode": _mode(info),
        "uid": info.st_uid,
        "gid": info.st_gid,
    }


def _state_log_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.state-log")


def _read_private_bytes(
    path: Path,
    *,
    expected_uid: int,
    expected_gid: int,
) -> tuple[bytes, os.stat_result]:
    path = _absolute(path)
    parent_descriptor = _open_directory_chain_nofollow(path.parent)
    descriptor = -1
    try:
        descriptor, initial = _open_plain_regular_at(
            parent_descriptor,
            path.name,
            require_private=True,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
        )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        payload = b"".join(chunks)
        final = os.fstat(descriptor)
        visible = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            _stat_identity(initial) != _stat_identity(final)
            or _stat_identity(visible) != _stat_identity(final)
            or final.st_size != len(payload)
        ):
            raise RuntimeError(f"private file changed while reading: {path}")
        return payload, final
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)


def _state_record(
    payload: dict[str, Any],
    *,
    sequence: int,
    previous_sha256: str | None,
) -> dict[str, Any]:
    core = {
        "kind": "state",
        "payload": payload,
        "previous_sha256": previous_sha256,
        "sequence": sequence,
    }
    return {
        **core,
        "sha256": hashlib.sha256(_compact_json(core)).hexdigest(),
    }


def _bootstrap_invocation_payload(
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "version": payload["version"],
        "operation": payload["operation"],
        "journal_parent": payload["journal_parent"],
        "contract_manifest": payload["contract_manifest"],
        "keep_digest": payload["keep_digest"],
        "partitions": payload["partitions"],
        "recording_digests": payload["recording_digests"],
        "rename_strategy": payload["rename_strategy"],
        "source": payload["source"],
        "destinations": {
            digest: {
                "path": destination["path"],
                "working_mode": destination["working_mode"],
                "committed_mode": destination["committed_mode"],
                "owner_uid": destination["owner_uid"],
                "owner_gid": destination["owner_gid"],
            }
            for digest, destination in payload["destinations"].items()
        },
    }


def _bootstrap_record(
    payload: dict[str, Any],
    *,
    journal_path: Path,
    state_identity: dict[str, int],
) -> dict[str, Any]:
    core = {
        "invocation_sha256": hashlib.sha256(
            _compact_json(_bootstrap_invocation_payload(payload))
        ).hexdigest(),
        "journal_path": str(journal_path),
        "kind": "bootstrap",
        "payload": payload,
        "state_log_identity": state_identity,
        "version": 1,
    }
    return {
        **core,
        "sha256": hashlib.sha256(_compact_json(core)).hexdigest(),
    }


def _parse_bootstrap_record(
    encoded: bytes,
    *,
    journal_path: Path,
    state_identity: dict[str, int],
) -> dict[str, Any]:
    try:
        record = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise _IncompleteJournalBootstrap(
            "partition bootstrap record is incomplete"
        ) from exc
    if not isinstance(record, dict):
        raise RuntimeError("partition bootstrap record is invalid")
    core = {
        "invocation_sha256": record.get("invocation_sha256"),
        "journal_path": record.get("journal_path"),
        "kind": record.get("kind"),
        "payload": record.get("payload"),
        "state_log_identity": record.get("state_log_identity"),
        "version": record.get("version"),
    }
    payload = core["payload"]
    if (
        core["kind"] != "bootstrap"
        or core["version"] != 1
        or core["journal_path"] != str(journal_path)
        or core["state_log_identity"] != state_identity
        or not isinstance(payload, dict)
        or core["invocation_sha256"]
        != hashlib.sha256(
            _compact_json(_bootstrap_invocation_payload(payload))
        ).hexdigest()
        or record.get("sha256")
        != hashlib.sha256(_compact_json(core)).hexdigest()
    ):
        raise RuntimeError("partition bootstrap authority is invalid")
    return payload


def _parse_state_log(
    state_bytes: bytes,
    *,
    journal_path: Path,
    journal: dict[str, Any],
    journal_identity: dict[str, int],
    journal_sha256: str,
    state_identity: dict[str, int],
) -> tuple[dict[str, Any], int, str, int]:
    complete_lines: list[bytes] = []
    valid_size = 0
    for line in state_bytes.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            break
        complete_lines.append(line[:-1])
        valid_size += len(line)
    if not complete_lines:
        raise RuntimeError("partition state log has no durable header")
    bootstrap_payload = _parse_bootstrap_record(
        complete_lines[0],
        journal_path=journal_path,
        state_identity=state_identity,
    )
    if _bootstrap_invocation_payload(bootstrap_payload) != (
        _bootstrap_invocation_payload(journal)
    ):
        raise RuntimeError("partition bootstrap invocation changed")
    if len(complete_lines) < 3:
        raise _IncompleteJournalBootstrap(
            "partition journal bootstrap is not fully durable"
        )
    try:
        header = json.loads(complete_lines[1].decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("partition state log header is invalid") from exc
    if (
        not isinstance(header, dict)
        or header
        != {
            "journal_identity": journal_identity,
            "journal_sha256": journal_sha256,
            "kind": "header",
            "plan_sha256": journal["plan_sha256"],
            "state_log_identity": state_identity,
            "version": 1,
        }
    ):
        raise RuntimeError("partition state log authority does not match journal")

    current = journal
    previous_sha256: str | None = None
    sequence = -1
    for encoded in complete_lines[2:]:
        try:
            record = json.loads(encoded.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("partition state log record is invalid") from exc
        if not isinstance(record, dict):
            raise RuntimeError("partition state log record is invalid")
        record_sequence = record.get("sequence")
        if isinstance(record_sequence, bool) or not isinstance(
            record_sequence,
            int,
        ):
            raise RuntimeError("partition state log sequence is invalid")
        core = {
            "kind": record.get("kind"),
            "payload": record.get("payload"),
            "previous_sha256": record.get("previous_sha256"),
            "sequence": record_sequence,
        }
        expected_sha256 = hashlib.sha256(_compact_json(core)).hexdigest()
        if (
            core["kind"] != "state"
            or core["sequence"] != sequence + 1
            or core["previous_sha256"] != previous_sha256
            or record.get("sha256") != expected_sha256
            or not isinstance(core["payload"], dict)
            or core["payload"].get("plan_sha256")
            != journal["plan_sha256"]
            or _plan_payload(core["payload"]) != _plan_payload(journal)
        ):
            raise RuntimeError("partition state log chain is invalid")
        current = core["payload"]
        sequence = record_sequence
        previous_sha256 = expected_sha256
    if sequence < 0 or previous_sha256 is None:
        raise RuntimeError("partition state log has no durable state")
    return current, sequence, previous_sha256, valid_size


def _read_journal(
    path: Path,
    *,
    expected_uid: int,
    expected_gid: int,
) -> tuple[
    dict[str, Any],
    dict[str, int],
    str,
    tuple[int, str, int],
]:
    journal, identity, journal_sha256 = _read_private_json(
        path,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    stable_journal_identity = {
        key: identity[key]
        for key in ("device", "inode", "mode", "uid", "gid")
    }
    if journal.get("journal_identity") != stable_journal_identity:
        raise RuntimeError("partition journal inode was replaced")
    state = journal.get("state_log")
    expected_state_path = _state_log_path(_absolute(path))
    if (
        not isinstance(state, dict)
        or state.get("path") != str(expected_state_path)
        or not isinstance(state.get("identity"), dict)
    ):
        raise RuntimeError("partition journal state log binding is invalid")
    state_bytes, state_info = _read_private_bytes(
        expected_state_path,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    state_identity = _stable_identity(state_info)
    if state["identity"] != state_identity:
        raise RuntimeError("partition state log inode was replaced")
    current, sequence, previous_sha256, valid_size = _parse_state_log(
        state_bytes,
        journal_path=_absolute(path),
        journal=journal,
        journal_identity=stable_journal_identity,
        journal_sha256=journal_sha256,
        state_identity=state_identity,
    )
    return (
        current,
        identity,
        journal_sha256,
        (sequence, previous_sha256, valid_size),
    )


def _read_bootstrap_payload(
    path: Path,
    *,
    expected_uid: int,
    expected_gid: int,
) -> dict[str, Any]:
    path = _absolute(path)
    state_path = _state_log_path(path)
    state_bytes, state_info = _read_private_bytes(
        state_path,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    newline = state_bytes.find(b"\n")
    if newline < 0:
        raise _IncompleteJournalBootstrap(
            "partition bootstrap payload is not durable"
        )
    return _parse_bootstrap_record(
        state_bytes[:newline],
        journal_path=path,
        state_identity=_stable_identity(state_info),
    )


def _write_journal(
    path: Path,
    payload: dict[str, Any],
    *,
    expected_identity: tuple[int, int] | None,
    expected_owner_uid: int,
    expected_owner_gid: int,
    rename_strategy: str,
) -> tuple[int, int]:
    path = _absolute(path)
    parent_descriptor, _ = _private_journal_parent(
        path.parent,
        expected_uid=expected_owner_uid,
        expected_gid=expected_owner_gid,
    )
    state_path = _state_log_path(path)
    descriptor = -1
    state_descriptor = -1
    try:
        if expected_identity is None:
            state_created = False
            try:
                state_descriptor = os.open(
                    state_path.name,
                    os.O_RDWR
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NONBLOCK", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=parent_descriptor,
                )
            except FileExistsError:
                state_descriptor, state_info = _open_plain_regular_at(
                    parent_descriptor,
                    state_path.name,
                    require_private=True,
                    expected_uid=expected_owner_uid,
                    expected_gid=expected_owner_gid,
                    writable=True,
                )
            else:
                state_created = True
                os.fchmod(state_descriptor, 0o600)
                state_info = _ensure_fd_owner(
                    state_descriptor,
                    expected_uid=expected_owner_uid,
                    expected_gid=expected_owner_gid,
                    label="partition state log",
                )
            if state_created:
                _bootstrap_checkpoint("state-log-created")
            state_identity = _stable_identity(state_info)
            state_bytes = _read_fd_bytes(state_descriptor)
            newline = state_bytes.find(b"\n")
            if newline < 0:
                if any(
                    _path_presence(Path(destination[key])) is not None
                    for destination in payload["destinations"].values()
                    for key in ("path", "construction")
                ):
                    raise RuntimeError(
                        "incomplete bootstrap has destination artifacts"
                    )
                os.ftruncate(state_descriptor, 0)
                os.lseek(state_descriptor, 0, os.SEEK_SET)
                bootstrap = _bootstrap_record(
                    payload,
                    journal_path=path,
                    state_identity=state_identity,
                )
                bootstrap_bytes = _compact_json(bootstrap) + b"\n"
                _write_all(state_descriptor, bootstrap_bytes)
                os.fsync(state_descriptor)
                os.fsync(parent_descriptor)
                _bootstrap_checkpoint("bootstrap-durable")
                bootstrap_end = len(bootstrap_bytes)
            else:
                bootstrap_end = newline + 1
                bootstrap_payload = _parse_bootstrap_record(
                    state_bytes[:newline],
                    journal_path=path,
                    state_identity=state_identity,
                )
                if _bootstrap_invocation_payload(bootstrap_payload) != (
                    _bootstrap_invocation_payload(payload)
                ):
                    raise RuntimeError(
                        "partition bootstrap invocation changed"
                    )
                payload.clear()
                payload.update(bootstrap_payload)

            journal_created = False
            try:
                descriptor = os.open(
                    path.name,
                    os.O_RDWR
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NONBLOCK", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=parent_descriptor,
                )
            except FileExistsError:
                descriptor, journal_info = _open_plain_regular_at(
                    parent_descriptor,
                    path.name,
                    require_private=True,
                    expected_uid=expected_owner_uid,
                    expected_gid=expected_owner_gid,
                    writable=True,
                )
            else:
                journal_created = True
                os.fchmod(descriptor, 0o600)
                journal_info = _ensure_fd_owner(
                    descriptor,
                    expected_uid=expected_owner_uid,
                    expected_gid=expected_owner_gid,
                    label="partition journal",
                )
            if journal_created:
                _bootstrap_checkpoint("journal-created")
            payload["journal_identity"] = _stable_identity(journal_info)
            payload["state_log"] = {
                "identity": state_identity,
                "path": str(state_path),
            }
            payload["plan_sha256"] = _plan_sha256(payload)
            encoded = _encode_json(payload)
            existing_journal = _read_fd_bytes(descriptor)
            if existing_journal != encoded:
                if existing_journal:
                    try:
                        parsed_existing = json.loads(
                            existing_journal.decode("utf-8")
                        )
                    except (UnicodeError, json.JSONDecodeError):
                        parsed_existing = None
                    if isinstance(parsed_existing, dict):
                        raise RuntimeError(
                            "existing bootstrap journal does not match plan"
                        )
                    complete_tail = state_bytes[bootstrap_end:]
                    if b"\n" in complete_tail:
                        raise RuntimeError(
                            "journal is invalid after durable state bootstrap"
                        )
                os.ftruncate(descriptor, 0)
                os.lseek(descriptor, 0, os.SEEK_SET)
                _write_all(descriptor, encoded)
                os.fsync(descriptor)
                _bootstrap_checkpoint("journal-durable")
            journal_sha256 = hashlib.sha256(encoded).hexdigest()
            header = {
                "journal_identity": payload["journal_identity"],
                "journal_sha256": journal_sha256,
                "kind": "header",
                "plan_sha256": payload["plan_sha256"],
                "state_log_identity": payload["state_log"]["identity"],
                "version": 1,
            }
            record = _state_record(
                payload,
                sequence=0,
                previous_sha256=None,
            )
            current_state_bytes = _read_fd_bytes(state_descriptor)
            complete_lines = [
                line
                for line in current_state_bytes.splitlines(keepends=True)
                if line.endswith(b"\n")
            ]
            if len(complete_lines) >= 3:
                current, _, _, valid_size = _parse_state_log(
                    current_state_bytes,
                    journal_path=path,
                    journal=payload,
                    journal_identity=payload["journal_identity"],
                    journal_sha256=journal_sha256,
                    state_identity=state_identity,
                )
                if current != payload or valid_size != len(current_state_bytes):
                    raise RuntimeError(
                        "existing durable bootstrap state is inconsistent"
                    )
                return _stat_identity(journal_info)
            if len(complete_lines) > 2:
                raise RuntimeError("partition bootstrap state is ambiguous")
            os.ftruncate(state_descriptor, bootstrap_end)
            os.lseek(state_descriptor, 0, os.SEEK_END)
            _write_all(
                state_descriptor,
                _compact_json(header)
                + b"\n"
                + _compact_json(record)
                + b"\n",
            )
            os.fsync(state_descriptor)
            os.fsync(parent_descriptor)
            _bootstrap_checkpoint("state-durable")
            visible_journal = os.stat(
                path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            visible_state = os.stat(
                state_path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (
                _stat_identity(visible_journal)
                != _stat_identity(journal_info)
                or _stat_identity(visible_state) != _stat_identity(state_info)
            ):
                raise RuntimeError(
                    "partition bootstrap path identity changed"
                )
            return _stat_identity(journal_info)

        current, identity, _, state_context = _read_journal(
            path,
            expected_uid=expected_owner_uid,
            expected_gid=expected_owner_gid,
        )
        if _journal_identity(identity) != expected_identity:
            raise RuntimeError("journal identity changed before state append")
        if _plan_payload(current) != _plan_payload(payload):
            raise RuntimeError("journal plan changed before state append")
        sequence, previous_sha256, valid_size = state_context
        state_descriptor, state_info = _open_plain_regular_at(
            parent_descriptor,
            state_path.name,
            require_private=True,
            expected_uid=expected_owner_uid,
            expected_gid=expected_owner_gid,
            writable=True,
        )
        if _stable_identity(state_info) != payload["state_log"]["identity"]:
            raise RuntimeError("partition state log identity changed")
        if state_info.st_size != valid_size:
            os.ftruncate(state_descriptor, valid_size)
            os.fsync(state_descriptor)
        os.lseek(state_descriptor, 0, os.SEEK_END)
        record = _state_record(
            payload,
            sequence=sequence + 1,
            previous_sha256=previous_sha256,
        )
        _write_all(state_descriptor, _compact_json(record) + b"\n")
        os.fsync(state_descriptor)
        os.fsync(parent_descriptor)
        final_state = os.fstat(state_descriptor)
        visible_state = os.stat(
            state_path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        visible_journal = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(final_state.st_mode)
            or final_state.st_nlink != 1
            or _mode(final_state) != 0o600
            or final_state.st_uid != expected_owner_uid
            or final_state.st_gid != expected_owner_gid
            or _stat_identity(visible_state)
            != _stat_identity(final_state)
            or _stat_identity(visible_journal) != expected_identity
            or not stat.S_ISREG(visible_journal.st_mode)
            or visible_journal.st_nlink != 1
            or _mode(visible_journal) != 0o600
            or visible_journal.st_uid != expected_owner_uid
            or visible_journal.st_gid != expected_owner_gid
        ):
            raise RuntimeError(
                "journal or state-log path changed during durable append"
            )
        return expected_identity
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if state_descriptor >= 0:
            os.close(state_descriptor)
        os.close(parent_descriptor)


def _canonical_digest(contract: dict[str, Any]) -> str:
    encoded = json.dumps(
        contract,
        allow_nan=False,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _plan_payload(journal: dict[str, Any]) -> dict[str, Any]:
    return {
        "token": journal["token"],
        "journal_identity": journal["journal_identity"],
        "state_log": journal["state_log"],
        "journal_parent": journal["journal_parent"],
        "contract_manifest": journal["contract_manifest"],
        "keep_digest": journal["keep_digest"],
        "partitions": journal["partitions"],
        "recording_digests": journal["recording_digests"],
        "rename_strategy": journal["rename_strategy"],
        "source": journal["source"],
        "destinations": {
            digest: {
                "path": destination["path"],
                "construction": destination["construction"],
                "marker_name": destination["marker_name"],
                "marker_sha256": destination["marker_sha256"],
                "marker_size": destination["marker_size"],
                "working_mode": destination["working_mode"],
                "committed_mode": destination["committed_mode"],
                "owner_uid": destination["owner_uid"],
                "owner_gid": destination["owner_gid"],
            }
            for digest, destination in journal["destinations"].items()
        },
    }


def _plan_sha256(journal: dict[str, Any]) -> str:
    encoded = json.dumps(
        _plan_payload(journal),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_digest(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _DIGEST_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("contract digest must be lowercase SHA-256")
    return value


def _validate_contract_manifest(
    payload: dict[str, Any],
    *,
    source_task: Path,
) -> dict[str, list[str]]:
    invariants = payload.get("invariants")
    required_invariants = {
        "partition_intersections_empty": True,
        "raw_mutation_performed": False,
        "recorded_exactly_once": True,
        "resolved_invalid_intersection_empty": True,
    }
    if (
        payload.get("version") != 1
        or payload.get("contract_version") != 1
        or payload.get("digest_algorithm") != "sha256"
        or invariants != required_invariants
        or payload.get("invalid") != []
    ):
        raise ValueError("contract manifest version or invariants are invalid")

    task = payload.get("task")
    if not isinstance(task, str):
        raise ValueError("contract manifest task is invalid")
    task_parts = PurePosixPath(task).parts
    if (
        not task_parts
        or PurePosixPath(task).is_absolute()
        or any(part in {"", ".", ".."} for part in task_parts)
        or tuple(source_task.parts[-len(task_parts) :]) != task_parts
    ):
        raise ValueError("contract manifest task does not match source task")

    partitions_payload = payload.get("partitions")
    if not isinstance(partitions_payload, list) or not partitions_payload:
        raise ValueError("contract manifest has no partitions")
    partitions: dict[str, list[str]] = {}
    seen_serials: set[str] = set()
    for partition in partitions_payload:
        if not isinstance(partition, dict):
            raise ValueError("contract partition must be an object")
        digest = _validate_digest(partition.get("digest"))
        contract = partition.get("contract")
        serials = partition.get("serials")
        if (
            digest in partitions
            or not isinstance(contract, dict)
            or _canonical_digest(contract) != digest
            or not isinstance(serials, list)
            or not serials
        ):
            raise ValueError("contract partition digest or payload is invalid")
        normalized: list[str] = []
        for serial in serials:
            if (
                not isinstance(serial, str)
                or not _SERIAL_RE.fullmatch(serial)
                or serial in seen_serials
            ):
                raise ValueError(
                    "contract partitions are not disjoint or contain duplicate serials"
                )
            seen_serials.add(serial)
            normalized.append(serial)
        if normalized != sorted(normalized):
            raise ValueError("contract partition serials must be sorted")
        partitions[digest] = normalized

    recordings_payload = payload.get("recordings")
    if not isinstance(recordings_payload, list):
        raise ValueError("contract manifest recordings are invalid")
    recording_map: dict[str, str] = {}
    for recording in recordings_payload:
        if not isinstance(recording, dict):
            raise ValueError("contract manifest recording must be an object")
        serial = recording.get("serial")
        digest = recording.get("digest")
        if (
            recording.get("status") != "resolved"
            or not isinstance(serial, str)
            or not _SERIAL_RE.fullmatch(serial)
            or serial in recording_map
            or digest not in partitions
        ):
            raise ValueError("contract manifest recording is invalid or duplicate")
        recording_map[serial] = digest
    expected_recording_map = {
        serial: digest
        for digest, serials in partitions.items()
        for serial in serials
    }
    if recording_map != expected_recording_map:
        raise ValueError(
            "contract recordings do not exactly match disjoint partitions"
        )

    summary = payload.get("summary")
    expected_count = len(expected_recording_map)
    if summary != {
        "invalid": 0,
        "partition_count": len(partitions),
        "resolved": expected_count,
        "total": expected_count,
    }:
        raise ValueError("contract manifest summary is inconsistent")
    return partitions


def _file_inventory(info: os.stat_result, name: str) -> dict[str, Any]:
    return {
        "name": name,
        "device": info.st_dev,
        "inode": info.st_ino,
        "size": info.st_size,
        "mode": _mode(info),
        "uid": info.st_uid,
        "gid": info.st_gid,
        "nlink": info.st_nlink,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
    }


def _directory_inventory(info: os.stat_result) -> dict[str, int]:
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "mode": _mode(info),
        "uid": info.st_uid,
        "gid": info.st_gid,
    }


def _inventory_recording(root_descriptor: int, serial: str) -> dict[str, Any]:
    visible_info = os.stat(
        serial,
        dir_fd=root_descriptor,
        follow_symlinks=False,
    )
    if not stat.S_ISDIR(visible_info.st_mode):
        raise ValueError(f"recording must be a plain directory: {serial}")
    descriptor = os.open(
        serial,
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=root_descriptor,
    )
    try:
        pinned_info = os.fstat(descriptor)
        if _stat_identity(pinned_info) != _stat_identity(visible_info):
            raise RuntimeError(f"recording identity changed: {serial}")
        files: list[dict[str, Any]] = []
        for name in sorted(os.listdir(descriptor)):
            info = os.stat(
                name,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(
                    f"recording entries must be regular files: {serial}/{name}"
                )
            files.append(_file_inventory(info, name))
        names = {item["name"] for item in files}
        if (
            "metacard.json" not in names
            or not any(name.endswith(".mcap") for name in names)
        ):
            raise ValueError(f"recording is missing required regular files: {serial}")
        return {
            "serial": serial,
            "directory": _directory_inventory(pinned_info),
            "files": files,
        }
    finally:
        os.close(descriptor)


def _inventory_preserved_entry(
    parent_descriptor: int,
    name: str,
) -> dict[str, Any]:
    info = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if stat.S_ISREG(info.st_mode):
        return {
            "name": name,
            "kind": "regular_file",
            **_file_inventory(info, name),
        }
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"preserved hidden entry is not regular/plain: {name}")
    descriptor = os.open(
        name,
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_descriptor,
    )
    try:
        pinned = os.fstat(descriptor)
        if _stat_identity(pinned) != _stat_identity(info):
            raise RuntimeError(f"preserved directory identity changed: {name}")
        children = [
            _inventory_preserved_entry(descriptor, child)
            for child in sorted(os.listdir(descriptor))
        ]
        return {
            "name": name,
            "kind": "directory",
            **_directory_inventory(pinned),
            "children": children,
        }
    finally:
        os.close(descriptor)


def _inventory_source(
    source_task: Path,
    expected_serials: set[str],
) -> dict[str, Any]:
    parent_descriptor = _open_directory_chain_nofollow(source_task.parent)
    try:
        descriptor = _open_directory_chain_nofollow(source_task)
        try:
            parent_info = os.fstat(parent_descriptor)
            root_info = os.fstat(descriptor)
            actual_entries = set(os.listdir(descriptor))
            missing = expected_serials - actual_entries
            if missing:
                raise ValueError(
                    "source does not exactly contain manifest recordings; "
                    f"missing {sorted(missing)[:3]}"
                )
            recordings = {
                serial: _inventory_recording(descriptor, serial)
                for serial in sorted(expected_serials)
            }
            preserved: list[dict[str, Any]] = []
            for name in sorted(actual_entries - expected_serials):
                if not name.startswith("."):
                    raise ValueError(
                        "source contains recording omitted from exact contract "
                        f"manifest: {name}"
                    )
                preserved.append(
                    _inventory_preserved_entry(descriptor, name)
                )
            return {
                "path": str(source_task),
                "parent_device": parent_info.st_dev,
                "parent_inode": parent_info.st_ino,
                "parent_mode": _mode(parent_info),
                "parent_uid": parent_info.st_uid,
                "parent_gid": parent_info.st_gid,
                **_directory_inventory(root_info),
                "recordings": recordings,
                "preserved_entries": preserved,
            }
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_descriptor)


def _matches_file(info: os.stat_result, expected: dict[str, Any]) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and (
            info.st_dev,
            info.st_ino,
            info.st_size,
            _mode(info),
            info.st_uid,
            info.st_gid,
            info.st_nlink,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )
        == (
            expected["device"],
            expected["inode"],
            expected["size"],
            expected["mode"],
            expected["uid"],
            expected["gid"],
            expected["nlink"],
            expected["mtime_ns"],
            expected["ctime_ns"],
        )
    )


def _matches_directory(info: os.stat_result, expected: dict[str, Any]) -> bool:
    return (
        stat.S_ISDIR(info.st_mode)
        and (
            info.st_dev,
            info.st_ino,
            _mode(info),
            info.st_uid,
            info.st_gid,
        )
        == (
            expected["device"],
            expected["inode"],
            expected["mode"],
            expected["uid"],
            expected["gid"],
        )
    )


def _validate_recording_at(
    root_descriptor: int,
    serial: str,
    expected: dict[str, Any],
) -> None:
    visible = os.stat(
        serial,
        dir_fd=root_descriptor,
        follow_symlinks=False,
    )
    if not _matches_directory(visible, expected["directory"]):
        raise RuntimeError(f"recording directory identity changed or replaced: {serial}")
    descriptor = os.open(
        serial,
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=root_descriptor,
    )
    try:
        pinned = os.fstat(descriptor)
        if _stat_identity(pinned) != _stat_identity(visible):
            raise RuntimeError(f"recording path identity changed: {serial}")
        expected_files = {item["name"]: item for item in expected["files"]}
        if set(os.listdir(descriptor)) != set(expected_files):
            raise RuntimeError(f"recording file inventory changed: {serial}")
        for name, expected_file in expected_files.items():
            actual = os.stat(
                name,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            if not _matches_file(actual, expected_file):
                raise RuntimeError(
                    f"recording file identity changed or replaced: {serial}/{name}"
                )
    finally:
        os.close(descriptor)


def _validate_preserved_entry(
    parent_descriptor: int,
    expected: dict[str, Any],
) -> None:
    name = expected["name"]
    info = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if expected["kind"] == "regular_file":
        if not _matches_file(info, expected):
            raise RuntimeError(f"preserved hidden file changed: {name}")
        return
    if not _matches_directory(info, expected):
        raise RuntimeError(f"preserved hidden directory changed: {name}")
    descriptor = os.open(
        name,
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_descriptor,
    )
    try:
        children = {child["name"]: child for child in expected["children"]}
        if set(os.listdir(descriptor)) != set(children):
            raise RuntimeError(f"preserved hidden directory inventory changed: {name}")
        for child in children.values():
            _validate_preserved_entry(descriptor, child)
    finally:
        os.close(descriptor)


def _normalize_destinations(
    *,
    source_task: Path,
    partitions: dict[str, list[str]],
    keep_digest: str,
    destinations: dict[str, Path],
) -> dict[str, str]:
    keep_digest = _validate_digest(keep_digest)
    if keep_digest not in partitions:
        raise ValueError("keep digest is not present in the contract manifest")
    expected = set(partitions) - {keep_digest}
    if set(destinations) != expected:
        raise ValueError("destinations must exactly cover every non-keep digest")
    normalized: dict[str, str] = {}
    seen_paths: set[Path] = set()
    source_device = _plain_directory_info(source_task).st_dev
    for digest, destination in destinations.items():
        _validate_digest(digest)
        path = _absolute(Path(destination))
        if (
            path.parent != source_task.parent
            or path == source_task
            or path.name.startswith(".")
            or not _SERIAL_RE.fullmatch(path.name)
            or path in seen_paths
        ):
            raise ValueError(
                "each destination must be a distinct visible sibling task"
            )
        parent_descriptor = _open_directory_chain_nofollow(path.parent)
        try:
            if os.fstat(parent_descriptor).st_dev != source_device:
                raise RuntimeError("destination is on a different filesystem")
        finally:
            os.close(parent_descriptor)
        seen_paths.add(path)
        normalized[digest] = str(path)
    return normalized


def _new_destination_record(
    path: Path,
    token: str,
    *,
    committed_mode: int,
    owner_uid: int,
    owner_gid: int,
) -> dict[str, Any]:
    marker_name = f".robodata-contract-partition-{token}"
    marker_payload = (
        f"robodata-contract-partition-v1:{marker_name}\n".encode("ascii")
    )
    return {
        "path": str(path),
        "construction": str(
            path.with_name(
                f".{path.name}.contract-partition-construction-{token}"
            )
        ),
        "marker_name": marker_name,
        "marker_sha256": hashlib.sha256(marker_payload).hexdigest(),
        "marker_size": len(marker_payload),
        "working_mode": 0o700,
        "committed_mode": committed_mode,
        "owner_uid": owner_uid,
        "owner_gid": owner_gid,
        "device": None,
        "inode": None,
        "mode": None,
        "uid": None,
        "gid": None,
        "marker_device": None,
        "marker_inode": None,
    }


def _path_presence(path: Path) -> os.stat_result | None:
    parent_descriptor = _open_directory_chain_nofollow(path.parent)
    try:
        try:
            return os.stat(
                path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return None
    finally:
        os.close(parent_descriptor)


def _validate_destination_marker(
    descriptor: int,
    destination: dict[str, Any],
    *,
    require_bound_identity: bool,
) -> os.stat_result:
    marker_name = destination["marker_name"]
    marker_descriptor = os.open(
        marker_name,
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=descriptor,
    )
    try:
        marker = os.fstat(marker_descriptor)
        marker_bytes = b""
        while True:
            chunk = os.read(marker_descriptor, 4096)
            if not chunk:
                break
            marker_bytes += chunk
        path_marker = os.stat(
            marker_name,
            dir_fd=descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(marker.st_mode)
            or marker.st_size != destination["marker_size"]
            or len(marker_bytes) != destination["marker_size"]
            or hashlib.sha256(marker_bytes).hexdigest()
            != destination["marker_sha256"]
            or marker.st_nlink != 1
            or _mode(marker) != 0o600
            or marker.st_uid != destination["owner_uid"]
            or marker.st_gid != destination["owner_gid"]
            or _stat_identity(path_marker) != _stat_identity(marker)
        ):
            raise RuntimeError("destination ownership marker changed")
        if require_bound_identity and _stat_identity(marker) != (
            destination["marker_device"],
            destination["marker_inode"],
        ):
            raise RuntimeError("destination ownership marker identity changed")
        return marker
    finally:
        os.close(marker_descriptor)


def _destination_marker_info(
    descriptor: int,
    destination: dict[str, Any],
    *,
    required: bool,
) -> os.stat_result | None:
    try:
        return _validate_destination_marker(
            descriptor,
            destination,
            require_bound_identity=destination["marker_device"] is not None,
        )
    except FileNotFoundError:
        if required:
            raise RuntimeError("destination ownership marker disappeared")
        return None


def _open_destination(
    destination: dict[str, Any],
    *,
    require_bound_identity: bool,
    allowed_modes: set[int] | None = None,
) -> int:
    path = Path(destination["path"])
    descriptor = _open_directory_chain_nofollow(path)
    info = os.fstat(descriptor)
    if require_bound_identity:
        if (
            info.st_dev,
            info.st_ino,
            info.st_uid,
            info.st_gid,
        ) != (
            destination["device"],
            destination["inode"],
            destination["uid"],
            destination["gid"],
        ) or (
            allowed_modes is None
            and _mode(info) != destination["mode"]
        ) or (
            allowed_modes is not None
            and _mode(info) not in allowed_modes
        ):
            os.close(descriptor)
            raise RuntimeError("destination directory identity changed")
    return descriptor


def _create_destination_marker(
    descriptor: int,
    destination: dict[str, Any],
) -> None:
    marker_descriptor = os.open(
        destination["marker_name"],
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=descriptor,
    )
    try:
        os.fchmod(marker_descriptor, 0o600)
        _ensure_fd_owner(
            marker_descriptor,
            expected_uid=destination["owner_uid"],
            expected_gid=destination["owner_gid"],
            label="destination marker",
        )
        marker_payload = (
            "robodata-contract-partition-v1:"
            f"{destination['marker_name']}\n"
        ).encode("ascii")
        _write_all(marker_descriptor, marker_payload)
        os.fsync(marker_descriptor)
    finally:
        os.close(marker_descriptor)


def _prepare_destination(
    destination: dict[str, Any],
    *,
    source: dict[str, Any],
    strategy: str,
) -> None:
    path = Path(destination["path"])
    construction = Path(destination["construction"])
    path_info = _path_presence(path)
    construction_info = _path_presence(construction)
    if path_info is not None and construction_info is not None:
        raise RuntimeError("destination and construction both exist")

    if path_info is None:
        if construction_info is None:
            parent_descriptor = _open_directory_chain_nofollow(path.parent)
            try:
                os.mkdir(
                    construction.name,
                    mode=destination["working_mode"],
                    dir_fd=parent_descriptor,
                )
                descriptor = os.open(
                    construction.name,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent_descriptor,
                )
                try:
                    os.fchmod(descriptor, destination["working_mode"])
                    _ensure_fd_owner(
                        descriptor,
                        expected_uid=source["uid"],
                        expected_gid=source["gid"],
                        label="destination construction",
                    )
                    _create_destination_marker(descriptor, destination)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
        else:
            descriptor = _open_directory_chain_nofollow(construction)
            try:
                construction_root = os.fstat(descriptor)
                entries = set(os.listdir(descriptor))
                if (
                    _mode(construction_root) != destination["working_mode"]
                    or construction_root.st_uid != source["uid"]
                    or construction_root.st_gid != source["gid"]
                ):
                    raise RuntimeError(
                        "destination construction contains foreign entries"
                    )
                if not entries:
                    _create_destination_marker(descriptor, destination)
                    os.fsync(descriptor)
                elif entries != {destination["marker_name"]}:
                    raise RuntimeError(
                        "destination construction contains foreign entries"
                    )
                _validate_destination_marker(
                    descriptor,
                    destination,
                    require_bound_identity=False,
                )
            finally:
                os.close(descriptor)
        _rename_partition_noreplace(
            construction,
            path,
            strategy=strategy,
        )
    elif not stat.S_ISDIR(path_info.st_mode):
        raise FileExistsError(f"destination is not a plain directory: {path}")

    descriptor = _open_destination(
        destination,
        require_bound_identity=destination["device"] is not None,
        allowed_modes=(
            None
            if destination["device"] is None
            else {destination["working_mode"]}
        ),
    )
    try:
        root_info = os.fstat(descriptor)
        if destination["device"] is None:
            if (
                _mode(root_info) != destination["working_mode"]
                or root_info.st_uid != source["uid"]
                or root_info.st_gid != source["gid"]
                or set(os.listdir(descriptor))
                != {destination["marker_name"]}
            ):
                raise FileExistsError(
                    f"destination exists with foreign content: {path}"
                )
            marker = _validate_destination_marker(
                descriptor,
                destination,
                require_bound_identity=False,
            )
            destination.update(
                {
                    "device": root_info.st_dev,
                    "inode": root_info.st_ino,
                    "mode": _mode(root_info),
                    "uid": root_info.st_uid,
                    "gid": root_info.st_gid,
                    "marker_device": marker.st_dev,
                    "marker_inode": marker.st_ino,
                }
            )
        else:
            _destination_marker_info(
                descriptor,
                destination,
                required=True,
            )
    finally:
        os.close(descriptor)


def _recording_present(root_descriptor: int, serial: str) -> bool:
    try:
        os.stat(serial, dir_fd=root_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _validate_live_state(
    journal: dict[str, Any],
    *,
    expected_phase: str | None,
) -> dict[str, str]:
    source = journal["source"]
    phase = journal["phase"]
    marker_required = phase in {"reserved", "applying"}
    marker_forbidden = phase in {"committed", "rolled_back"}
    if phase in {"reserved", "applying"}:
        allowed_destination_modes = {
            destination["working_mode"]
            for destination in journal["destinations"].values()
        }
    elif phase in {"finalizing", "rolling_back"}:
        allowed_destination_modes = {
            mode
            for destination in journal["destinations"].values()
            for mode in (
                destination["working_mode"],
                destination["committed_mode"],
            )
        }
    else:
        allowed_destination_modes = {
            destination["committed_mode"]
            for destination in journal["destinations"].values()
        }
    source_path = Path(source["path"])
    source_parent_descriptor = _open_directory_chain_nofollow(
        source_path.parent
    )
    try:
        source_descriptor = _open_directory_chain_nofollow(source_path)
        destination_descriptors: dict[str, int] = {}
        try:
            source_parent_info = os.fstat(source_parent_descriptor)
            if (
                source_parent_info.st_dev,
                source_parent_info.st_ino,
                _mode(source_parent_info),
                source_parent_info.st_uid,
                source_parent_info.st_gid,
            ) != (
                source["parent_device"],
                source["parent_inode"],
                source["parent_mode"],
                source["parent_uid"],
                source["parent_gid"],
            ):
                raise RuntimeError("source parent identity changed")
            source_info = os.fstat(source_descriptor)
            if not _matches_directory(source_info, source):
                raise RuntimeError("source task identity changed")
            preserved = {
                entry["name"]: entry for entry in source["preserved_entries"]
            }
            for entry in preserved.values():
                _validate_preserved_entry(source_descriptor, entry)

            for digest, destination in journal["destinations"].items():
                path = Path(destination["path"])
                if destination["device"] is None:
                    if _path_presence(path) is not None:
                        raise RuntimeError("unbound destination appeared")
                    continue
                descriptor = _open_destination(
                    destination,
                    require_bound_identity=True,
                    allowed_modes=allowed_destination_modes,
                )
                marker = _destination_marker_info(
                    descriptor,
                    destination,
                    required=marker_required,
                )
                if marker_forbidden and marker is not None:
                    raise RuntimeError(
                        "terminal destination still has an ownership marker"
                    )
                destination_descriptors[digest] = descriptor

            locations: dict[str, str] = {}
            keep_digest = journal["keep_digest"]
            for serial, recording in source["recordings"].items():
                digest = journal["recording_digests"][serial]
                source_present = _recording_present(
                    source_descriptor,
                    serial,
                )
                destination_present: list[str] = []
                for (
                    candidate_digest,
                    descriptor,
                ) in destination_descriptors.items():
                    if _recording_present(descriptor, serial):
                        destination_present.append(candidate_digest)
                if int(source_present) + len(destination_present) != 1:
                    raise RuntimeError(
                        "recording is missing or duplicated across partitions: "
                        f"{serial}"
                    )
                if source_present:
                    _validate_recording_at(
                        source_descriptor,
                        serial,
                        recording,
                    )
                    locations[serial] = "source"
                else:
                    actual_digest = destination_present[0]
                    if actual_digest != digest or digest == keep_digest:
                        raise RuntimeError(
                            "recording exists in the wrong contract "
                            f"destination: {serial}"
                        )
                    _validate_recording_at(
                        destination_descriptors[actual_digest],
                        serial,
                        recording,
                    )
                    locations[serial] = actual_digest

            source_expected_entries = set(preserved) | {
                serial
                for serial, location in locations.items()
                if location == "source"
            }
            if set(os.listdir(source_descriptor)) != source_expected_entries:
                raise RuntimeError("source namespace contains foreign entries")
            for digest, descriptor in destination_descriptors.items():
                marker = journal["destinations"][digest]["marker_name"]
                expected_entries = {
                    serial
                    for serial, location in locations.items()
                    if location == digest
                }
                if _recording_present(descriptor, marker):
                    expected_entries.add(marker)
                if set(os.listdir(descriptor)) != expected_entries:
                    raise RuntimeError(
                        "destination namespace contains foreign entries"
                    )

            if expected_phase == "committed":
                for serial, digest in journal["recording_digests"].items():
                    expected = "source" if digest == keep_digest else digest
                    if locations[serial] != expected:
                        raise RuntimeError(
                            "committed partition does not match contract"
                        )
            elif expected_phase == "rolled_back" and any(
                location != "source" for location in locations.values()
            ):
                raise RuntimeError(
                    "rolled-back partition did not restore source"
                )
            return locations
        finally:
            for descriptor in destination_descriptors.values():
                os.close(descriptor)
            os.close(source_descriptor)
    finally:
        os.close(source_parent_descriptor)


def _finalize_destinations(journal: dict[str, Any]) -> None:
    for destination in journal["destinations"].values():
        if destination["device"] is None:
            continue
        descriptor = _open_destination(
            destination,
            require_bound_identity=True,
            allowed_modes={
                destination["working_mode"],
                destination["committed_mode"],
            },
        )
        try:
            marker = _destination_marker_info(
                descriptor,
                destination,
                required=False,
            )
            if marker is not None:
                path_marker = os.stat(
                    destination["marker_name"],
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                if _stat_identity(path_marker) != _stat_identity(marker):
                    raise RuntimeError(
                        "destination marker path changed before finalization"
                    )
                os.unlink(destination["marker_name"], dir_fd=descriptor)
            os.fchmod(descriptor, destination["committed_mode"])
            os.fsync(descriptor)
            updated = os.fstat(descriptor)
            destination["mode"] = _mode(updated)
            if destination["mode"] != destination["committed_mode"]:
                raise RuntimeError("destination committed mode was not installed")
        finally:
            os.close(descriptor)
        parent_descriptor = _open_directory_chain_nofollow(
            Path(destination["path"]).parent
        )
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)


def _validate_loaded_journal(
    journal: dict[str, Any],
    *,
    source_task: Path,
    contract_manifest_path: Path,
    contract_identity: dict[str, int],
    contract_sha256: str,
    journal_parent_identity: dict[str, Any],
    keep_digest: str,
    destinations: dict[str, str],
) -> None:
    if (
        journal.get("version") != _JOURNAL_VERSION
        or journal.get("operation") != _JOURNAL_OPERATION
        or journal.get("phase") not in _JOURNAL_PHASES
        or journal.get("source", {}).get("path") != str(source_task)
        or journal.get("journal_parent") != journal_parent_identity
        or journal.get("contract_manifest", {}).get("path")
        != str(contract_manifest_path)
        or journal.get("contract_manifest", {}).get("identity")
        != contract_identity
        or journal.get("contract_manifest", {}).get("sha256")
        != contract_sha256
        or journal.get("keep_digest") != keep_digest
        or journal.get("rename_strategy") not in _RENAME_STRATEGIES
        or journal.get("plan_sha256") != _plan_sha256(journal)
        or {
            digest: destination["path"]
            for digest, destination in journal.get("destinations", {}).items()
        }
        != destinations
    ):
        raise RuntimeError("partition journal identity or invocation changed")


@contextmanager
def _partition_lock(
    source_task: Path,
    *,
    expected_uid: int,
    expected_gid: int,
) -> Iterator[None]:
    lock_path = source_task.with_name(
        f".{source_task.name}.contract-partition.lock"
    )
    parent_descriptor = _open_directory_chain_nofollow(lock_path.parent)
    descriptor = -1
    try:
        try:
            descriptor = os.open(
                lock_path.name,
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent_descriptor,
            )
        except FileExistsError:
            probe_descriptor = os.open(
                lock_path.name,
                getattr(os, "O_PATH", os.O_RDONLY)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_descriptor,
            )
            try:
                probe_info = os.fstat(probe_descriptor)
                if (
                    not stat.S_ISREG(probe_info.st_mode)
                    or probe_info.st_nlink != 1
                    or _mode(probe_info) != 0o600
                    or probe_info.st_uid != expected_uid
                    or probe_info.st_gid != expected_gid
                ):
                    raise RuntimeError(
                        "partition lock is not an owned regular file"
                    )
                descriptor = os.open(
                    lock_path.name,
                    os.O_RDWR
                    | getattr(os, "O_NONBLOCK", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent_descriptor,
                )
                info = os.fstat(descriptor)
                if _stat_identity(info) != _stat_identity(probe_info):
                    raise RuntimeError(
                        "partition lock identity changed while opening"
                    )
            finally:
                os.close(probe_descriptor)
        else:
            os.fchmod(descriptor, 0o600)
            info = _ensure_fd_owner(
                descriptor,
                expected_uid=expected_uid,
                expected_gid=expected_gid,
                label="partition lock",
            )
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or _mode(info) != 0o600
            or info.st_uid != expected_uid
            or info.st_gid != expected_gid
        ):
            raise RuntimeError("partition lock is not an owned regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)


def _journal_identity(identity: dict[str, int]) -> tuple[int, int]:
    return identity["device"], identity["inode"]


def apply_partition(
    source_task: Path,
    contract_manifest_path: Path,
    journal_path: Path,
    keep_digest: str,
    destinations: dict[str, Path],
    *,
    fault_after_moves: int | None = None,
) -> dict[str, Any]:
    """Apply or resume an exact same-filesystem contract partition."""
    source_task = _absolute(Path(source_task))
    contract_manifest_path = _absolute(Path(contract_manifest_path))
    journal_path = _absolute(Path(journal_path))
    source_info = _plain_directory_info(source_task)
    contract, contract_identity, contract_sha256 = _read_private_json(
        contract_manifest_path
    )
    partitions = _validate_contract_manifest(
        contract,
        source_task=source_task,
    )
    normalized_destinations = _normalize_destinations(
        source_task=source_task,
        partitions=partitions,
        keep_digest=keep_digest,
        destinations=destinations,
    )
    if (
        contract_manifest_path == journal_path
        or journal_path == source_task
        or journal_path.is_relative_to(source_task)
        or any(
            journal_path == Path(path)
            or journal_path.is_relative_to(Path(path))
            for path in normalized_destinations.values()
        )
    ):
        raise ValueError("journal must be outside source and destination trees")
    journal_parent_descriptor, journal_parent_info = _private_journal_parent(
        journal_path.parent,
        expected_uid=source_info.st_uid,
        expected_gid=source_info.st_gid,
    )
    os.close(journal_parent_descriptor)
    journal_parent_identity = {
        "path": str(journal_path.parent),
        **_directory_inventory(journal_parent_info),
    }
    rename_strategy = _rename_strategy(source_task)

    with _partition_lock(
        source_task,
        expected_uid=source_info.st_uid,
        expected_gid=source_info.st_gid,
    ):
        try:
            journal, journal_file_identity, _, _ = _read_journal(
                journal_path,
                expected_uid=source_info.st_uid,
                expected_gid=source_info.st_gid,
            )
        except (
            FileNotFoundError,
            _IncompleteJournalBootstrap,
            ValueError,
        ):
            for path_value in normalized_destinations.values():
                path = Path(path_value)
                if _path_presence(path) is not None:
                    raise FileExistsError(
                        f"destination already exists; refusing clobber: {path}"
                    )
            expected_serials = {
                serial for serials in partitions.values() for serial in serials
            }
            source = _inventory_source(source_task, expected_serials)
            if (
                source["device"],
                source["inode"],
                source["mode"],
                source["uid"],
                source["gid"],
            ) != (
                source_info.st_dev,
                source_info.st_ino,
                _mode(source_info),
                source_info.st_uid,
                source_info.st_gid,
            ):
                raise RuntimeError("source identity changed during reservation")
            token = secrets.token_hex(32)
            journal = {
                "version": _JOURNAL_VERSION,
                "operation": _JOURNAL_OPERATION,
                "phase": "reserved",
                "created_at": _utc_now(),
                "token": token,
                "journal_parent": journal_parent_identity,
                "contract_manifest": {
                    "path": str(contract_manifest_path),
                    "identity": contract_identity,
                    "sha256": contract_sha256,
                },
                "keep_digest": keep_digest,
                "partitions": partitions,
                "recording_digests": {
                    serial: digest
                    for digest, serials in partitions.items()
                    for serial in serials
                },
                "rename_strategy": rename_strategy,
                "source": source,
                "destinations": {
                    digest: _new_destination_record(
                        Path(path),
                        hashlib.sha256(
                            f"{token}:{digest}".encode("utf-8")
                        ).hexdigest(),
                        committed_mode=source["mode"],
                        owner_uid=source["uid"],
                        owner_gid=source["gid"],
                    )
                    for digest, path in normalized_destinations.items()
                },
            }
            journal_file_identity = _write_journal(
                journal_path,
                journal,
                expected_identity=None,
                expected_owner_uid=source_info.st_uid,
                expected_owner_gid=source_info.st_gid,
                rename_strategy=rename_strategy,
            )
        else:
            _validate_loaded_journal(
                journal,
                source_task=source_task,
                contract_manifest_path=contract_manifest_path,
                contract_identity=contract_identity,
                contract_sha256=contract_sha256,
                journal_parent_identity=journal_parent_identity,
                keep_digest=keep_digest,
                destinations=normalized_destinations,
            )
            if journal["rename_strategy"] != rename_strategy:
                raise RuntimeError(
                    "current filesystem rename strategy differs from journal"
                )
            journal_file_identity = _journal_identity(journal_file_identity)

        if journal["phase"] == "committed":
            _validate_live_state(journal, expected_phase="committed")
            return journal
        if journal["phase"] in {"rolling_back", "rolled_back"}:
            raise RuntimeError(
                "partition journal is in rollback direction; apply cannot resume it"
            )

        if journal["phase"] in {"reserved", "applying"}:
            for destination in journal["destinations"].values():
                _prepare_destination(
                    destination,
                    source=journal["source"],
                    strategy=journal["rename_strategy"],
                )
            journal["phase"] = "applying"
            journal.pop("rolled_back_at", None)
            journal_file_identity = _write_journal(
                journal_path,
                journal,
                expected_identity=journal_file_identity,
                expected_owner_uid=source_info.st_uid,
                expected_owner_gid=source_info.st_gid,
                rename_strategy=journal["rename_strategy"],
            )
            locations = _validate_live_state(journal, expected_phase=None)

            moved = 0
            for digest in sorted(journal["partitions"]):
                if digest == keep_digest:
                    continue
                for serial in journal["partitions"][digest]:
                    if locations[serial] == digest:
                        continue
                    if locations[serial] != "source":
                        raise RuntimeError(
                            f"recording is in an unexpected partition: {serial}"
                        )
                    _rename_recording_noreplace(
                        source_root=journal["source"],
                        destination_root=journal["destinations"][digest],
                        serial=serial,
                        recording=journal["source"]["recordings"][serial],
                        strategy=journal["rename_strategy"],
                    )
                    locations[serial] = digest
                    moved += 1
                    if (
                        fault_after_moves is not None
                        and moved >= fault_after_moves
                    ):
                        raise RuntimeError(
                            "injected partition interruption fault"
                        )

            _validate_live_state(journal, expected_phase="committed")
            journal["phase"] = "finalizing"
            journal_file_identity = _write_journal(
                journal_path,
                journal,
                expected_identity=journal_file_identity,
                expected_owner_uid=source_info.st_uid,
                expected_owner_gid=source_info.st_gid,
                rename_strategy=journal["rename_strategy"],
            )

        _validate_live_state(journal, expected_phase="committed")
        _finalize_destinations(journal)
        _validate_live_state(journal, expected_phase="committed")
        journal["phase"] = "committed"
        journal["committed_at"] = _utc_now()
        journal.pop("rolled_back_at", None)
        _write_journal(
            journal_path,
            journal,
            expected_identity=journal_file_identity,
            expected_owner_uid=source_info.st_uid,
            expected_owner_gid=source_info.st_gid,
            rename_strategy=journal["rename_strategy"],
        )
        return journal


def rollback_partition(
    source_task: Path,
    journal_path: Path,
) -> dict[str, Any]:
    """Resume an explicit reverse-rename rollback without deleting artifacts."""
    source_task = _absolute(Path(source_task))
    journal_path = _absolute(Path(journal_path))
    source_info = _plain_directory_info(source_task)
    current_strategy = _rename_strategy(source_task)
    journal_parent_descriptor, journal_parent_info = _private_journal_parent(
        journal_path.parent,
        expected_uid=source_info.st_uid,
        expected_gid=source_info.st_gid,
    )
    os.close(journal_parent_descriptor)
    current_journal_parent = {
        "path": str(journal_path.parent),
        **_directory_inventory(journal_parent_info),
    }
    with _partition_lock(
        source_task,
        expected_uid=source_info.st_uid,
        expected_gid=source_info.st_gid,
    ):
        try:
            journal, journal_identity_payload, _, _ = _read_journal(
                journal_path,
                expected_uid=source_info.st_uid,
                expected_gid=source_info.st_gid,
            )
        except (
            FileNotFoundError,
            _IncompleteJournalBootstrap,
            ValueError,
        ):
            bootstrap = _read_bootstrap_payload(
                journal_path,
                expected_uid=source_info.st_uid,
                expected_gid=source_info.st_gid,
            )
            _write_journal(
                journal_path,
                bootstrap,
                expected_identity=None,
                expected_owner_uid=source_info.st_uid,
                expected_owner_gid=source_info.st_gid,
                rename_strategy=current_strategy,
            )
            journal, journal_identity_payload, _, _ = _read_journal(
                journal_path,
                expected_uid=source_info.st_uid,
                expected_gid=source_info.st_gid,
            )
        journal_identity = _journal_identity(journal_identity_payload)
        if (
            journal.get("version") != _JOURNAL_VERSION
            or journal.get("operation") != _JOURNAL_OPERATION
            or journal.get("phase") not in _JOURNAL_PHASES
            or journal.get("source", {}).get("path") != str(source_task)
            or journal.get("journal_parent") != current_journal_parent
            or journal.get("rename_strategy") != current_strategy
            or journal.get("plan_sha256") != _plan_sha256(journal)
        ):
            raise RuntimeError("rollback journal does not match source task")
        if journal["phase"] == "rolled_back":
            _validate_live_state(journal, expected_phase="rolled_back")
            return journal

        adopted_destination = False
        for destination in journal["destinations"].values():
            if destination["device"] is not None:
                continue
            if (
                _path_presence(Path(destination["path"])) is None
                and _path_presence(Path(destination["construction"])) is None
            ):
                continue
            _prepare_destination(
                destination,
                source=journal["source"],
                strategy=journal["rename_strategy"],
            )
            adopted_destination = True
        if adopted_destination:
            journal_identity = _write_journal(
                journal_path,
                journal,
                expected_identity=journal_identity,
                expected_owner_uid=source_info.st_uid,
                expected_owner_gid=source_info.st_gid,
                rename_strategy=journal["rename_strategy"],
            )

        _validate_live_state(journal, expected_phase=None)
        journal["phase"] = "rolling_back"
        journal_identity = _write_journal(
            journal_path,
            journal,
            expected_identity=journal_identity,
            expected_owner_uid=source_info.st_uid,
            expected_owner_gid=source_info.st_gid,
            rename_strategy=journal["rename_strategy"],
        )
        locations = _validate_live_state(journal, expected_phase=None)
        keep_digest = journal["keep_digest"]
        for digest in sorted(journal["partitions"], reverse=True):
            if digest == keep_digest:
                continue
            for serial in reversed(journal["partitions"][digest]):
                if locations[serial] == "source":
                    continue
                if locations[serial] != digest:
                    raise RuntimeError(
                        f"recording is in an unexpected partition: {serial}"
                    )
                _rename_recording_noreplace(
                    source_root=journal["destinations"][digest],
                    destination_root=journal["source"],
                    serial=serial,
                    recording=journal["source"]["recordings"][serial],
                    strategy=journal["rename_strategy"],
                )
                locations[serial] = "source"
        _validate_live_state(journal, expected_phase="rolled_back")
        _finalize_destinations(journal)
        _validate_live_state(journal, expected_phase="rolled_back")
        journal["phase"] = "rolled_back"
        journal["rolled_back_at"] = _utc_now()
        journal.pop("committed_at", None)
        _write_journal(
            journal_path,
            journal,
            expected_identity=journal_identity,
            expected_owner_uid=source_info.st_uid,
            expected_owner_gid=source_info.st_gid,
            rename_strategy=journal["rename_strategy"],
        )
        return journal


def _parse_destination(value: str) -> tuple[str, Path]:
    digest, separator, path = value.partition("=")
    if not separator or not path:
        raise argparse.ArgumentTypeError(
            "destination must use DIGEST=/absolute/sibling/task"
        )
    try:
        _validate_digest(digest)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return digest, Path(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("source_task", type=Path)
    apply_parser.add_argument("contract_manifest", type=Path)
    apply_parser.add_argument("journal", type=Path)
    apply_parser.add_argument("keep_digest")
    apply_parser.add_argument(
        "--destination",
        action="append",
        type=_parse_destination,
        default=[],
        metavar="DIGEST=PATH",
    )

    rollback_parser = subparsers.add_parser("rollback")
    rollback_parser.add_argument("source_task", type=Path)
    rollback_parser.add_argument("journal", type=Path)

    args = parser.parse_args()
    if args.command == "apply":
        destination_pairs = args.destination
        destinations = dict(destination_pairs)
        if len(destinations) != len(destination_pairs):
            parser.error("destination digest appears more than once")
        result = apply_partition(
            args.source_task,
            args.contract_manifest,
            args.journal,
            args.keep_digest,
            destinations,
        )
    else:
        result = rollback_partition(args.source_task, args.journal)
    print(
        json.dumps(
            {
                "phase": result["phase"],
                "source_task": result["source"]["path"],
                "recordings": len(result["recording_digests"]),
                "partitions": len(result["partitions"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
