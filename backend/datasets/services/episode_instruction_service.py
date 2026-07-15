"""Crash-recoverable task-instruction mutations for LeRobot datasets."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import fcntl

import pyarrow as pa
import pyarrow.parquet as pq

from backend.datasets.services.task_parquet import get_task_text_column_name


class InstructionConflictError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _files(root: Path, section: str) -> list[Path]:
    return sorted((root / section).glob("chunk-*/file-*.parquet"))


def _manifest_path(root: Path) -> Path:
    return root.parent / f".{root.name}.instruction-transaction.json"


def _write_manifest(root: Path, state: dict[str, str]) -> None:
    path = _manifest_path(root)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(state, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory = os.open(root.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


@contextmanager
def dataset_lock(root: Path):
    """Cross-process lock for preview, recovery, and commit serialization."""
    path = root.parent / f".{root.name}.instruction.lock"
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def recover(root: Path) -> None:
    """Restore the known original dataset after an interrupted transaction."""
    manifest = _manifest_path(root)
    if not manifest.exists():
        return
    try:
        state = json.loads(manifest.read_text())
        backup, staging = Path(state["backup"]), Path(state["staging"])
    except (OSError, ValueError, KeyError) as exc:
        raise InstructionConflictError("instruction_recovery_required", "Invalid instruction transaction manifest") from exc
    token_prefix = f".{root.name}.instruction-"
    if backup.parent != root.parent or staging.parent != root.parent or not backup.name.startswith(f"{token_prefix}backup-") or not staging.name.startswith(f"{token_prefix}staging-"):
        raise InstructionConflictError("instruction_recovery_required", "Instruction transaction manifest paths are invalid")
    if not backup.exists():
        if state.get("phase") == "prepared" and root.exists() and not staging.exists():
            manifest.unlink()
            return
        raise InstructionConflictError("instruction_recovery_required", "Instruction transaction backup is missing")
    if state.get("phase") == "moved" and root.exists() and not staging.exists():
        shutil.rmtree(backup)
        manifest.unlink()
        return
    if root.exists():
        shutil.rmtree(root)
    if staging.exists():
        shutil.rmtree(staging)
    backup.rename(root)
    manifest.unlink()


def _revision(root: Path) -> str:
    entries: list[tuple[str, int, int]] = []
    for path in sorted((root / "meta").rglob("*.parquet")) + sorted((root / "data").rglob("*.parquet")) + [root / "meta/info.json"]:
        if path.exists():
            stat = path.stat()
            entries.append((str(path.relative_to(root)), stat.st_size, stat.st_mtime_ns))
    return hashlib.sha256(json.dumps(entries).encode()).hexdigest()


def _task_table(root: Path) -> tuple[pa.Table, str, list[int], list[str]]:
    table = pq.read_table(root / "meta/tasks.parquet")
    text_column = get_task_text_column_name(table)
    if text_column is None:
        raise ValueError("tasks.parquet has no task text column")
    return table, text_column, [int(v) for v in table.column("task_index").to_pylist()], [str(v) for v in table.column(text_column).to_pylist()]


def _episode_task(table: pa.Table, position: int, lookup: dict[str, int], mode: str) -> int:
    if "task_index" in table.column_names:
        return int(table.column("task_index")[position].as_py())
    if "tasks" not in table.column_names:
        raise InstructionConflictError("multi_task_episode", "Episode has no supported task reference")
    names = table.column("tasks")[position].as_py() or []
    if not isinstance(names, list) or not names:
        raise InstructionConflictError("multi_task_episode", "Episode task list is empty")
    if mode == "episode" and len(names) != 1:
        raise InstructionConflictError("multi_task_episode", "Only-this-episode editing requires exactly one task")
    try:
        return lookup[str(names[0])]
    except KeyError as exc:
        raise InstructionConflictError("multi_task_episode", "Episode refers to an unknown task") from exc


def preview(root: Path, episode_index: int, instruction: str, mode: str) -> dict[str, Any]:
    recover(root)
    table, text_column, indexes, texts = _task_table(root)
    if indexes != list(range(len(indexes))):
        raise InstructionConflictError("task_index_invalid", "tasks.parquet task_index values must be contiguous")
    lookup = {text: index for index, text in zip(indexes, texts, strict=True)}
    selected: tuple[pa.Table, int] | None = None
    for path in _files(root, "meta/episodes"):
        episodes = pq.read_table(path)
        for position, value in enumerate(episodes.column("episode_index").to_pylist()):
            if int(value) == episode_index:
                selected = (episodes, position)
    if selected is None:
        raise KeyError(f"episode {episode_index} not found")
    current = _episode_task(selected[0], selected[1], lookup, mode)
    old_text = texts[indexes.index(current)]
    if mode == "shared" and instruction != old_text and instruction in lookup:
        raise InstructionConflictError("instruction_duplicate", "Shared rename conflicts with an existing task instruction")
    affected = 1 if mode == "episode" else 0
    if mode == "shared":
        for path in _files(root, "meta/episodes"):
            episodes = pq.read_table(path)
            for position in range(episodes.num_rows):
                if "task_index" in episodes.column_names and int(episodes.column("task_index")[position].as_py()) == current:
                    affected += 1
                elif "tasks" in episodes.column_names and old_text in (episodes.column("tasks")[position].as_py() or []):
                    affected += 1
    match = next((index for index, text in zip(indexes, texts, strict=True) if text == instruction), None)
    action = "shared" if mode == "shared" else ("no_op" if texts[indexes.index(current)] == instruction else "reuse" if match is not None else "create")
    result = {"episode_index": episode_index, "mode": mode, "normalized_instruction": instruction, "current_task_index": current, "action": action, "target_task_index": match, "affected_episode_count": affected, "revision": _revision(root)}
    result["fingerprint"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    return result


def _set_column(table: pa.Table, name: str, values: list[Any]) -> pa.Table:
    return table.set_column(table.schema.get_field_index(name), name, pa.array(values, type=table.schema.field(name).type))


def _append_task(table: pa.Table, text_column: str, next_index: int, instruction: str) -> pa.Table:
    columns = {}
    for field in table.schema:
        value: Any = next_index if field.name == "task_index" else instruction if field.name == text_column else None
        columns[field.name] = pa.array([value], type=field.type)
    row = pa.Table.from_arrays(list(columns.values()), schema=table.schema)
    return pa.concat_tables([table, row])


def _compact_task_indexes(root: Path) -> None:
    """Remove unreferenced task rows and remap task-index references densely."""
    task_table, _, indexes, _ = _task_table(root)
    episode_paths = _files(root, "meta/episodes")
    if not episode_paths:
        return
    _, _, _, texts = _task_table(root)
    text_to_index = {text: index for index, text in zip(indexes, texts, strict=True)}
    if all("task_index" in pq.read_schema(path).names for path in episode_paths):
        used = sorted({int(value) for path in episode_paths for value in pq.read_table(path, columns=["task_index"]).column("task_index").to_pylist()})
    elif all("tasks" in pq.read_schema(path).names for path in episode_paths):
        used = sorted({text_to_index[str(name)] for path in episode_paths for names in pq.read_table(path, columns=["tasks"]).column("tasks").to_pylist() for name in (names or [])})
    else:
        return
    if used == indexes:
        return
    mapping = {old: new for new, old in enumerate(used)}
    positions = [indexes.index(old) for old in used]
    compacted = task_table.take(pa.array(positions, type=pa.int64()))
    pq.write_table(_set_column(compacted, "task_index", list(range(len(used)))), root / "meta/tasks.parquet")
    for path in episode_paths:
        table = pq.read_table(path)
        if "task_index" in table.column_names:
            values = [mapping[int(value)] for value in table.column("task_index").to_pylist()]
            pq.write_table(_set_column(table, "task_index", values), path)
    for path in _files(root, "data"):
        table = pq.read_table(path)
        if "task_index" not in table.column_names:
            continue
        values = [mapping.get(int(value), int(value)) for value in table.column("task_index").to_pylist()]
        pq.write_table(_set_column(table, "task_index", values), path)
    info_path = root / "meta/info.json"
    info_data = json.loads(info_path.read_text())
    if "total_tasks" in info_data:
        info_data["total_tasks"] = len(used)
        info_path.write_text(json.dumps(info_data, indent=2))


def _mutate(root: Path, info: dict[str, Any]) -> None:
    task_path = root / "meta/tasks.parquet"
    task_table, text_column, indexes, texts = _task_table(root)
    old_index, instruction = int(info["current_task_index"]), str(info["normalized_instruction"])
    old_text = texts[indexes.index(old_index)]
    if info["mode"] == "shared":
        pq.write_table(_set_column(task_table, text_column, [instruction if text == old_text else text for text in texts]), task_path)
        for path in _files(root, "meta/episodes"):
            table = pq.read_table(path)
            if "tasks" not in table.column_names:
                continue
            lists = table.column("tasks").to_pylist()
            changed = [[instruction if str(name) == old_text else name for name in names] if isinstance(names, list) else names for names in lists]
            if changed != lists:
                pq.write_table(_set_column(table, "tasks", changed), path)
        return
    target = info["target_task_index"]
    if info["action"] == "create":
        target = len(indexes)
        pq.write_table(_append_task(task_table, text_column, target, instruction), task_path)
        info_path = root / "meta/info.json"
        info_data = json.loads(info_path.read_text())
        if "total_tasks" in info_data:
            info_data["total_tasks"] = len(indexes) + 1
            info_path.write_text(json.dumps(info_data, indent=2))
    target = int(target)
    for path in _files(root, "meta/episodes"):
        table = pq.read_table(path)
        positions = [i for i, value in enumerate(table.column("episode_index").to_pylist()) if int(value) == int(info["episode_index"])]
        if not positions: continue
        position = positions[0]
        if "task_index" in table.column_names:
            values = table.column("task_index").to_pylist(); values[position] = target
            pq.write_table(_set_column(table, "task_index", values), path)
        else:
            values = table.column("tasks").to_pylist(); values[position] = [instruction]
            pq.write_table(_set_column(table, "tasks", values), path)
    for path in _files(root, "data"):
        table = pq.read_table(path)
        if "episode_index" not in table.column_names or "task_index" not in table.column_names: continue
        indices = table.column("episode_index").to_pylist(); values = table.column("task_index").to_pylist(); changed = False
        for pos, value in enumerate(indices):
            if int(value) == int(info["episode_index"]): values[pos] = target; changed = True
        if changed: pq.write_table(_set_column(table, "task_index", values), path)
    _compact_task_indexes(root)


def commit(root: Path, episode_index: int, instruction: str, mode: str, fingerprint: str, confirm_shared: bool) -> dict[str, Any]:
    info = preview(root, episode_index, instruction, mode)
    if info["fingerprint"] != fingerprint:
        raise InstructionConflictError("instruction_preview_stale", "Instruction preview is stale")
    if mode == "shared" and not confirm_shared:
        raise InstructionConflictError("shared_confirmation_required", "Shared change requires confirmation")
    if info["action"] == "no_op": return info
    token = uuid.uuid4().hex; backup = root.parent / f".{root.name}.instruction-backup-{token}"; staging = root.parent / f".{root.name}.instruction-staging-{token}"; manifest = _manifest_path(root); moved = False
    _write_manifest(root, {"backup": str(backup), "staging": str(staging), "phase": "prepared"})
    try:
        root.rename(backup); moved = True; _write_manifest(root, {"backup": str(backup), "staging": str(staging), "phase": "moved"}); shutil.copytree(backup, staging); _mutate(staging, info); staging.rename(root); manifest.unlink(); shutil.rmtree(backup)
    except Exception:
        if moved and root.exists(): shutil.rmtree(root)
        if staging.exists(): shutil.rmtree(staging)
        if backup.exists(): backup.rename(root)
        if manifest.exists(): manifest.unlink()
        raise
    return info
