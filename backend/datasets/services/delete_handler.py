"""Job handler for dataset delete-episodes operations."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from backend.datasets.services import dataset_ops_engine as engine
from backend.jobs.runner_helpers import run_in_place_with_rollback


def _episode_ids(payload: Mapping[str, Any]) -> list[int]:
    value = payload.get("episode_ids")
    if not isinstance(value, list) or not value:
        raise ValueError("episode_ids is required")
    return [int(item) for item in value]


async def handle_delete(job: Mapping[str, Any]) -> dict[str, str]:
    payload = job.get("payload") or {}
    source_raw = payload.get("source_path")
    if not isinstance(source_raw, str) or not source_raw:
        raise ValueError("source_path is required")
    source = Path(source_raw)
    episode_ids = _episode_ids(payload)
    output_dir_raw = payload.get("output_dir")
    if isinstance(output_dir_raw, str) and output_dir_raw:
        output_path = Path(output_dir_raw)
        engine.delete_episodes(source, episode_ids, output_path)
    else:
        run_in_place_with_rollback(
            source,
            lambda src, dst: engine.delete_episodes(src, episode_ids, dst),
        )
        output_path = source
    return {"result_path": str(output_path)}
