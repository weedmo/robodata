"""Job handler for dataset split operations."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from backend.datasets.services import dataset_ops_engine as engine
from backend.datasets.services.split_payload import SplitPayload


async def handle_split(job: Mapping[str, Any]) -> dict[str, str]:
    payload = SplitPayload.model_validate(job.get("payload") or {})
    output_path = payload.output_path()
    engine.split_dataset(Path(payload.source_path), payload.episode_ids, output_path)
    return {"result_path": str(output_path)}
