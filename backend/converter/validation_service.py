"""Persistence helpers for converter validation state."""

from __future__ import annotations

import asyncio
import importlib
import json
import re
import sys
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from backend.converter.service import LEROBOT_BASE
from backend.datasets.services.task_parquet import (
    get_task_text_column_name,
    has_required_task_columns,
)

ValidationMode = Literal["quick", "full"]
ValidationStatus = Literal["not_run", "running", "passed", "failed", "partial"]

VALIDATION_STATE_FILE = LEROBOT_BASE / "convert_validation_state.json"

_validation_locks: dict[tuple[str, ValidationMode], asyncio.Lock] = {}
_validation_locks_mutex = threading.Lock()
_state_write_lock = threading.Lock()

REQUIRED_INFO_KEYS = {
    "features",
    "fps",
    "total_episodes",
    "total_frames",
}
REQUIRED_DATA_COLUMNS = {
    "episode_index",
    "frame_index",
    "index",
    "task_index",
    "timestamp",
}
REQUIRED_EPISODE_COLUMNS = {
    "data/chunk_index",
    "data/file_index",
    "dataset_from_index",
    "dataset_to_index",
    "episode_index",
}
LOADER_SKIP_SUMMARY = "Full partial: dataset OK, official loader skipped"
MIN_LEROBOT_VERSION = (0, 3, 0)


class ValidationAlreadyRunningError(RuntimeError):
    """Raised when a validation run is already active for a task/mode pair."""


@dataclass(slots=True)
class ValidationResult:
    status: ValidationStatus
    summary: str
    checked_at: str

    def as_dict(self) -> dict[str, str]:
        return {
            "status": self.status,
            "summary": self.summary,
            "checked_at": self.checked_at,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lock_for(cell_task: str, mode: ValidationMode) -> asyncio.Lock:
    key = (cell_task, mode)
    with _validation_locks_mutex:
        lock = _validation_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _validation_locks[key] = lock
        return lock


def ensure_not_running(cell_task: str, mode: ValidationMode) -> None:
    if _lock_for(cell_task, mode).locked():
        raise ValidationAlreadyRunningError(
            f"Validation is already running for {cell_task!r} in {mode!r} mode."
        )


def read_validation_state() -> dict:
    try:
        if VALIDATION_STATE_FILE.is_file():
            return json.loads(VALIDATION_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def write_validation_state(state: dict) -> None:
    VALIDATION_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = Path(f"{VALIDATION_STATE_FILE}.tmp")
    tmp_file.write_text(
        json.dumps(state, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp_file.replace(VALIDATION_STATE_FILE)


def _upsert_result(
    state: dict,
    cell_task: str,
    mode: ValidationMode,
    result: ValidationResult,
) -> dict:
    task_state = state.setdefault(cell_task, {})
    task_state[mode] = result.as_dict()
    return state


def _persist_running_result(
    cell_task: str,
    mode: ValidationMode,
    result: ValidationResult,
) -> None:
    with _state_write_lock:
        state = read_validation_state()
        write_validation_state(_upsert_result(state, cell_task, mode, result))


def _persist_result(
    cell_task: str,
    mode: ValidationMode,
    result: ValidationResult,
) -> None:
    with _state_write_lock:
        state = read_validation_state()
        write_validation_state(_upsert_result(state, cell_task, mode, result))


def _result(status: ValidationStatus, summary: str) -> ValidationResult:
    return ValidationResult(status=status, summary=summary, checked_at=_now_iso())


def _dataset_dir_for(cell_task: str) -> Path:
    return LEROBOT_BASE / cell_task


def _read_info_json(info_path: Path) -> dict:
    return json.loads(info_path.read_text(encoding="utf-8").rstrip("\x00"))


def _parquet_files(root: Path) -> list[Path]:
    return sorted(root.glob("chunk-*/file-*.parquet"))


def _read_parquet_table(path: Path):
    import pyarrow.parquet as pq

    return pq.read_table(path)


def _count_parquet_rows(paths: list[Path]) -> int:
    import pyarrow.parquet as pq

    return sum(pq.ParquetFile(path).metadata.num_rows for path in paths)


def _task_index_set(values) -> set[int]:
    return {int(value) for value in values if value is not None}


def _missing_columns(table, required_columns: set[str]) -> list[str]:
    return sorted(required_columns - set(table.column_names))


def _missing_episode_columns(table) -> list[str]:
    missing_columns = _missing_columns(table, REQUIRED_EPISODE_COLUMNS)
    if "task_index" not in table.column_names and "tasks" not in table.column_names:
        missing_columns.append("task_index")
    return missing_columns


def _validate_required_columns(
    files: list[Path],
    required_columns: set[str],
    kind: str,
) -> tuple[ValidationResult | None, list]:
    tables = []
    for path in files:
        try:
            table = _read_parquet_table(path)
        except Exception as exc:
            return (
                _result("failed", f"Quick failed: could not read {kind} parquet {path} ({exc})"),
                [],
            )
        missing_columns = _missing_columns(table, required_columns)
        if missing_columns:
            return (
                _result(
                    "failed",
                    f"Quick failed: {kind} parquet {path} missing columns "
                    + ", ".join(missing_columns),
                ),
                [],
            )
        tables.append(table)
    return None, tables


def _validate_required_parquet_schemas(
    files: list[Path],
    required_columns: set[str],
    kind: str,
) -> ValidationResult | None:
    import pyarrow.parquet as pq

    for path in files:
        try:
            column_names = pq.ParquetFile(path).schema_arrow.names
        except Exception as exc:
            return _result(
                "failed",
                f"Quick failed: could not read {kind} parquet {path} ({exc})",
            )
        missing_columns = sorted(required_columns - set(column_names))
        if missing_columns:
            return _result(
                "failed",
                f"Quick failed: {kind} parquet {path} missing columns "
                + ", ".join(missing_columns),
            )
    return None


def _validate_episode_columns(files: list[Path]) -> tuple[ValidationResult | None, list]:
    tables = []
    for path in files:
        try:
            table = _read_parquet_table(path)
        except Exception as exc:
            return (
                _result("failed", f"Quick failed: could not read episode parquet {path} ({exc})"),
                [],
            )
        missing_columns = _missing_episode_columns(table)
        if missing_columns:
            return (
                _result(
                    "failed",
                    f"Quick failed: episode parquet {path} missing columns "
                    + ", ".join(missing_columns),
                ),
                [],
            )
        tables.append(table)
    return None, tables


def _validate_episode_ranges(episode_tables: list, data_rows: int) -> ValidationResult | None:
    ranges: list[tuple[int, int]] = []
    for table in episode_tables:
        starts = [int(value) for value in table.column("dataset_from_index").to_pylist()]
        ends = [int(value) for value in table.column("dataset_to_index").to_pylist()]
        if len(starts) != len(ends):
            return _result("failed", "Quick failed: invalid episode index ranges")
        for start, end in zip(starts, ends):
            if start < 0 or end <= start:
                return _result("failed", "Quick failed: invalid episode index ranges")
            ranges.append((start, end))

    cursor = 0
    for start, end in sorted(ranges):
        if start != cursor:
            return _result(
                "failed",
                f"Quick failed: data rows {data_rows} do not match episode ranges",
            )
        cursor = end

    if cursor != data_rows:
        return _result(
            "failed",
            f"Quick failed: data rows {data_rows} do not match episode range end {cursor}",
        )
    return None


def _video_feature_keys(info: dict) -> list[str]:
    features = info.get("features", {})
    if not isinstance(features, dict):
        return []
    return [
        key for key, meta in features.items()
        if isinstance(meta, dict) and meta.get("dtype") == "video"
    ]


def _is_video_dataset(info: dict) -> bool:
    return bool(info.get("video_path")) or bool(_video_feature_keys(info))


def _video_files(dataset_dir: Path) -> list[Path]:
    videos_dir = dataset_dir / "videos"
    if not videos_dir.is_dir():
        return []
    return sorted(videos_dir.rglob("*.mp4"))


def _parse_version(version: object) -> tuple[int, ...] | None:
    if not isinstance(version, str) or not version.strip():
        return None
    parts: list[int] = []
    for token in version.split("."):
        digits = ""
        for char in token:
            if char.isdigit():
                digits += char
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) if parts else None


def _version_is_supported(version: object) -> bool:
    parsed = _parse_version(version)
    if parsed is None:
        return False
    padded = parsed + (0,) * max(0, len(MIN_LEROBOT_VERSION) - len(parsed))
    return padded >= MIN_LEROBOT_VERSION


def _task_name_lookup(tasks_table) -> dict[str, int]:
    task_text_column = get_task_text_column_name(tasks_table)
    if task_text_column is None:
        raise ValueError("tasks.parquet missing task text column")

    return {
        str(task_name): int(task_index)
        for task_index, task_name in zip(
            tasks_table.column("task_index").to_pylist(),
            tasks_table.column(task_text_column).to_pylist(),
            strict=True,
        )
    }


def _episode_task_index_sets(
    table,
    task_name_lookup: dict[str, int],
) -> list[frozenset[int]]:
    if "task_index" in table.column_names:
        return [frozenset({int(value)}) for value in table.column("task_index").to_pylist()]

    index_sets: list[frozenset[int]] = []
    for row_tasks in table.column("tasks").to_pylist():
        if isinstance(row_tasks, list):
            task_names = row_tasks
        else:
            task_names = [row_tasks]

        if not task_names:
            raise ValueError("episode tasks list is empty")

        resolved: set[int] = set()
        for task_name in task_names:
            try:
                resolved.add(task_name_lookup[str(task_name)])
            except KeyError as exc:
                raise ValueError(f"unknown episode task {task_name!r}") from exc
        index_sets.append(frozenset(resolved))
    return index_sets


def _episode_segments(
    episode_tables: list,
    task_name_lookup: dict[str, int],
) -> list[dict]:
    segments: list[dict] = []
    for table in episode_tables:
        starts = [int(value) for value in table.column("dataset_from_index").to_pylist()]
        ends = [int(value) for value in table.column("dataset_to_index").to_pylist()]
        episode_indices = [int(value) for value in table.column("episode_index").to_pylist()]
        task_index_sets = _episode_task_index_sets(table, task_name_lookup)
        chunk_indices = [int(value) for value in table.column("data/chunk_index").to_pylist()]
        file_indices = [int(value) for value in table.column("data/file_index").to_pylist()]
        segments.extend(
            {
                "start": start,
                "end": end,
                "episode_index": episode_index,
                "task_indices": task_indices,
                "data_chunk_index": chunk_index,
                "data_file_index": file_index,
            }
            for start, end, episode_index, task_indices, chunk_index, file_index in zip(
                starts,
                ends,
                episode_indices,
                task_index_sets,
                chunk_indices,
                file_indices,
            )
        )
    return sorted(segments, key=lambda item: item["start"])


def _validate_data_references(
    data_tables: list,
    episode_tables: list,
    data_rows: int,
    task_name_lookup: dict[str, int],
) -> ValidationResult | None:
    try:
        segments = _episode_segments(episode_tables, task_name_lookup)
    except ValueError as exc:
        return _result("failed", f"Full failed: {exc}")
    data_refs: list[tuple[int, int, int]] = []
    for table in data_tables:
        indices = [int(value) for value in table.column("index").to_pylist()]
        episode_indices = [int(value) for value in table.column("episode_index").to_pylist()]
        task_indices = [int(value) for value in table.column("task_index").to_pylist()]
        data_refs.extend(zip(indices, episode_indices, task_indices))

    data_refs.sort(key=lambda item: item[0])
    expected_indices = list(range(data_rows))
    actual_indices = [index for index, _, _ in data_refs]
    if actual_indices != expected_indices:
        return _result("failed", "Full failed: data index reference mismatch")

    segment_index = 0
    for row_index, episode_index, task_index in data_refs:
        while segment_index < len(segments) and row_index >= segments[segment_index]["end"]:
            segment_index += 1
        if segment_index >= len(segments) or row_index < segments[segment_index]["start"]:
            return _result("failed", "Full failed: data index reference mismatch")
        segment = segments[segment_index]
        if episode_index != segment["episode_index"]:
            return _result("failed", "Full failed: data episode reference mismatch")
        if task_index not in segment["task_indices"]:
            return _result("failed", "Full failed: data task reference mismatch")
    return None


def _validate_videos_accessible(
    dataset_dir: Path,
    info: dict,
    episode_tables: list,
    task_name_lookup: dict[str, int],
) -> ValidationResult | None:
    actual_video_files = _video_files(dataset_dir)
    if not actual_video_files:
        return _result("failed", "Full failed: video dataset missing videos/**/*.mp4")

    expected_paths: list[Path] = []
    video_path_template = info.get("video_path")
    video_keys = _video_feature_keys(info)
    if isinstance(video_path_template, str) and video_keys:
        seen_paths: set[Path] = set()
        for segment in _episode_segments(episode_tables, task_name_lookup):
            for video_key in video_keys:
                rel_path = video_path_template.format(
                    video_key=video_key,
                    chunk_index=segment["data_chunk_index"],
                    file_index=segment["data_file_index"],
                )
                full_path = dataset_dir / rel_path
                if full_path not in seen_paths:
                    seen_paths.add(full_path)
                    expected_paths.append(full_path)
    else:
        expected_paths = actual_video_files

    for path in expected_paths:
        if not path.is_file():
            return _result("failed", f"Full failed: missing video file {path.relative_to(dataset_dir)}")
        try:
            with path.open("rb") as handle:
                if not handle.read(1):
                    return _result("failed", f"Full failed: video file is not accessible {path.relative_to(dataset_dir)}")
        except OSError as exc:
            return _result("failed", f"Full failed: video file is not accessible {path.relative_to(dataset_dir)} ({exc})")
    return None


def _validate_quick_dataset(dataset_dir: Path) -> ValidationResult:
    if not dataset_dir.is_dir():
        return _result("failed", f"Quick failed: missing dataset directory {dataset_dir}")

    info_path = dataset_dir / "meta" / "info.json"
    tasks_path = dataset_dir / "meta" / "tasks.parquet"
    episodes_dir = dataset_dir / "meta" / "episodes"
    data_dir = dataset_dir / "data"

    required_paths = [
        (info_path, "meta/info.json"),
        (tasks_path, "meta/tasks.parquet"),
    ]
    for path, rel_path in required_paths:
        if not path.is_file():
            return _result("failed", f"Quick failed: missing required file {rel_path}")

    if not episodes_dir.is_dir():
        return _result("failed", "Quick failed: missing required directory meta/episodes")
    if not data_dir.is_dir():
        return _result("failed", "Quick failed: missing required directory data")

    episode_files = _parquet_files(episodes_dir)
    if not episode_files:
        return _result("failed", "Quick failed: no episode parquet files under meta/episodes")

    data_files = _parquet_files(data_dir)
    if not data_files:
        return _result("failed", "Quick failed: no data parquet files under data")

    try:
        info = _read_info_json(info_path)
    except (OSError, json.JSONDecodeError) as exc:
        return _result("failed", f"Quick failed: could not read meta/info.json ({exc})")

    missing_info_keys = sorted(REQUIRED_INFO_KEYS - set(info))
    if missing_info_keys:
        return _result(
            "failed",
            "Quick failed: missing info.json keys " + ", ".join(missing_info_keys),
        )

    try:
        tasks_table = _read_parquet_table(tasks_path)
    except Exception as exc:
        return _result("failed", f"Quick failed: could not read meta/tasks.parquet ({exc})")

    if not has_required_task_columns(tasks_table):
        return _result("failed", "Quick failed: tasks.parquet missing required columns")
    if tasks_table.num_rows < 1:
        return _result(
            "failed",
            "Quick failed: tasks.parquet must contain at least 1 task",
        )

    episode_error, episode_tables = _validate_episode_columns(episode_files)
    if episode_error is not None:
        return episode_error

    data_error = _validate_required_parquet_schemas(
        data_files,
        REQUIRED_DATA_COLUMNS,
        "data",
    )
    if data_error is not None:
        return data_error

    try:
        total_episodes = int(info["total_episodes"])
        total_frames = int(info["total_frames"])
    except (TypeError, ValueError) as exc:
        return _result("failed", f"Quick failed: invalid info.json totals ({exc})")
    if total_episodes < 1:
        return _result(
            "failed",
            "Quick failed: info total_episodes must be at least 1 episode",
        )
    if total_frames < 1:
        return _result(
            "failed",
            "Quick failed: info total_frames must be at least 1 frame",
        )

    episode_rows = _count_parquet_rows(episode_files)
    data_rows = _count_parquet_rows(data_files)
    if episode_rows < 1:
        return _result(
            "failed",
            "Quick failed: episode parquet must contain at least 1 episode",
        )
    if data_rows < 1:
        return _result(
            "failed",
            "Quick failed: data parquet must contain at least 1 frame",
        )

    if episode_rows != total_episodes:
        return _result(
            "failed",
            f"Quick failed: info total_episodes={total_episodes} but episodes parquet has {episode_rows} rows",
        )

    if data_rows != total_frames:
        return _result(
            "failed",
            f"Quick failed: info total_frames={total_frames} but data parquet has {data_rows} rows",
        )

    range_error = _validate_episode_ranges(episode_tables, data_rows)
    if range_error is not None:
        return range_error

    if _is_video_dataset(info) and not _video_files(dataset_dir):
        return _result("failed", "Quick failed: video dataset requires at least one videos/**/*.mp4")

    return _result("passed", f"Quick passed: {episode_rows} episodes, 0 warnings")


def _validate_quick(cell_task: str) -> ValidationResult:
    return _validate_quick_dataset(_dataset_dir_for(cell_task))


def _local_repo_id(dataset_dir: Path) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]", "-", dataset_dir.name or "dataset")
    return f"local/{name}"


def _isolated_module_copy(module):
    """Load a private copy so local-only patches never touch shared loaders."""
    module_file = getattr(module, "__file__", None)
    package = getattr(module, "__package__", None)
    if not isinstance(module_file, str) or not isinstance(package, str):
        return module
    isolated_name = f"{package}._robodata_local_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(isolated_name, module_file)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot isolate official loader module {module_file}")
    isolated = importlib.util.module_from_spec(spec)
    sys.modules[isolated_name] = isolated
    try:
        spec.loader.exec_module(isolated)
    finally:
        sys.modules.pop(isolated_name, None)
    return isolated


def _isolate_current_loader_modules(module):
    """Clone both current loader modules and bind the private metadata class."""
    isolated_dataset = _isolated_module_copy(module)
    if (
        isolated_dataset is module
        and isinstance(getattr(module, "__file__", None), str)
    ):
        raise ImportError("official dataset loader module was not isolated")

    metadata_cls = getattr(isolated_dataset, "LeRobotDatasetMetadata", None)
    if metadata_cls is None:
        return isolated_dataset, None
    metadata_module_name = getattr(metadata_cls, "__module__", None)
    if not isinstance(metadata_module_name, str):
        raise ImportError("official metadata loader has no importable identity")

    metadata_source = importlib.import_module(metadata_module_name)
    isolated_metadata = _isolated_module_copy(metadata_source)
    if (
        isolated_metadata is metadata_source
        and isinstance(getattr(metadata_source, "__file__", None), str)
    ):
        raise ImportError("official metadata loader module was not isolated")
    isolated_metadata_cls = getattr(
        isolated_metadata,
        "LeRobotDatasetMetadata",
        None,
    )
    if isolated_metadata_cls is None:
        raise ImportError("isolated metadata loader has no LeRobotDatasetMetadata")

    setattr(
        isolated_dataset,
        "LeRobotDatasetMetadata",
        isolated_metadata_cls,
    )
    return isolated_dataset, isolated_metadata


def _requested_module_is_missing(
    exc: ModuleNotFoundError,
    requested_module: str,
) -> bool:
    return exc.name == requested_module


def _install_local_only_loader_guards(module, metadata_module) -> None:
    """Guard private loader clones against every known Hub access path."""

    def reject_remote_access(*_args, **_kwargs):
        raise RuntimeError(
            "offline local-only validation refused remote access"
        )

    dataset_cls = getattr(module, "LeRobotDataset", None)
    metadata_cls = getattr(module, "LeRobotDatasetMetadata", None)
    if callable(getattr(dataset_cls, "_download", None)):
        setattr(dataset_cls, "_download", reject_remote_access)
    if callable(getattr(metadata_cls, "_pull_from_repo", None)):
        setattr(metadata_cls, "_pull_from_repo", reject_remote_access)
    for owner in (module, metadata_module):
        if owner is None:
            continue
        for remote_name in (
            "get_safe_version",
            "snapshot_download",
            "hf_hub_download",
        ):
            if callable(getattr(owner, remote_name, None)):
                setattr(owner, remote_name, reject_remote_access)


def _run_official_loader_smoke_test(dataset_dir: Path) -> tuple[ValidationStatus, str]:
    try:
        lerobot_root = importlib.import_module("lerobot")
    except ModuleNotFoundError as exc:
        if _requested_module_is_missing(exc, "lerobot"):
            return ("partial", LOADER_SKIP_SUMMARY)
        return ("failed", f"Full failed: official loader import failed ({exc})")
    except Exception as exc:
        return ("failed", f"Full failed: official loader import failed ({exc})")

    if not _version_is_supported(getattr(lerobot_root, "__version__", None)):
        return ("partial", LOADER_SKIP_SUMMARY)

    dataset_cls = None
    loader_api: Literal["current", "legacy"] | None = None
    metadata_module = None
    for module_name, api_name in (
        ("lerobot.datasets.lerobot_dataset", "current"),
        ("lerobot.common.datasets.lerobot_dataset", "legacy"),
    ):
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if _requested_module_is_missing(exc, module_name):
                continue
            return ("failed", f"Full failed: official loader import failed ({exc})")
        except Exception as exc:
            return ("failed", f"Full failed: official loader import failed ({exc})")
        candidate = getattr(module, "LeRobotDataset", None)
        if candidate is not None:
            if api_name == "current":
                try:
                    module, metadata_module = (
                        _isolate_current_loader_modules(module)
                    )
                except Exception as exc:
                    return (
                        "failed",
                        f"Full failed: official loader isolation failed ({exc})",
                    )
                candidate = getattr(module, "LeRobotDataset", None)
                if candidate is None:
                    return (
                        "failed",
                        "Full failed: official loader isolation failed "
                        "(isolated module has no LeRobotDataset)",
                    )
            dataset_cls = candidate
            loader_api = api_name
            break

    if dataset_cls is None or loader_api is None:
        return ("partial", LOADER_SKIP_SUMMARY)

    try:
        if loader_api == "current":
            _install_local_only_loader_guards(module, metadata_module)
            dataset_cls(
                repo_id=_local_repo_id(dataset_dir),
                root=dataset_dir,
                download_videos=False,
            )
        else:
            from_local = getattr(dataset_cls, "from_local", None)
            if not callable(from_local):
                return ("partial", LOADER_SKIP_SUMMARY)
            from_local(str(dataset_dir))
    except Exception as exc:
        return ("failed", f"Full failed: official loader smoke test failed ({exc})")
    return ("passed", "Full passed: dataset OK, official loader smoke test passed")


def _validate_full_dataset(dataset_dir: Path) -> ValidationResult:
    quick_result = _validate_quick_dataset(dataset_dir)
    if quick_result.status != "passed":
        return _result("failed", quick_result.summary.replace("Quick", "Full", 1))

    info = _read_info_json(dataset_dir / "meta" / "info.json")
    tasks_table = _read_parquet_table(dataset_dir / "meta" / "tasks.parquet")
    episode_files = _parquet_files(dataset_dir / "meta" / "episodes")
    data_files = _parquet_files(dataset_dir / "data")
    episode_tables = [_read_parquet_table(path) for path in episode_files]
    data_tables = [_read_parquet_table(path) for path in data_files]
    data_rows = _count_parquet_rows(data_files)
    try:
        task_name_lookup = _task_name_lookup(tasks_table)
    except ValueError as exc:
        return _result("failed", f"Full failed: {exc}")
    episode_task_indices: set[int] = set()
    for episode_table in episode_tables:
        try:
            for indices in _episode_task_index_sets(episode_table, task_name_lookup):
                episode_task_indices.update(indices)
        except ValueError as exc:
            return _result("failed", f"Full failed: {exc}")

    task_indices = _task_index_set(tasks_table.column("task_index").to_pylist())
    expected_task_indices = set(range(tasks_table.num_rows))
    if task_indices != expected_task_indices or episode_task_indices != task_indices:
        return _result("failed", "Full failed: task index mismatch between tasks.parquet and episodes")

    data_reference_error = _validate_data_references(
        data_tables,
        episode_tables,
        data_rows,
        task_name_lookup,
    )
    if data_reference_error is not None:
        return data_reference_error

    if _is_video_dataset(info):
        video_error = _validate_videos_accessible(
            dataset_dir,
            info,
            episode_tables,
            task_name_lookup,
        )
        if video_error is not None:
            return video_error

    loader_status, loader_summary = _run_official_loader_smoke_test(dataset_dir)
    return _result(loader_status, loader_summary)


def _validate_full(cell_task: str) -> ValidationResult:
    return _validate_full_dataset(_dataset_dir_for(cell_task))


def _run_validation_sync(cell_task: str, mode: ValidationMode) -> dict[str, str]:
    ensure_not_running(cell_task, mode)
    result = _validate_quick(cell_task) if mode == "quick" else _validate_full(cell_task)
    _persist_result(cell_task, mode, result)
    return result.as_dict()


def run_quick_validation_sync(cell_task: str) -> dict[str, str]:
    return _run_validation_sync(cell_task, "quick")


def run_quick_validation_for_path_sync(dataset_dir: Path) -> dict[str, str]:
    """Run quick validation for an explicit path without persisting state."""
    return _validate_quick_dataset(dataset_dir).as_dict()


def run_full_validation_sync(cell_task: str) -> dict[str, str]:
    return _run_validation_sync(cell_task, "full")


def run_full_validation_for_path_sync(dataset_dir: Path) -> dict[str, str]:
    """Run full validation for an explicit dataset path without persisting state."""
    return _validate_full_dataset(dataset_dir).as_dict()


async def mark_validation_running(
    cell_task: str,
    mode: ValidationMode,
) -> None:
    lock = _lock_for(cell_task, mode)
    ensure_not_running(cell_task, mode)
    await lock.acquire()
    try:
        result = ValidationResult(
            status="running",
            summary="Quick check running" if mode == "quick" else "Full check running",
            checked_at=_now_iso(),
        )
        await asyncio.to_thread(_persist_running_result, cell_task, mode, result)
    except Exception:
        lock.release()
        raise
