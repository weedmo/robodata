#!/usr/bin/env python3
"""Safely reconcile convert_state.json after a committed raw contract split.

This command is intentionally offline.  The caller must stop every converter
writer before invoking it.  Authority comes from the committed partition
journal and its append-only state log, not from caller-supplied serial lists.
"""

from __future__ import annotations

import argparse
import copy
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

import pyarrow as pa
import pyarrow.parquet as pq

from scripts.partition_raw_by_contract import (
    _open_directory_chain_nofollow,
    _read_journal,
    _validate_live_state,
)


_SERIAL_RECONCILE_SCHEMA = "robodata-partition-state-reconcile/v1"
_STATE_NAME = "convert_state.json"


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _mode(info: os.stat_result) -> int:
    return stat.S_IMODE(info.st_mode)


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _task_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or value != path.as_posix()
        or path.is_absolute()
        or len(path.parts) < 2
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"task must be a canonical relative path: {value!r}")
    return value


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _read_regular_at(
    parent_fd: int,
    name: str,
) -> tuple[bytes, os.stat_result]:
    descriptor = os.open(
        name,
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    try:
        before = os.fstat(descriptor)
        visible = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or _identity(before) != _identity(visible)
        ):
            raise ValueError(f"file must be a single-link regular file: {name}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        visible_after = os.stat(
            name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            _identity(before) != _identity(after)
            or _identity(after) != _identity(visible_after)
            or before.st_size != len(payload)
        ):
            raise RuntimeError(f"file changed while reading: {name}")
        return payload, before
    finally:
        os.close(descriptor)


def _decode_state(payload: bytes) -> dict[str, Any]:
    try:
        state = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("convert_state.json is invalid JSON") from exc
    if not isinstance(state, dict):
        raise ValueError("convert_state.json must be a JSON object")
    if any(not isinstance(key, str) or not key for key in state):
        raise ValueError("convert_state.json contains an invalid task key")
    return state


def _open_task_directory(root_fd: int, task: str) -> int | None:
    descriptor = os.dup(root_fd)
    try:
        for part in PurePosixPath(task).parts:
            try:
                child = os.open(
                    part,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                os.close(descriptor)
                return None
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _episode_parquet_fds(dataset_fd: int) -> list[tuple[str, int]]:
    meta_fd = -1
    episodes_fd = -1
    try:
        meta_fd = os.open(
            "meta",
            os.O_RDONLY
            | os.O_DIRECTORY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=dataset_fd,
        )
        episodes_fd = os.open(
            "episodes",
            os.O_RDONLY
            | os.O_DIRECTORY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=meta_fd,
        )
    except FileNotFoundError as exc:
        if episodes_fd >= 0:
            os.close(episodes_fd)
        if meta_fd >= 0:
            os.close(meta_fd)
        raise ValueError(
            "existing destination output has no meta/episodes directory"
        ) from exc
    os.close(meta_fd)

    opened: list[tuple[str, int]] = []

    def visit(directory_fd: int, prefix: str) -> None:
        for name in sorted(os.listdir(directory_fd)):
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            relative = f"{prefix}/{name}" if prefix else name
            if stat.S_ISDIR(info.st_mode):
                child_fd = os.open(
                    name,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
                try:
                    if _identity(os.fstat(child_fd)) != _identity(info):
                        raise RuntimeError(
                            f"episode metadata directory changed: {relative}"
                        )
                    visit(child_fd, relative)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(info.st_mode) and name.endswith(".parquet"):
                file_fd = os.open(
                    name,
                    os.O_RDONLY
                    | getattr(os, "O_NONBLOCK", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
                if _identity(os.fstat(file_fd)) != _identity(info):
                    os.close(file_fd)
                    raise RuntimeError(
                        f"episode metadata file changed: {relative}"
                    )
                opened.append((relative, file_fd))
            else:
                raise ValueError(
                    f"unexpected episode metadata entry: {relative}"
                )

    try:
        visit(episodes_fd, "")
        if not opened:
            raise ValueError("existing destination output has no episode parquet")
        return opened
    except BaseException:
        for _, descriptor in opened:
            os.close(descriptor)
        raise
    finally:
        os.close(episodes_fd)


def _durable_serials(lerobot_fd: int, task: str) -> list[str] | None:
    dataset_fd = _open_task_directory(lerobot_fd, task)
    if dataset_fd is None:
        return None
    parquet_fds = _episode_parquet_fds(dataset_fd)
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
                    raise ValueError(
                        f"cannot read Serial_number from {relative}"
                    ) from exc
            after = os.fstat(descriptor)
            if (
                _identity(before) != _identity(after)
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
            ):
                raise RuntimeError(
                    f"episode metadata changed while reading: {relative}"
                )
            for value in table.column("Serial_number").to_pylist():
                if not isinstance(value, str) or not value:
                    raise ValueError(
                        f"invalid Serial_number in {relative}"
                    )
                serials.append(value)
    finally:
        for _, descriptor in parquet_fds:
            os.close(descriptor)
        os.close(dataset_fd)
    if len(serials) != len(set(serials)):
        raise ValueError(f"destination output has duplicate serials: {task}")
    return sorted(serials)


def _validate_failure_fields(
    entry: dict[str, Any],
    *,
    task: str,
) -> tuple[list[str], dict[str, Any]]:
    failed = entry.get("failed_serials", [])
    transient = entry.get("transient_failed", {})
    if (
        not isinstance(failed, list)
        or any(not isinstance(serial, str) or not serial for serial in failed)
    ):
        raise ValueError(f"{task} failed_serials is invalid")
    if transient is None:
        transient = {}
    if (
        not isinstance(transient, dict)
        or any(not isinstance(serial, str) or not serial for serial in transient)
    ):
        raise ValueError(f"{task} transient_failed is invalid")
    return failed, transient


def _derive_replacement(
    original: dict[str, Any],
    *,
    journal: dict[str, Any],
    source_task: str,
    destination_tasks: dict[str, str],
    durable_by_task: dict[str, list[str] | None],
    updated_at: str,
) -> dict[str, Any]:
    replacement = copy.deepcopy(original)
    moved_serials = {
        serial
        for digest, serials in journal["partitions"].items()
        if digest != journal["keep_digest"]
        for serial in serials
    }
    durable_source = durable_by_task[source_task]
    source_entry = replacement.get(source_task)
    if source_entry is None and durable_source is not None:
        source_entry = {}
        replacement[source_task] = source_entry
    if source_entry is not None:
        if not isinstance(source_entry, dict):
            raise ValueError(f"{source_task} state entry is not an object")
        failed, transient = _validate_failure_fields(
            source_entry,
            task=source_task,
        )
        durable_source_set = set(durable_source or ())
        keep_serials = set(journal["partitions"][journal["keep_digest"]])
        if not durable_source_set.issubset(keep_serials):
            raise ValueError(
                f"source output serials are not a raw subset: {source_task}"
            )
        cleared_source = moved_serials | durable_source_set
        source_entry["failed_serials"] = sorted(
            {serial for serial in failed if serial not in cleared_source}
        )
        remaining_transient = {
            serial: value
            for serial, value in transient.items()
            if serial not in cleared_source
        }
        if remaining_transient:
            source_entry["transient_failed"] = remaining_transient
        else:
            source_entry.pop("transient_failed", None)
        if durable_source is not None:
            source_entry["converted_count"] = len(durable_source)
            source_entry["last_serial"] = (
                max(durable_source) if durable_source else ""
            )
        source_entry["last_updated"] = updated_at

    for digest, task in destination_tasks.items():
        raw_serials = set(journal["partitions"][digest])
        durable = durable_by_task[task] or []
        durable_set = set(durable)
        if not durable_set.issubset(raw_serials):
            raise ValueError(
                f"destination output serials are not a raw subset: {task}"
            )
        before_entry = replacement.get(task, {})
        if not isinstance(before_entry, dict):
            raise ValueError(f"{task} state entry is not an object")
        failed, transient = _validate_failure_fields(
            before_entry,
            task=task,
        )
        foreign_failures = (set(failed) | set(transient)) - raw_serials
        if foreign_failures:
            raise ValueError(
                f"{task} state contains failures outside its raw partition"
            )
        after_entry = copy.deepcopy(before_entry)
        after_entry["converted_count"] = len(durable)
        after_entry["failed_serials"] = sorted(
            {serial for serial in failed if serial not in durable_set}
        )
        remaining_transient = {
            serial: value
            for serial, value in transient.items()
            if serial not in durable_set
        }
        if remaining_transient:
            after_entry["transient_failed"] = remaining_transient
        else:
            after_entry.pop("transient_failed", None)
        after_entry["last_serial"] = max(durable) if durable else ""
        after_entry["last_updated"] = updated_at
        replacement[task] = after_entry
    return replacement


def _validate_authority(
    *,
    raw_root: Path,
    journal_path: Path,
    source_task: str,
    destination_tasks: dict[str, str],
    expected_journal_uid: int,
    expected_journal_gid: int,
) -> dict[str, Any]:
    source_path = raw_root / PurePosixPath(source_task)
    source_fd = _open_directory_chain_nofollow(source_path)
    try:
        _open_fd_info(source_fd)
    finally:
        os.close(source_fd)
    journal, _, _, _ = _read_journal(
        journal_path,
        expected_uid=expected_journal_uid,
        expected_gid=expected_journal_gid,
    )
    expected_destinations = {
        digest: str(raw_root / PurePosixPath(task))
        for digest, task in destination_tasks.items()
    }
    actual_destinations = {
        digest: destination.get("path")
        for digest, destination in journal.get("destinations", {}).items()
    }
    if (
        journal.get("phase") != "committed"
        or journal.get("source", {}).get("path") != str(source_path)
        or actual_destinations != expected_destinations
        or set(destination_tasks) != (
            set(journal.get("partitions", {})) - {journal.get("keep_digest")}
        )
    ):
        raise RuntimeError(
            "committed partition journal does not match exact source/destinations"
        )
    _validate_live_state(journal, expected_phase="committed")
    return journal


def _backup_name(journal: dict[str, Any]) -> str:
    plan = journal.get("plan_sha256")
    if (
        not isinstance(plan, str)
        or len(plan) != 64
        or any(character not in "0123456789abcdef" for character in plan)
    ):
        raise RuntimeError("partition journal plan digest is invalid")
    return f"{_STATE_NAME}.partition-reconcile-{plan}.bak"


@contextmanager
def _state_lock(lerobot_fd: int, state_info: os.stat_result) -> Iterator[None]:
    name = f".{_STATE_NAME}.partition-reconcile.lock"
    flags = (
        os.O_RDWR
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    created = False
    try:
        descriptor = os.open(
            name,
            flags | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=lerobot_fd,
        )
        created = True
    except FileExistsError:
        descriptor = os.open(name, flags, dir_fd=lerobot_fd)
    lock_identity: tuple[int, int] | None = None
    try:
        info = _open_fd_info(descriptor)
        lock_identity = _identity(info)
        visible = os.stat(name, dir_fd=lerobot_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or _identity(info) != _identity(visible)
            or info.st_uid != state_info.st_uid
            or info.st_gid != state_info.st_gid
        ):
            raise PermissionError("state lock identity or owner is unsafe")
        if created:
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            os.fsync(lerobot_fd)
            info = _open_fd_info(descriptor)
            visible = os.stat(
                name,
                dir_fd=lerobot_fd,
                follow_symlinks=False,
            )
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or _identity(info) != lock_identity
            or _identity(visible) != lock_identity
            or _mode(info) != 0o600
            or _mode(visible) != 0o600
            or info.st_uid != state_info.st_uid
            or info.st_gid != state_info.st_gid
        ):
            raise PermissionError("state lock identity, owner, or mode is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except BaseException:
        if created and lock_identity is not None:
            _unlink_if_identity(lerobot_fd, name, lock_identity)
        os.close(descriptor)
        raise
    try:
        yield
    finally:
        os.close(descriptor)


def _open_fd_info(descriptor: int) -> os.stat_result:
    """Stat an open descriptor without relying on os.fstat."""
    return os.stat(f"/proc/self/fd/{descriptor}")


def _write_new_regular(
    parent_fd: int,
    name: str,
    payload: bytes,
    *,
    template: os.stat_result,
) -> tuple[int, int]:
    descriptor = os.open(
        name,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        _mode(template),
        dir_fd=parent_fd,
    )
    created_identity: tuple[int, int] | None = None
    try:
        created_info = _open_fd_info(descriptor)
        created_identity = _identity(created_info)
        os.fchmod(descriptor, _mode(template))
        if (created_info.st_uid, created_info.st_gid) != (
            template.st_uid,
            template.st_gid,
        ):
            os.fchown(descriptor, template.st_uid, template.st_gid)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("short write while persisting state artifact")
            offset += written
        os.fsync(descriptor)
        info = _open_fd_info(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or _mode(info) != _mode(template)
            or info.st_uid != template.st_uid
            or info.st_gid != template.st_gid
        ):
            raise RuntimeError("state artifact metadata was not preserved")
        os.fsync(parent_fd)
        return created_identity
    except BaseException:
        if created_identity is None:
            try:
                created_identity = _identity(_open_fd_info(descriptor))
            except OSError:
                pass
        if created_identity is not None:
            _unlink_if_identity(parent_fd, name, created_identity)
        raise
    finally:
        os.close(descriptor)


def _unlink_if_identity(
    parent_fd: int,
    name: str,
    expected_identity: tuple[int, int],
) -> None:
    try:
        visible = os.stat(
            name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    if _identity(visible) != expected_identity:
        return
    os.unlink(name, dir_fd=parent_fd)
    os.fsync(parent_fd)


def _install_bytes(
    lerobot_fd: int,
    payload: bytes,
    *,
    template: os.stat_result,
    token: str,
) -> None:
    temporary = (
        f".{_STATE_NAME}.{token}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    temporary_identity = _write_new_regular(
        lerobot_fd,
        temporary,
        payload,
        template=template,
    )
    try:
        os.replace(
            temporary,
            _STATE_NAME,
            src_dir_fd=lerobot_fd,
            dst_dir_fd=lerobot_fd,
        )
        os.fsync(lerobot_fd)
    except BaseException:
        _unlink_if_identity(
            lerobot_fd,
            temporary,
            temporary_identity,
        )
        raise


def reconcile_partition_state(
    *,
    raw_root: Path,
    lerobot_root: Path,
    journal_path: Path,
    source_task: str,
    destination_tasks: dict[str, str],
) -> dict[str, Any]:
    """Apply or idempotently replay state reconciliation."""
    raw_root = _absolute(Path(raw_root))
    lerobot_root = _absolute(Path(lerobot_root))
    journal_path = _absolute(Path(journal_path))
    source_task = _task_path(source_task)
    destination_tasks = {
        digest: _task_path(task)
        for digest, task in destination_tasks.items()
    }
    if (
        raw_root == lerobot_root
        or raw_root in lerobot_root.parents
        or lerobot_root in raw_root.parents
    ):
        raise ValueError("raw and lerobot roots must be separate trees")
    lerobot_fd = _open_directory_chain_nofollow(lerobot_root)
    try:
        _, state_info = _read_regular_at(
            lerobot_fd,
            _STATE_NAME,
        )
        journal = _validate_authority(
            raw_root=raw_root,
            journal_path=journal_path,
            source_task=source_task,
            destination_tasks=destination_tasks,
            expected_journal_uid=state_info.st_uid,
            expected_journal_gid=state_info.st_gid,
        )
        with _state_lock(lerobot_fd, state_info):
            current_bytes, current_info = _read_regular_at(
                lerobot_fd,
                _STATE_NAME,
            )
            durable_by_task: dict[str, list[str] | None] = {
                source_task: _durable_serials(lerobot_fd, source_task),
                **{
                    task: _durable_serials(lerobot_fd, task)
                    for task in destination_tasks.values()
                },
            }
            backup_name = _backup_name(journal)
            backup_exists = False
            try:
                backup_bytes, backup_info = _read_regular_at(
                    lerobot_fd,
                    backup_name,
                )
                backup_exists = True
            except FileNotFoundError:
                backup_bytes = b""
                backup_info = current_info

            base_bytes = backup_bytes if backup_exists else current_bytes
            base_state = _decode_state(base_bytes)
            updated_at = (
                journal.get("committed_at")
                if isinstance(journal.get("committed_at"), str)
                else datetime.now(timezone.utc).isoformat()
            )
            replacement = _derive_replacement(
                base_state,
                journal=journal,
                source_task=source_task,
                destination_tasks=destination_tasks,
                durable_by_task=durable_by_task,
                updated_at=updated_at,
            )
            replacement_bytes = _json_bytes(replacement)
            if backup_exists:
                if (
                    _mode(backup_info) != _mode(current_info)
                    or backup_info.st_uid != current_info.st_uid
                    or backup_info.st_gid != current_info.st_gid
                ):
                    raise RuntimeError("state backup metadata is inconsistent")
                if current_bytes == replacement_bytes:
                    status = "already_reconciled"
                elif current_bytes == backup_bytes:
                    _install_bytes(
                        lerobot_fd,
                        replacement_bytes,
                        template=backup_info,
                        token=f"reconcile-{journal['plan_sha256']}",
                    )
                    status = "reconciled"
                else:
                    raise RuntimeError(
                        "canonical state differs from both backup and derived state"
                    )
            else:
                _write_new_regular(
                    lerobot_fd,
                    backup_name,
                    current_bytes,
                    template=current_info,
                )
                _install_bytes(
                    lerobot_fd,
                    replacement_bytes,
                    template=current_info,
                    token=f"reconcile-{journal['plan_sha256']}",
                )
                status = "reconciled"

            installed_bytes, installed_info = _read_regular_at(
                lerobot_fd,
                _STATE_NAME,
            )
            if (
                installed_bytes != replacement_bytes
                or _mode(installed_info) != _mode(state_info)
                or installed_info.st_uid != state_info.st_uid
                or installed_info.st_gid != state_info.st_gid
            ):
                raise RuntimeError(
                    "installed state content or metadata verification failed"
                )
            return {
                "schema": _SERIAL_RECONCILE_SCHEMA,
                "status": status,
                "backup": str(lerobot_root / backup_name),
                "state_sha256": hashlib.sha256(installed_bytes).hexdigest(),
                "source_task": source_task,
                "destination_counts": {
                    task: len(durable_by_task[task] or [])
                    for task in destination_tasks.values()
                },
                "source_count": (
                    None
                    if durable_by_task[source_task] is None
                    else len(durable_by_task[source_task] or [])
                ),
            }
    finally:
        os.close(lerobot_fd)


def rollback_partition_state(
    *,
    raw_root: Path,
    lerobot_root: Path,
    journal_path: Path,
    source_task: str,
    destination_tasks: dict[str, str],
) -> dict[str, Any]:
    """Restore the durable no-clobber backup for this exact partition plan."""
    raw_root = _absolute(Path(raw_root))
    lerobot_root = _absolute(Path(lerobot_root))
    journal_path = _absolute(Path(journal_path))
    source_task = _task_path(source_task)
    destination_tasks = {
        digest: _task_path(task)
        for digest, task in destination_tasks.items()
    }
    lerobot_fd = _open_directory_chain_nofollow(lerobot_root)
    try:
        current_bytes, current_info = _read_regular_at(
            lerobot_fd,
            _STATE_NAME,
        )
        journal = _validate_authority(
            raw_root=raw_root,
            journal_path=journal_path,
            source_task=source_task,
            destination_tasks=destination_tasks,
            expected_journal_uid=current_info.st_uid,
            expected_journal_gid=current_info.st_gid,
        )
        with _state_lock(lerobot_fd, current_info):
            current_bytes, current_info = _read_regular_at(
                lerobot_fd,
                _STATE_NAME,
            )
            backup_name = _backup_name(journal)
            backup_bytes, backup_info = _read_regular_at(
                lerobot_fd,
                backup_name,
            )
            if (
                _mode(backup_info) != _mode(current_info)
                or backup_info.st_uid != current_info.st_uid
                or backup_info.st_gid != current_info.st_gid
            ):
                raise RuntimeError("state backup metadata is inconsistent")
            if current_bytes == backup_bytes:
                status = "already_rolled_back"
            else:
                durable_by_task: dict[str, list[str] | None] = {
                    source_task: _durable_serials(lerobot_fd, source_task),
                    **{
                        task: _durable_serials(lerobot_fd, task)
                        for task in destination_tasks.values()
                    },
                }
                replacement = _derive_replacement(
                    _decode_state(backup_bytes),
                    journal=journal,
                    source_task=source_task,
                    destination_tasks=destination_tasks,
                    durable_by_task=durable_by_task,
                    updated_at=journal["committed_at"],
                )
                if current_bytes != _json_bytes(replacement):
                    raise RuntimeError(
                        "canonical state is not the derived reconciled state"
                    )
                _install_bytes(
                    lerobot_fd,
                    backup_bytes,
                    template=backup_info,
                    token=f"rollback-{journal['plan_sha256']}",
                )
                status = "rolled_back"
            installed_bytes, installed_info = _read_regular_at(
                lerobot_fd,
                _STATE_NAME,
            )
            if (
                installed_bytes != backup_bytes
                or _mode(installed_info) != _mode(backup_info)
                or installed_info.st_uid != backup_info.st_uid
                or installed_info.st_gid != backup_info.st_gid
            ):
                raise RuntimeError("state rollback verification failed")
            return {
                "schema": _SERIAL_RECONCILE_SCHEMA,
                "status": status,
                "backup": str(lerobot_root / backup_name),
                "state_sha256": hashlib.sha256(installed_bytes).hexdigest(),
                "source_task": source_task,
            }
    finally:
        os.close(lerobot_fd)


def _destination(value: str) -> tuple[str, str]:
    digest, separator, task = value.partition("=")
    if not separator:
        raise argparse.ArgumentTypeError("destination must use DIGEST=cell/task")
    if (
        len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise argparse.ArgumentTypeError(
            "destination digest must be lowercase SHA-256"
        )
    try:
        return digest, _task_path(task)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "reconcile canonical convert_state.json after a committed raw "
            "contract partition; all converter writers must already be stopped"
        )
    )
    parser.add_argument("command", choices=("apply", "rollback"))
    parser.add_argument("raw_root", type=Path)
    parser.add_argument("lerobot_root", type=Path)
    parser.add_argument("journal", type=Path)
    parser.add_argument("source_task", type=_task_path)
    parser.add_argument(
        "--destination",
        action="append",
        default=[],
        type=_destination,
        metavar="DIGEST=cell/task",
    )
    args = parser.parse_args()
    pairs = args.destination
    destinations = dict(pairs)
    if len(destinations) != len(pairs):
        parser.error("destination digest appears more than once")
    function = (
        reconcile_partition_state
        if args.command == "apply"
        else rollback_partition_state
    )
    result = function(
        raw_root=args.raw_root,
        lerobot_root=args.lerobot_root,
        journal_path=args.journal,
        source_task=args.source_task,
        destination_tasks=destinations,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
