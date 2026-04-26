"""Dataset split/merge/delete helpers backed by LeRobot dataset tools.

The public functions keep the curation-tools service contract stable while
delegating dataset rewriting to ``lerobot.datasets.dataset_tools``.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import re
from glob import glob
from pathlib import Path
from typing import Any, Callable

import pyarrow as pa
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _LeRobotDatasetTools:
    LeRobotDataset: type
    delete_episodes: Callable[..., Any]
    split_dataset: Callable[..., Any]
    merge_datasets: Callable[..., Any]


def _load_lerobot_dataset_tools() -> _LeRobotDatasetTools:
    """Load LeRobot dataset APIs lazily so import-time errors stay actionable."""
    try:
        from lerobot.datasets.dataset_tools import (
            delete_episodes as lerobot_delete_episodes,
            merge_datasets as lerobot_merge_datasets,
            split_dataset as lerobot_split_dataset,
        )
        from lerobot.datasets.lerobot_dataset import LeRobotDataset
    except ImportError as exc:
        raise RuntimeError(
            "LeRobot dataset tools are required for split/merge/delete operations. "
            "Install the project dependencies so the 'lerobot' package is available."
        ) from exc

    return _LeRobotDatasetTools(
        LeRobotDataset=LeRobotDataset,
        delete_episodes=lerobot_delete_episodes,
        split_dataset=lerobot_split_dataset,
        merge_datasets=lerobot_merge_datasets,
    )


def _repo_id_for_path(path: Path) -> str:
    """Create a local-only repo id acceptable to LeRobot from an arbitrary path."""
    name = re.sub(r"[^A-Za-z0-9._-]", "-", Path(path).name or "dataset")
    return f"local/{name}"


def _open_lerobot_dataset(tools: _LeRobotDatasetTools, dataset_root: Path) -> Any:
    return tools.LeRobotDataset(
        repo_id=_repo_id_for_path(dataset_root),
        root=dataset_root,
    )


# ---------------------------------------------------------------------------
# Read utilities
# ---------------------------------------------------------------------------


def read_info(dataset_root: Path) -> dict:
    """Read meta/info.json and return as dict."""
    info_path = dataset_root / "meta" / "info.json"
    with info_path.open("r", encoding="utf-8") as fh:
        content = fh.read().rstrip("\x00")
        return json.loads(content)


def read_episodes(dataset_root: Path) -> pa.Table:
    """Read all meta/episodes/chunk-*/file-*.parquet into a single Table, sorted by episode_index."""
    pattern = str(dataset_root / "meta" / "episodes" / "chunk-*" / "file-*.parquet")
    files = sorted(glob(pattern))
    if not files:
        return pa.table({"episode_index": pa.array([], type=pa.int64())})
    tables = [pq.read_table(f) for f in files]
    combined = pa.concat_tables(tables, promote_options="default")
    indices = combined.column("episode_index").to_pylist()
    sort_order = sorted(range(len(indices)), key=lambda i: indices[i])
    return combined.take(sort_order)


def read_tasks(dataset_root: Path) -> pa.Table:
    """Read meta/tasks.parquet."""
    tasks_path = dataset_root / "meta" / "tasks.parquet"
    if not tasks_path.exists():
        return pa.table({"task_index": pa.array([], type=pa.int64())})
    return pq.read_table(str(tasks_path))


def get_camera_keys(info: dict) -> list[str]:
    """Extract camera keys from info features dict."""
    features = info.get("features", {})
    return [
        key for key in features
        if key.startswith("observation.images.") or key.startswith("observation.image.")
    ]


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------


def delete_episodes(
    dataset_root: Path,
    episode_ids: list[int],
    output_dir: Path,
) -> Path:
    """Delete specified episodes and write the edited LeRobot dataset to output_dir."""
    tools = _load_lerobot_dataset_tools()
    dataset = _open_lerobot_dataset(tools, dataset_root)

    tools.delete_episodes(
        dataset=dataset,
        episode_indices=episode_ids,
        output_dir=output_dir,
        repo_id=_repo_id_for_path(output_dir),
    )

    logger.info("Deleted %d episodes from %s -> %s", len(episode_ids), dataset_root, output_dir)
    return output_dir


def split_dataset(
    dataset_root: Path,
    episode_ids: list[int],
    output_dir: Path,
) -> Path:
    """Extract specified episodes into a new LeRobot dataset at output_dir."""
    tools = _load_lerobot_dataset_tools()
    dataset = _open_lerobot_dataset(tools, dataset_root)
    split_name = output_dir.name or "split"

    tools.split_dataset(
        dataset=dataset,
        splits={split_name: episode_ids},
        output_dir=output_dir.parent,
    )

    logger.info("Split %d episodes from %s -> %s", len(episode_ids), dataset_root, output_dir)
    return output_dir


def merge_datasets(
    dataset_roots: list[Path],
    output_dir: Path,
) -> Path:
    """Merge multiple LeRobot datasets into one at output_dir."""
    if not dataset_roots:
        raise ValueError("No datasets to merge")

    tools = _load_lerobot_dataset_tools()
    datasets = [_open_lerobot_dataset(tools, root) for root in dataset_roots]

    tools.merge_datasets(
        datasets=datasets,
        output_repo_id=_repo_id_for_path(output_dir),
        output_dir=output_dir,
    )

    logger.info(
        "Merged %d datasets -> %s",
        len(dataset_roots), output_dir,
    )
    return output_dir
