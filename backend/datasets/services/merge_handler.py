"""Job handler for dataset merge operations."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from backend.datasets.services import dataset_ops_engine as engine
from backend.datasets.services.merge_payload import MergePayload


async def handle_merge(job: Mapping[str, Any]) -> dict[str, str]:
    payload = MergePayload.model_validate(job.get("payload") or {})
    sources = [Path(p) for p in payload.source_paths]
    output_path = payload.output_path()
    engine.merge_datasets(sources, output_path)
    return {"result_path": str(output_path)}
