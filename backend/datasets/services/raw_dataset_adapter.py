"""Dataset-compatible adapter for raw recording task directories."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from backend.core.config import settings
from backend.core.db import get_db
from backend.converter.service import SERIAL_RE


RAW_READ_ONLY_ERROR = {
    "error": "raw_read_only",
    "detail": "Raw fields and task metadata are read-only in v1",
}


def raw_base() -> Path:
    return (Path(settings.dataset_root_base) / "raw").resolve()


def is_raw_dataset_path(path: str | Path) -> bool:
    resolved = Path(path).resolve()
    base = raw_base()
    return resolved == base or resolved.is_relative_to(base)


def raw_dataset_key_for(path: str | Path) -> str:
    from backend.datasets.services.dataset_registry import dataset_key_for

    resolved = Path(path).resolve()
    return f"raw-{dataset_key_for(resolved)}"


def _recording_dirs(task_dir: Path) -> list[Path]:
    try:
        children = sorted(task_dir.iterdir())
    except OSError:
        return []
    return [
        child
        for child in children
        if child.is_dir()
        and SERIAL_RE.match(child.name)
        and (child / "metacard.json").is_file()
        and (child / f"{child.name}_0.mcap").is_file()
    ]


def is_raw_task_dir(path: str | Path) -> bool:
    resolved = Path(path).resolve()
    if not is_raw_dataset_path(resolved) or not resolved.is_dir():
        return False
    return bool(_recording_dirs(resolved))


def _validate_raw_task_path(path: str | Path) -> Path:
    resolved = Path(path).resolve()
    base = raw_base()
    allowed_roots = [Path(root).resolve() for root in settings.allowed_dataset_roots]
    if not resolved.is_relative_to(base):
        raise ValueError(f"Raw dataset path is not under raw root: {resolved}")
    if allowed_roots and not any(resolved.is_relative_to(root) for root in allowed_roots):
        raise ValueError(f"Raw dataset path is not under any allowed root: {resolved}")
    if not resolved.exists():
        raise FileNotFoundError(f"Raw dataset path does not exist: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"Raw dataset path is not a directory: {resolved}")
    if not _recording_dirs(resolved):
        raise ValueError(f"Raw dataset path has no valid recordings: {resolved}")
    return resolved


def _safe_relative_to_raw(path: Path) -> str:
    rel = path.resolve().relative_to(raw_base())
    pure = PurePosixPath(rel.as_posix())
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"invalid raw path: {path}")
    return pure.as_posix()


def _read_metacard(recording_dir: Path) -> dict[str, Any]:
    try:
        return json.loads((recording_dir / "metacard.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


@dataclass
class RawDatasetContext:
    dataset_path: Path
    recordings: list[Path]
    info: dict[str, Any]
    episodes: list[dict[str, Any]]
    tasks: list[dict[str, Any]]
    episodes_cache: dict[int, dict[str, Any]] | None = None
    distribution_cache: dict[str, dict] = field(default_factory=dict)

    is_raw_dataset: bool = True

    def get_info(self) -> dict[str, Any]:
        return self.info

    def get_episodes(self) -> list[dict[str, Any]]:
        return self.episodes

    def get_tasks(self) -> list[dict[str, Any]]:
        return self.tasks

    def get_features(self) -> dict[str, Any]:
        return self.info.get("features", {})

    def get_dataset_path(self) -> str:
        return str(self.dataset_path)

    async def get_tasks_map(self) -> dict[int, str]:
        return {int(t["task_index"]): str(t.get("task", "")) for t in self.tasks}

    def iter_episode_parquet_files(self) -> list[Path]:
        return []

    def get_episode_file_location(self, episode_index: int) -> dict[str, Any]:
        episode = self.episodes[episode_index]
        return {
            "data_chunk_index": 0,
            "data_file_index": episode_index,
            "dataset_from_index": episode.get("dataset_from_index", 0),
            "dataset_to_index": episode.get("dataset_to_index", 0),
            "raw_recording": episode.get("raw_recording"),
            "viewer": {"type": "rerun_raw", "recording": episode.get("raw_recording")},
            "videos": {},
        }

    def get_file_for_episode(self, episode_index: int) -> Path | None:
        if 0 <= episode_index < len(self.recordings):
            return self.recordings[episode_index]
        return None

    def get_file_lock(self, _file_path: str | Path):
        import asyncio

        return asyncio.Lock()

    def reload_tasks(self) -> None:
        return None


def load_raw_context(path: str | Path) -> RawDatasetContext:
    task_dir = _validate_raw_task_path(path)
    recordings = _recording_dirs(task_dir)
    first_card = _read_metacard(recordings[0]) if recordings else {}
    task_instruction = str(first_card.get("task_name") or task_dir.name)
    rel_task = _safe_relative_to_raw(task_dir)

    episodes: list[dict[str, Any]] = []
    for index, recording_dir in enumerate(recordings):
        rel_recording = _safe_relative_to_raw(recording_dir)
        card = _read_metacard(recording_dir)
        episodes.append(
            {
                "episode_index": index,
                "length": 0,
                "task_index": 0,
                "task_instruction": str(card.get("task_name") or task_instruction),
                "chunk_index": 0,
                "file_index": index,
                "dataset_from_index": 0,
                "dataset_to_index": 0,
                "grade": None,
                "tags": [],
                "reason": None,
                "created_at": _created_at_from_serial(recording_dir.name),
                "Serial_number": f"raw:{rel_recording}",
                "raw_recording": rel_recording,
            }
        )

    info = {
        "fps": 0,
        "total_episodes": len(episodes),
        "total_tasks": 1,
        "robot_type": "raw",
        "features": {
            "raw.rerun": {
                "dtype": "rerun",
                "viewer": "rerun_raw",
                "task": rel_task,
            }
        },
        "raw_task_path": rel_task,
        "source_type": "raw",
    }
    tasks = [{"task_index": 0, "task": task_instruction, "task_instruction": task_instruction}]
    return RawDatasetContext(task_dir, recordings, info, episodes, tasks)


def _created_at_from_serial(serial: str) -> str | None:
    if not SERIAL_RE.match(serial):
        return None
    return f"{serial[0:4]}-{serial[4:6]}-{serial[6:8]}"


async def ensure_raw_dataset_registered(ctx: RawDatasetContext) -> int:
    db = await get_db()
    resolved = str(ctx.dataset_path.resolve())
    async with db.execute("SELECT id FROM datasets WHERE path = ?", (resolved,)) as cursor:
        row = await cursor.fetchone()
    if row:
        dataset_id = row[0]
    else:
        await db.execute(
            """
            INSERT INTO datasets (path, name, cell_name, fps, total_episodes, robot_type, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, NOW())
            """,
            (
                resolved,
                ctx.dataset_path.name,
                ctx.dataset_path.parent.name,
                0,
                len(ctx.episodes),
                "raw",
            ),
        )
        await db.commit()
        async with db.execute("SELECT id FROM datasets WHERE path = ?", (resolved,)) as cursor:
            dataset_id = (await cursor.fetchone())[0]

    await db.execute("DELETE FROM episode_serials WHERE dataset_id = ?", (dataset_id,))
    await db.executemany(
        "INSERT INTO episode_serials (dataset_id, episode_index, serial_number) VALUES (?, ?, ?)",
        [
            (dataset_id, int(ep["episode_index"]), str(ep["Serial_number"]))
            for ep in ctx.episodes
        ],
    )
    await db.commit()
    return int(dataset_id)


async def raw_annotation_counts_for_task(task_dir: str | Path) -> dict[str, int]:
    serials = [
        f"raw:{_safe_relative_to_raw(recording_dir)}"
        for recording_dir in _recording_dirs(Path(task_dir).resolve())
    ]
    counts = {"graded_count": 0, "good_count": 0, "normal_count": 0, "bad_count": 0}
    if not serials:
        return counts

    db = await get_db()
    for i in range(0, len(serials), 500):
        chunk = serials[i:i + 500]
        placeholders = ",".join("?" * len(chunk))
        async with db.execute(
            f"""
            SELECT grade, COUNT(*) AS count
            FROM annotations
            WHERE serial_number IN ({placeholders})
              AND grade IS NOT NULL
            GROUP BY grade
            """,
            tuple(chunk),
        ) as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            grade = row["grade"]
            count = int(row["count"])
            counts["graded_count"] += count
            if grade == "good":
                counts["good_count"] += count
            elif grade == "normal":
                counts["normal_count"] += count
            elif grade == "bad":
                counts["bad_count"] += count
    return counts


def raw_info_fields(dataset_path: str | Path) -> list[dict[str, Any]]:
    ctx = load_raw_context(dataset_path)
    fields: list[dict[str, Any]] = []
    for key, value in ctx.info.items():
        fields.append(
            {
                "key": key,
                "value": value,
                "dtype": type(value).__name__,
                "is_system": True,
            }
        )
    return fields


def raw_episode_columns(dataset_path: str | Path) -> list[dict[str, Any]]:
    return [
        {"name": "episode_index", "dtype": "int64", "is_system": True},
        {"name": "serial", "dtype": "string", "is_system": True},
        {"name": "recording", "dtype": "string", "is_system": True},
        {"name": "task_name", "dtype": "string", "is_system": True},
        {"name": "mcap_count", "dtype": "int64", "is_system": True},
    ]


def raw_dataset_summaries_for_cell(cell_path: str | Path) -> list[dict[str, Any]]:
    cell_dir = Path(cell_path).resolve()
    if not cell_dir.is_relative_to(raw_base()) or not cell_dir.is_dir():
        return []

    summaries: list[dict[str, Any]] = []
    for task_dir in _iter_raw_task_dirs(cell_dir):
        recordings = _recording_dirs(task_dir)
        if not recordings:
            continue
        name = task_dir.relative_to(cell_dir).as_posix()
        summaries.append(
            {
                "name": name,
                "path": str(task_dir.resolve()),
                "total_episodes": len(recordings),
                "graded_count": 0,
                "good_count": 0,
                "normal_count": 0,
                "bad_count": 0,
                "robot_type": "raw",
                "fps": 0,
                "total_duration_sec": 0,
                "good_duration_sec": 0,
                "normal_duration_sec": 0,
                "bad_duration_sec": 0,
            }
        )
    return summaries


def _iter_raw_task_dirs(cell_dir: Path) -> list[Path]:
    task_dirs: list[Path] = []
    for child in sorted(p for p in cell_dir.iterdir() if p.is_dir()):
        if child.name.startswith("."):
            continue
        if _recording_dirs(child):
            task_dirs.append(child)
            continue
        for grandchild in sorted(p for p in child.iterdir() if p.is_dir()):
            if grandchild.name.startswith("."):
                continue
            if _recording_dirs(grandchild):
                task_dirs.append(grandchild)
    return task_dirs
