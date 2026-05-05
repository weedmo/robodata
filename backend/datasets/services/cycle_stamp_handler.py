"""Job handler for dataset cycle stamping."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Mapping

from backend.datasets.services import cycle_stamp_service
from backend.datasets.services.stamp_cycles_payload import StampCyclesPayload
from backend.jobs.runner_helpers import run_in_place_with_rollback


async def handle_stamp_cycles(job: Mapping[str, Any]) -> dict[str, str]:
    payload = StampCyclesPayload.model_validate(job.get("payload") or {})
    source = Path(payload.source_path)
    overwrite = payload.overwrite

    def stamp_into_copy(src: Path, dst: Path) -> None:
        shutil.copytree(src, dst)
        cycle_stamp_service.stamp_dataset_cycles(dst, overwrite=overwrite)

    run_in_place_with_rollback(source, stamp_into_copy)
    return {"result_path": str(source)}
