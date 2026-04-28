"""Path-keyed registry for loaded LeRobot dataset contexts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from glob import glob
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from backend.core.config import settings
from backend.datasets.services.task_parquet import normalize_task_records


def dataset_key_for(path: str | Path) -> str:
    """Return a stable short key for a resolved dataset path."""
    return hashlib.sha256(str(Path(path).resolve()).encode("utf-8")).hexdigest()[:16]


@dataclass
class DatasetContext:
    """Per-dataset state previously held by the DatasetService singleton."""

    dataset_path: Path
    info: dict
    episodes: list[dict]
    tasks: list[dict]
    episode_file_index: dict[int, dict]
    episode_parquet_files: list[Path]
    episode_to_file_map: dict[int, Path]
    file_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    episodes_cache: dict[int, dict] | None = None
    distribution_cache: dict[str, dict] = field(default_factory=dict)

    def get_info(self) -> dict:
        return self.info

    def get_episodes(self) -> list[dict]:
        return self.episodes

    def get_tasks(self) -> list[dict]:
        return self.tasks

    def get_features(self) -> dict:
        return self.info.get("features", {})

    def get_dataset_path(self) -> str:
        return str(self.dataset_path)

    async def get_tasks_map(self) -> dict[int, str]:
        return {
            int(t["task_index"]): str(t.get("task", ""))
            for t in self.tasks
        }

    def iter_episode_parquet_files(self) -> list[Path]:
        return list(self.episode_parquet_files)

    def get_episode_file_location(self, episode_index: int) -> dict:
        if episode_index not in self.episode_file_index:
            raise KeyError(f"Episode index {episode_index!r} not found.")
        return self.episode_file_index[episode_index]

    def get_file_for_episode(self, episode_index: int) -> Path | None:
        return self.episode_to_file_map.get(episode_index)

    def get_file_lock(self, file_path: str | Path) -> asyncio.Lock:
        key = str(file_path)
        if key not in self.file_locks:
            self.file_locks[key] = asyncio.Lock()
        return self.file_locks[key]

    @property
    def file_lock(self) -> asyncio.Lock:
        return self.get_file_lock("__tasks_parquet__")

    def reload_tasks(self) -> None:
        tasks_path = self.dataset_path / "meta" / "tasks.parquet"
        if not tasks_path.exists():
            self.tasks = []
            return

        from backend.datasets.services.dataset_service import _table_to_list_of_dicts

        table = pq.read_table(str(tasks_path))
        self.tasks = normalize_task_records(_table_to_list_of_dicts(table), table)


class DatasetRegistry:
    """Thread-safe LRU cache mapping dataset paths to DatasetContext objects."""

    def __init__(self, max_size: int = 8) -> None:
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        self._max_size = max_size
        self._items: OrderedDict[Path, DatasetContext] = OrderedDict()
        self._key_to_path: dict[str, Path] = {}
        self._lock = threading.Lock()

    def get(self, path: str | Path) -> DatasetContext:
        resolved = self._resolve_and_validate(path)
        with self._lock:
            ctx = self._items.get(resolved)
            if ctx is not None:
                self._items.move_to_end(resolved)
                return ctx

        ctx = self._load(resolved)

        with self._lock:
            self._items[resolved] = ctx
            self._items.move_to_end(resolved)
            self._key_to_path[dataset_key_for(resolved)] = resolved
            while len(self._items) > self._max_size:
                self._items.popitem(last=False)
        return ctx

    def get_by_key(self, dataset_key: str) -> DatasetContext:
        with self._lock:
            path = self._key_to_path.get(dataset_key)
        if path is None:
            raise KeyError(f"Unknown dataset_key: {dataset_key}")
        return self.get(path)

    def invalidate(self, path: str | Path) -> None:
        resolved = Path(path).resolve()
        with self._lock:
            self._items.pop(resolved, None)
            self._key_to_path = {
                key: value
                for key, value in self._key_to_path.items()
                if value != resolved
            }

    def _resolve_and_validate(self, path: str | Path) -> Path:
        resolved = Path(path).resolve()
        allowed_roots = [Path(p).resolve() for p in settings.allowed_dataset_roots]
        if not any(resolved.is_relative_to(root) for root in allowed_roots):
            raise ValueError(f"Dataset path is not under any allowed root: {resolved}")
        if not resolved.exists():
            raise FileNotFoundError(f"Dataset path does not exist: {resolved}")
        if not resolved.is_dir():
            raise ValueError(f"Dataset path is not a directory: {resolved}")
        return resolved

    def _load(self, root: Path) -> DatasetContext:
        info = self._load_info(root)
        episodes, parquet_files, episode_to_file = self._load_episodes(root)
        tasks = self._load_tasks(root)
        episode_file_index = self._build_episode_file_index(episodes, info)
        return DatasetContext(
            dataset_path=root,
            info=info,
            episodes=episodes,
            tasks=tasks,
            episode_file_index=episode_file_index,
            episode_parquet_files=parquet_files,
            episode_to_file_map=episode_to_file,
        )

    def _load_info(self, root: Path) -> dict:
        info_path = root / "meta" / "info.json"
        with info_path.open("r", encoding="utf-8") as fh:
            content = fh.read().rstrip("\x00")
            return json.loads(content)

    def _load_episodes(self, root: Path) -> tuple[list[dict], list[Path], dict[int, Path]]:
        from backend.datasets.services.dataset_service import (
            _normalize_compatible_string_widths,
            _table_to_list_of_dicts,
        )

        pattern = str(root / "meta" / "episodes" / "chunk-*" / "file-*.parquet")
        parquet_files = [Path(f) for f in sorted(glob(pattern))]
        if not parquet_files:
            return [], [], {}

        tables: list[pa.Table] = []
        episode_to_file: dict[int, Path] = {}
        for file_path in parquet_files:
            table = pq.read_table(file_path)
            tables.append(table)
            for idx in table.column("episode_index").to_pylist():
                episode_to_file[int(idx)] = file_path

        combined = pa.concat_tables(
            _normalize_compatible_string_widths(tables),
            promote_options="default",
        )
        return _table_to_list_of_dicts(combined), parquet_files, episode_to_file

    def _load_tasks(self, root: Path) -> list[dict]:
        from backend.datasets.services.dataset_service import _table_to_list_of_dicts

        tasks_path = root / "meta" / "tasks.parquet"
        if not tasks_path.exists():
            return []
        table = pq.read_table(str(tasks_path))
        return normalize_task_records(_table_to_list_of_dicts(table), table)

    def _build_episode_file_index(self, episodes: list[dict], info: dict) -> dict[int, dict]:
        features: dict = info.get("features", {})
        camera_keys: list[str] = [
            key for key in features
            if key.startswith("observation.images.") or key.startswith("observation.image.")
        ]

        index: dict[int, dict] = {}
        for ep in episodes:
            ep_idx = int(ep["episode_index"])
            entry: dict = {
                "data_chunk_index": ep.get("data/chunk_index", 0),
                "data_file_index": ep.get("data/file_index", 0),
                "dataset_from_index": ep.get("dataset_from_index", 0),
                "dataset_to_index": ep.get("dataset_to_index", 0),
                "videos": {},
            }
            for cam_key in camera_keys:
                chunk_col = f"videos/{cam_key}/chunk_index"
                file_col = f"videos/{cam_key}/file_index"
                from_ts_col = f"videos/{cam_key}/from_timestamp"
                to_ts_col = f"videos/{cam_key}/to_timestamp"
                chunk_val = ep.get(chunk_col)
                file_val = ep.get(file_col)
                if chunk_val is not None or file_val is not None:
                    entry["videos"][cam_key] = {
                        "chunk_index": chunk_val,
                        "file_index": file_val,
                        "from_timestamp": ep.get(from_ts_col),
                        "to_timestamp": ep.get(to_ts_col),
                    }
            index[ep_idx] = entry
        return index


dataset_registry = DatasetRegistry()
