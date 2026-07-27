"""Dataset-compatible adapter for raw recording task directories."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from backend.core.config import settings
from backend.core.db import get_db
from backend.converter.service import (
    SERIAL_RE,
    inspect_worker_recording,
    is_worker_plain_directory,
    scan_worker_recordings,
)
from backend.datasets.services.path_policy import (
    normalize_roots,
)


RAW_READ_ONLY_ERROR = {
    "error": "raw_read_only",
    "detail": "Raw fields and task metadata are read-only in v1",
}


def raw_base() -> Path:
    return (Path(settings.dataset_root_base) / "raw").resolve()


def _raw_path_and_key(path: str | Path) -> tuple[Path, str]:
    base = raw_base()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        relative = candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"Raw path is not under raw root: {path}") from exc
    pure = PurePosixPath(relative.as_posix())
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"invalid raw path: {path}")
    return base.joinpath(*pure.parts), pure.as_posix() if pure.parts else ""


def is_raw_dataset_path(path: str | Path) -> bool:
    try:
        _, key = _raw_path_and_key(path)
    except ValueError:
        return False
    if not is_worker_plain_directory(key, raw_base=raw_base()):
        return False
    accepted = _accepted_recordings_snapshot()
    if not key:
        return bool(accepted)
    prefix = f"{key}/"
    return key in accepted or any(task.startswith(prefix) for task in accepted)


def raw_dataset_key_for(path: str | Path) -> str:
    canonical, _ = _raw_path_and_key(path)
    digest = hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()[:16]
    return f"raw-{digest}"


def _accepted_recordings_snapshot() -> dict[str, list[str]]:
    return scan_worker_recordings(raw_base(), cached=True)


def _recording_dirs(
    task_dir: Path,
    accepted: Mapping[str, list[str]] | None = None,
) -> list[Path]:
    """Return task recordings accepted by the converter worker contract."""
    try:
        canonical, task = _raw_path_and_key(task_dir)
    except ValueError:
        return []
    if not task or not is_worker_plain_directory(task, raw_base=raw_base()):
        return []
    recordings = (
        _accepted_recordings_snapshot()
        if accepted is None
        else accepted
    ).get(task, [])
    return [canonical / serial for serial in recordings]


def is_raw_task_dir(path: str | Path) -> bool:
    try:
        canonical, task = _raw_path_and_key(path)
    except ValueError:
        return False
    accepted = _accepted_recordings_snapshot()
    if (
        not task
        or task not in accepted
        or not is_worker_plain_directory(task, raw_base=raw_base())
    ):
        return False
    return bool(_recording_dirs(canonical, accepted))


def _validate_raw_task_path(
    path: str | Path,
    accepted: Mapping[str, list[str]] | None = None,
) -> Path:
    candidate, task = _raw_path_and_key(path)
    base = raw_base()
    if (
        not task
        or not is_worker_plain_directory(task, raw_base=base)
    ):
        raise ValueError(f"Raw dataset path is not a plain directory: {candidate}")
    if settings.allowed_dataset_roots:
        allowed = normalize_roots(settings.allowed_dataset_roots)
        if not any(base == root or base.is_relative_to(root) for root in allowed):
            raise ValueError(
                f"Raw dataset path is not under any allowed root: {candidate}"
            )
    if not _recording_dirs(candidate, accepted):
        raise ValueError(f"Raw dataset path has no valid recordings: {candidate}")
    return candidate


def _safe_relative_to_raw(path: Path) -> str:
    _, key = _raw_path_and_key(path)
    if not key:
        raise ValueError(f"Raw recording path is empty: {path}")
    return key


def _read_metacard(recording_dir: Path) -> dict[str, Any]:
    try:
        report = inspect_worker_recording(
            _safe_relative_to_raw(recording_dir),
            raw_base=raw_base(),
            load_metacard=True,
        )
        payload = json.loads(report.metacard_text or "{}")
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


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
    accepted = _accepted_recordings_snapshot()
    task_dir = _validate_raw_task_path(path, accepted)
    recordings = _recording_dirs(task_dir, accepted)
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
        "features": {},
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
    resolved = str(ctx.dataset_path)
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
        for recording_dir in _recording_dirs(Path(task_dir))
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
    try:
        cell_dir, cell = _raw_path_and_key(cell_path)
    except ValueError:
        return []
    if (
        not cell
        or not is_worker_plain_directory(cell, raw_base=raw_base())
    ):
        return []

    accepted = _accepted_recordings_snapshot()
    summaries: list[dict[str, Any]] = []
    for task_dir in _iter_raw_task_dirs(cell_dir, accepted):
        recordings = _recording_dirs(task_dir, accepted)
        if not recordings:
            continue
        name = task_dir.relative_to(cell_dir).as_posix()
        summaries.append(
            {
                "name": name,
                "path": str(task_dir),
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


def _iter_raw_task_dirs(
    cell_dir: Path,
    accepted: Mapping[str, list[str]] | None = None,
) -> list[Path]:
    try:
        _, cell = _raw_path_and_key(cell_dir)
    except ValueError:
        return []
    prefix = f"{cell}/"
    task_map = _accepted_recordings_snapshot() if accepted is None else accepted
    return [
        raw_base() / cell_task
        for cell_task in sorted(task_map)
        if cell_task.startswith(prefix)
    ]
