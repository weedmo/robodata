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

from backend.core import config as core_config
from backend.datasets.services.path_policy import (
    PathOutsideAllowedRoots,
    require_contained_in_roots,
)
from backend.datasets.services.task_parquet import normalize_task_records

settings = core_config.settings


# Fingerprint is a tuple of (relative path, size, mtime_ns) triples for the
# metadata files that uniquely define a cached DatasetContext. It deliberately
# excludes large data/ and videos/ payloads so cache-hit refresh stays cheap.
DatasetFingerprint = tuple[tuple[str, int, int], ...]


def dataset_key_for(path: str | Path) -> str:
    """Return a stable short key for a resolved dataset path."""
    return hashlib.sha256(str(Path(path).resolve()).encode("utf-8")).hexdigest()[:16]


class DatasetMetadataChangedError(RuntimeError):
    """Raised when dataset metadata files keep changing during a stable-load attempt.

    Treated as a transient conflict, not a corrupt-dataset verdict. Dataset
    endpoints map this to HTTP 409 so clients can retry.
    """


def _fingerprint_dataset(root: Path) -> DatasetFingerprint:
    """Lightweight metadata fingerprint of a LeRobot dataset.

    Includes ``meta/info.json``, ``meta/tasks.parquet`` (when present), and
    every ``meta/episodes/chunk-*/file-*.parquet`` shard. Each entry is
    ``(relative_path, size, mtime_ns)``; relative path is included so added or
    removed shards register as a change even when size/mtime collide.
    """
    candidates: list[Path] = []

    info_path = root / "meta" / "info.json"
    if info_path.exists():
        candidates.append(info_path)

    tasks_path = root / "meta" / "tasks.parquet"
    if tasks_path.exists():
        candidates.append(tasks_path)

    pattern = str(root / "meta" / "episodes" / "chunk-*" / "file-*.parquet")
    for f in sorted(glob(pattern)):
        candidates.append(Path(f))

    entries: list[tuple[str, int, int]] = []
    for p in candidates:
        try:
            st = p.stat()
        except FileNotFoundError:
            # Race with rename/delete mid-scan: encode a sentinel so a follow-up
            # fingerprint that observes the file (or its absence) consistently
            # will compare unequal and force a retry.
            entries.append((str(p.relative_to(root)), -1, -1))
            continue
        entries.append((str(p.relative_to(root)), int(st.st_size), int(st.st_mtime_ns)))
    return tuple(entries)


def _allowed_dataset_roots() -> list[str]:
    """Return allowed roots from both live config and compatibility alias.

    Some tests and legacy callers monkeypatch the imported ``settings`` object
    in this module, while runtime code may replace ``backend.core.config.settings``
    wholesale. Accept both surfaces so validation follows the active config
    without breaking object-level monkeypatches.
    """
    roots: list[str] = []
    seen: set[str] = set()
    for candidate in (core_config.settings, globals().get("settings")):
        if candidate is None:
            continue
        for root in getattr(candidate, "allowed_dataset_roots", []):
            if root not in seen:
                roots.append(root)
                seen.add(root)
    return roots


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
    fingerprint: DatasetFingerprint = field(default_factory=tuple)
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

    # Maximum stable-load attempts before giving up. PRD calls for one retry,
    # i.e. two total attempts.
    _STABLE_LOAD_MAX_ATTEMPTS = 2

    def __init__(self, max_size: int = 8) -> None:
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        self._max_size = max_size
        self._items: OrderedDict[Path, DatasetContext] = OrderedDict()
        self._key_to_path: dict[str, Path] = {}
        # Per-resolved-path file lock dictionaries are owned by the registry so
        # automatic context replacement on metadata reload keeps the same
        # asyncio.Lock identities. Without this, an in-flight parquet write
        # could race a new context's fresh lock for the same path.
        self._path_locks: dict[Path, dict[str, asyncio.Lock]] = {}
        self._lock = threading.Lock()

    def get(self, path: str | Path) -> DatasetContext:
        resolved = self._resolve_and_validate(path)
        with self._lock:
            ctx = self._items.get(resolved)

        if ctx is not None:
            current = _fingerprint_dataset(resolved)
            if current == ctx.fingerprint:
                with self._lock:
                    if self._items.get(resolved) is ctx:
                        self._items.move_to_end(resolved)
                        return ctx
                    fresh = self._items.get(resolved)
                    if fresh is not None and fresh.fingerprint == current:
                        self._items.move_to_end(resolved)
                        return fresh

        ctx = self._load_stable(resolved)

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
            # Explicit invalidation also drops the owned lock dictionary; any
            # caller still holding the old context has its own reference.
            self._path_locks.pop(resolved, None)

    def _resolve_and_validate(self, path: str | Path) -> Path:
        try:
            resolved = require_contained_in_roots(path, _allowed_dataset_roots()).path
        except PathOutsideAllowedRoots as exc:
            raise ValueError(f"Dataset path is not under any allowed root: {exc.candidate}") from exc
        if not resolved.exists():
            raise FileNotFoundError(f"Dataset path does not exist: {resolved}")
        if not resolved.is_dir():
            raise ValueError(f"Dataset path is not a directory: {resolved}")
        return resolved

    def _file_locks_for(self, resolved: Path) -> dict[str, asyncio.Lock]:
        """Return the registry-owned lock dictionary for a resolved path."""
        with self._lock:
            return self._path_locks.setdefault(resolved, {})

    def _load_stable(self, root: Path) -> DatasetContext:
        """Load with before/after fingerprint barrier and one retry.

        If the dataset metadata is being rewritten when we observe it, the
        before/after fingerprints will differ. We retry once. If the second
        attempt is still unstable we raise DatasetMetadataChangedError so HTTP
        callers can treat it as a transient conflict rather than publish a
        mixed metadata snapshot.
        """
        last_before: DatasetFingerprint | None = None
        last_after: DatasetFingerprint | None = None
        for _attempt in range(self._STABLE_LOAD_MAX_ATTEMPTS):
            before = _fingerprint_dataset(root)
            ctx = self._load(root, fingerprint=before)
            after = _fingerprint_dataset(root)
            if before == after:
                return ctx
            last_before, last_after = before, after
        raise DatasetMetadataChangedError(
            f"Dataset metadata changed while loading: {root} "
            f"(before={last_before!r}, after={last_after!r})"
        )

    def _load(self, root: Path, *, fingerprint: DatasetFingerprint) -> DatasetContext:
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
            file_locks=self._file_locks_for(root),
            fingerprint=fingerprint,
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
