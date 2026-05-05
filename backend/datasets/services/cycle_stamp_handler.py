"""Job handler for dataset cycle stamping."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Mapping

from backend.datasets.services import cycle_stamp_service
from backend.jobs.runner_helpers import run_in_place_with_rollback


def _payload_path(payload: Mapping[str, Any], key: str) -> Path:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return Path(value)


async def handle_stamp_cycles(job: Mapping[str, Any]) -> dict[str, str]:
    payload = job.get("payload") or {}
    source = _payload_path(payload, "source_path")
    overwrite = bool(payload.get("overwrite", False))

    def stamp_into_copy(src: Path, dst: Path) -> None:
        shutil.copytree(src, dst)
        cycle_stamp_service.stamp_dataset_cycles(dst, overwrite=overwrite)

    run_in_place_with_rollback(source, stamp_into_copy)
    return {"result_path": str(source)}
