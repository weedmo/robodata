"""Job handler for syncing selected good episodes."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from backend.datasets.services.rosbag_dataset_sync import load_sync_selected_episodes
from backend.datasets.services.sync_good_episodes_payload import SyncGoodEpisodesPayload


async def handle_sync_good_episodes(job: Mapping[str, Any]) -> dict[str, object]:
    payload = SyncGoodEpisodesPayload.model_validate(job.get("payload") or {})
    sync_selected_episodes = load_sync_selected_episodes()
    result = sync_selected_episodes(
        Path(payload.source_path),
        payload.episode_ids,
        Path(payload.destination_path),
    )
    return {
        "result_path": str(result.destination_path),
        "summary": {
            "mode": result.mode,
            "created": int(result.created),
            "skipped_duplicates": int(result.skipped_duplicates),
        },
    }
