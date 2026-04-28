"""Service for exporting curated datasets by filtering out excluded-grade episodes.

MVP approach: copies files rather than rewriting data parquets.
Keeps original chunk/file structure and indices.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from backend.datasets.services.dataset_service import dataset_service
from backend.datasets.services.dataset_registry import dataset_registry
from backend.datasets.services.episode_service import (
    _load_sidecar_json, _ensure_dataset_registered, _ensure_migrated, _load_annotations_from_db,
)

logger = logging.getLogger(__name__)


def export_dataset(
    output_path: str,
    exclude_grades: list[str],
    dataset_path: str | None = None,
) -> dict:
    """Export a dataset, filtering out episodes with excluded grades.

    Returns dict with output_path, total_episodes, and excluded_count.
    """
    ctx = dataset_registry.get(dataset_path) if dataset_path is not None else dataset_service
    ds_path = ctx.dataset_path
    info = ctx.get_info()
    episodes = ctx.get_episodes()
    features = info.get("features", {})

    # Read grades from DB (replaces sidecar).
    # export_dataset is sync but may be called from within an async context.
    # We run the async DB helpers in a dedicated thread with its own event loop
    # to avoid "event loop already running" errors.
    import concurrent.futures

    def _fetch_annotations():
        async def _inner():
            _dataset_id = await _ensure_dataset_registered(ds_path)
            await _ensure_migrated(_dataset_id, ds_path)
            return _dataset_id, await _load_annotations_from_db(_dataset_id)

        return asyncio.run(_inner())

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _pool:
        dataset_id, annotations = _pool.submit(_fetch_annotations).result()

    episode_grades: dict[int, str | None] = {}
    for ep in episodes:
        ep_idx = ep["episode_index"]
        episode_grades[ep_idx] = ep.get("grade")
    # DB annotations take priority
    for ep_idx, ann in annotations.items():
        grade = ann.get("grade")
        if grade is not None:
            episode_grades[ep_idx] = grade

    # Determine which episodes to keep
    exclude_set = {g.strip().lower() for g in exclude_grades}
    kept_episodes = []
    excluded_count = 0
    for ep in episodes:
        ep_idx = ep["episode_index"]
        grade = episode_grades.get(ep_idx)
        normalized = grade.strip().lower() if grade else None
        if normalized in exclude_set:
            excluded_count += 1
        else:
            kept_episodes.append(ep)

    kept_indices = {ep["episode_index"] for ep in kept_episodes}

    out = Path(output_path).resolve()
    if out.exists():
        raise ValueError(f"Output path already exists: {out}")

    # Create directory structure
    out.mkdir(parents=True, exist_ok=True)
    (out / "meta" / "episodes").mkdir(parents=True)
    (out / "data").mkdir(parents=True)

    # 1. Write meta/info.json with updated total_episodes
    new_info = dict(info)
    new_info["total_episodes"] = len(kept_episodes)
    (out / "meta" / "info.json").write_text(
        json.dumps(new_info, ensure_ascii=False, indent=2)
    )

    # 2. Copy meta/tasks.parquet as-is
    src_tasks = ds_path / "meta" / "tasks.parquet"
    if src_tasks.exists():
        shutil.copy2(src_tasks, out / "meta" / "tasks.parquet")

    # 3. Write new episode metadata parquet files (only kept episodes)
    for src_file in ctx.iter_episode_parquet_files():
        table = pq.read_table(src_file)
        ep_col = table.column("episode_index").to_pylist()
        mask = [idx in kept_indices for idx in ep_col]
        filtered = table.filter(mask)
        if filtered.num_rows == 0:
            continue
        # Preserve the relative path under meta/episodes/
        rel = src_file.relative_to(ds_path / "meta" / "episodes")
        dest = out / "meta" / "episodes" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(filtered, dest)

    # 4. Copy data parquet files referenced by kept episodes
    _copy_data_files(ds_path, out, kept_episodes)

    # 5. Copy video files referenced by kept episodes
    _copy_video_files(ds_path, out, kept_episodes, features)

    logger.info(
        "Exported %d episodes to %s (excluded %d)",
        len(kept_episodes), out, excluded_count,
    )
    return {
        "output_path": str(out),
        "total_episodes": len(kept_episodes),
        "excluded_count": excluded_count,
    }


def _copy_data_files(
    src_root: Path, dst_root: Path, kept_episodes: list[dict]
) -> None:
    """Copy data parquet files referenced by kept episodes."""
    seen: set[tuple[int, int]] = set()
    for ep in kept_episodes:
        chunk = ep.get("data/chunk_index", 0)
        file = ep.get("data/file_index", 0)
        key = (chunk, file)
        if key in seen:
            continue
        seen.add(key)

        src = src_root / "data" / f"chunk-{chunk:03d}" / f"file-{file:03d}.parquet"
        if not src.exists():
            logger.warning("Data file not found, skipping: %s", src)
            continue
        dest = dst_root / "data" / f"chunk-{chunk:03d}" / f"file-{file:03d}.parquet"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)


def _copy_video_files(
    src_root: Path,
    dst_root: Path,
    kept_episodes: list[dict],
    features: dict,
) -> None:
    """Copy video MP4 files referenced by kept episodes."""
    camera_keys = [
        key for key in features
        if key.startswith("observation.images.") or key.startswith("observation.image.")
    ]
    if not camera_keys:
        return

    seen: set[tuple[str, int, int]] = set()
    for ep in kept_episodes:
        for cam_key in camera_keys:
            chunk_col = f"videos/{cam_key}/chunk_index"
            file_col = f"videos/{cam_key}/file_index"
            chunk = ep.get(chunk_col)
            file = ep.get(file_col)
            if chunk is None or file is None:
                continue
            key = (cam_key, chunk, file)
            if key in seen:
                continue
            seen.add(key)

            src = (
                src_root / "videos" / cam_key
                / f"chunk-{chunk:03d}" / f"file-{file:03d}.mp4"
            )
            if not src.exists():
                logger.debug("Video file not found, skipping: %s", src)
                continue
            dest = (
                dst_root / "videos" / cam_key
                / f"chunk-{chunk:03d}" / f"file-{file:03d}.mp4"
            )
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
