import asyncio
import json
from pathlib import Path
from glob import glob
import pyarrow.parquet as pq
import pyarrow as pa

from backend.core.config import settings
from backend.datasets.services.task_parquet import normalize_task_records


class DatasetService:
    """Singleton service for loading and caching a LeRobot v3.0 dataset."""

    def __init__(self) -> None:
        self._dataset_path: Path | None = None
        self._info: dict | None = None
        self._episodes: list[dict] | None = None
        self._tasks: list[dict] | None = None
        self._episode_file_index: dict[int, dict] | None = None
        self._file_locks: dict[str, asyncio.Lock] = {}
        self._episode_parquet_files: list[Path] = []
        self._episode_to_file_map: dict[int, Path] = {}
        # Mutable cache for episode_service to populate with enriched episode dicts
        self.episodes_cache: dict[int, dict] | None = None
        # Distribution cache: field_name -> DistributionResponse dict
        self.distribution_cache: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Properties (used by consumer services)
    # ------------------------------------------------------------------

    @property
    def dataset_path(self) -> Path:
        self._require_loaded()
        return self._dataset_path  # type: ignore[return-value]

    @property
    def tasks(self) -> list[dict]:
        self._require_loaded()
        return self._tasks  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _clear(self) -> None:
        self._dataset_path = None
        self._info = None
        self._episodes = None
        self._tasks = None
        self._episode_file_index = None
        self._file_locks = {}
        self._episode_parquet_files = []
        self.episodes_cache = None
        self.distribution_cache = {}

    def _load_info(self, root: Path) -> dict:
        info_path = root / "meta" / "info.json"
        with info_path.open("r", encoding="utf-8") as fh:
            content = fh.read().rstrip("\x00")
            return json.loads(content)

    def _load_episodes(self, root: Path) -> list[dict]:
        pattern = str(root / "meta" / "episodes" / "chunk-*" / "file-*.parquet")
        parquet_files = sorted(glob(pattern))
        self._episode_parquet_files = [Path(f) for f in parquet_files]

        if not parquet_files:
            self._episode_to_file_map = {}
            return []

        tables: list[pa.Table] = []
        episode_to_file: dict[int, Path] = {}
        for f in parquet_files:
            file_path = Path(f)
            table = pq.read_table(f)
            tables.append(table)
            # Build episode_index -> file_path mapping while we already have the table
            for idx in table.column("episode_index").to_pylist():
                episode_to_file[idx] = file_path

        self._episode_to_file_map = episode_to_file
        combined: pa.Table = pa.concat_tables(
            _normalize_compatible_string_widths(tables),
            promote_options="default",
        )
        return _table_to_list_of_dicts(combined)

    def _load_tasks(self, root: Path) -> list[dict]:
        tasks_path = root / "meta" / "tasks.parquet"
        if not tasks_path.exists():
            return []
        table: pa.Table = pq.read_table(str(tasks_path))
        return normalize_task_records(_table_to_list_of_dicts(table), table)

    def _build_episode_file_index(self, episodes: list[dict], info: dict) -> dict[int, dict]:
        features: dict = info.get("features", {})
        camera_keys: list[str] = [
            key for key in features
            if key.startswith("observation.images.") or key.startswith("observation.image.")
        ]

        index: dict[int, dict] = {}
        for ep in episodes:
            ep_idx: int = ep["episode_index"]
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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _is_path_allowed(self, root: Path) -> bool:
        allowed_roots = [Path(p).resolve() for p in settings.allowed_dataset_roots]
        return any(root.is_relative_to(allowed_root) for allowed_root in allowed_roots)

    def load_dataset(self, path: str | Path) -> None:
        root = Path(path).resolve()

        if not self._is_path_allowed(root):
            raise ValueError(f"Dataset path is not under any allowed root: {root}")
        if not root.exists():
            raise FileNotFoundError(f"Dataset path does not exist: {root}")
        if not root.is_dir():
            raise ValueError(f"Dataset path is not a directory: {root}")

        self._clear()
        self._info = self._load_info(root)
        self._episodes = self._load_episodes(root)
        self._tasks = self._load_tasks(root)
        self._episode_file_index = self._build_episode_file_index(self._episodes, self._info)
        # _episode_to_file_map is already populated by _load_episodes()
        self._dataset_path = root

    def get_info(self) -> dict:
        self._require_loaded()
        return self._info  # type: ignore[return-value]

    def get_episodes(self) -> list[dict]:
        self._require_loaded()
        return self._episodes  # type: ignore[return-value]

    def get_tasks(self) -> list[dict]:
        self._require_loaded()
        return self._tasks  # type: ignore[return-value]

    def get_episode_file_location(self, episode_index: int) -> dict:
        self._require_loaded()
        index = self._episode_file_index  # type: ignore[assignment]
        if episode_index not in index:
            raise KeyError(f"Episode index {episode_index!r} not found.")
        return index[episode_index]

    def get_features(self) -> dict:
        self._require_loaded()
        return self._info.get("features", {})  # type: ignore[union-attr]

    def get_dataset_path(self) -> str:
        self._require_loaded()
        return str(self._dataset_path)

    # --- Methods used by episode_service ---

    async def get_tasks_map(self) -> dict[int, str]:
        """Return {task_index: task_instruction} mapping."""
        self._require_loaded()
        return {
            int(t["task_index"]): str(t.get("task", ""))
            for t in (self._tasks or [])
        }

    def iter_episode_parquet_files(self) -> list[Path]:
        self._require_loaded()
        return list(self._episode_parquet_files)

    def get_file_for_episode(self, episode_index: int) -> Path | None:
        return self._episode_to_file_map.get(episode_index)

    def get_file_lock(self, file_path: str | Path) -> asyncio.Lock:
        """Return asyncio.Lock for file_path (sync, creates on first use)."""
        key = str(file_path)
        if key not in self._file_locks:
            self._file_locks[key] = asyncio.Lock()
        return self._file_locks[key]

    # --- Methods used by task_service ---

    @property
    def file_lock(self) -> asyncio.Lock:
        """Single lock for tasks.parquet writes."""
        return self.get_file_lock("__tasks_parquet__")

    def reload_tasks(self) -> None:
        """Re-read tasks from disk into cache."""
        if self._dataset_path:
            self._tasks = self._load_tasks(self._dataset_path)

    # ------------------------------------------------------------------
    # Private guard
    # ------------------------------------------------------------------

    def _require_loaded(self) -> None:
        if self._dataset_path is None:
            raise RuntimeError(
                "No dataset loaded. Call load_dataset(path) before accessing data."
            )


def _table_to_list_of_dicts(table: pa.Table) -> list[dict]:
    column_names = table.schema.names
    columns = [table.column(name).to_pylist() for name in column_names]
    return [
        dict(zip(column_names, row))
        for row in zip(*columns)
    ] if columns else []


def _normalize_compatible_string_widths(tables: list[pa.Table]) -> list[pa.Table]:
    compatible_types: dict[str, pa.DataType] = {}
    field_types: dict[str, list[pa.DataType]] = {}

    for table in tables:
        for field in table.schema:
            field_types.setdefault(field.name, []).append(field.type)

    for field_name, types in field_types.items():
        if _has_only_string_width_mismatch(types):
            compatible_types[field_name] = pa.large_string()

    if not compatible_types:
        return tables

    normalized_tables: list[pa.Table] = []
    for table in tables:
        normalized_table = table
        for index, field in enumerate(normalized_table.schema):
            target_type = compatible_types.get(field.name)
            if target_type is None or field.type.equals(target_type):
                continue
            normalized_table = normalized_table.set_column(
                index,
                pa.field(
                    field.name,
                    target_type,
                    nullable=field.nullable,
                    metadata=field.metadata,
                ),
                normalized_table.column(field.name).cast(target_type),
            )
        normalized_tables.append(normalized_table)

    return normalized_tables


def _has_only_string_width_mismatch(types: list[pa.DataType]) -> bool:
    if len(types) < 2:
        return False

    distinct_types: list[pa.DataType] = []
    for data_type in types:
        if any(existing.equals(data_type) for existing in distinct_types):
            continue
        distinct_types.append(data_type)

    return (
        len(distinct_types) > 1
        and all(
            data_type.equals(pa.string()) or data_type.equals(pa.large_string())
            for data_type in distinct_types
        )
    )


dataset_service = DatasetService()
