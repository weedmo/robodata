"""Job handler for syncing selected good episodes."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from backend.datasets.services.rosbag_dataset_sync import load_sync_selected_episodes


def _episode_ids(payload: Mapping[str, Any]) -> list[int]:
    value = payload.get("episode_ids")
    if not isinstance(value, list) or not value:
        raise ValueError("episode_ids is required")
    return [int(item) for item in value]


async def handle_sync_good_episodes(job: Mapping[str, Any]) -> dict[str, object]:
    payload = job.get("payload") or {}
    source_raw = payload.get("source_path")
    destination_raw = payload.get("destination_path")
    if not isinstance(source_raw, str) or not source_raw:
        raise ValueError("source_path is required")
    if not isinstance(destination_raw, str) or not destination_raw:
        raise ValueError("destination_path is required")
    sync_selected_episodes = load_sync_selected_episodes()
    result = sync_selected_episodes(Path(source_raw), _episode_ids(payload), Path(destination_raw))
    return {
        "result_path": str(result.destination_path),
        "summary": {
            "mode": result.mode,
            "created": int(result.created),
            "skipped_duplicates": int(result.skipped_duplicates),
        },
    }
