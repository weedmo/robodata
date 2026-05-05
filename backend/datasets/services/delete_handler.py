"""Job handler for dataset delete-episodes operations."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from backend.datasets.services import dataset_ops_engine as engine
from backend.datasets.services.delete_payload import DeletePayload
from backend.jobs.runner_helpers import run_in_place_with_rollback


async def handle_delete(job: Mapping[str, Any]) -> dict[str, str]:
    payload = DeletePayload.model_validate(job.get("payload") or {})
    source = Path(payload.source_path)
    output_path = payload.output_path_or_none()
    if output_path is not None:
        engine.delete_episodes(source, payload.episode_ids, output_path)
    else:
        run_in_place_with_rollback(
            source,
            lambda src, dst: engine.delete_episodes(src, payload.episode_ids, dst),
        )
        output_path = source
    return {"result_path": str(output_path)}
