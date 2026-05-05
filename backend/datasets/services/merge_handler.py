"""Job handler for dataset merge operations."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from backend.datasets.services import dataset_ops_engine as engine


async def handle_merge(job: Mapping[str, Any]) -> dict[str, str]:
    payload = job.get("payload") or {}
    source_paths_raw = payload.get("source_paths")
    if not isinstance(source_paths_raw, list) or not source_paths_raw:
        raise ValueError("source_paths is required")
    sources = [Path(str(path)) for path in source_paths_raw]
    target_name = payload.get("target_name")
    if not isinstance(target_name, str) or not target_name:
        raise ValueError("target_name is required")
    output_dir_raw = payload.get("output_dir")
    output_path = Path(output_dir_raw) if isinstance(output_dir_raw, str) and output_dir_raw else sources[0].parent / target_name
    engine.merge_datasets(sources, output_path)
    return {"result_path": str(output_path)}
