"""Job handler for dataset split operations."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from backend.datasets.services import dataset_ops_engine as engine


def _payload_path(payload: Mapping[str, Any], key: str) -> Path:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return Path(value)


def _episode_ids(payload: Mapping[str, Any]) -> list[int]:
    value = payload.get("episode_ids")
    if not isinstance(value, list) or not value:
        raise ValueError("episode_ids is required")
    return [int(item) for item in value]


async def handle_split(job: Mapping[str, Any]) -> dict[str, str]:
    payload = job.get("payload") or {}
    source = _payload_path(payload, "source_path")
    target_name = payload.get("target_name")
    if not isinstance(target_name, str) or not target_name:
        raise ValueError("target_name is required")
    output_dir_raw = payload.get("output_dir")
    output_path = Path(output_dir_raw) if isinstance(output_dir_raw, str) and output_dir_raw else source.parent / target_name
    engine.split_dataset(source, _episode_ids(payload), output_path)
    return {"result_path": str(output_path)}
