"""REST surface for worker control plane."""
from __future__ import annotations
from typing import Any, Literal
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from backend.workers import repo, service


router = APIRouter(prefix="/api/workers", tags=["workers"])


class PatchBody(BaseModel):
    desired_state: Literal["running", "paused", "draining", "stopped"]
    note: str | None = None


@router.get("")
async def list_workers() -> list[dict[str, Any]]:
    return [dict(r) for r in await repo.list_workers()]


@router.get("/{worker_id}")
async def get_worker(worker_id: str) -> dict[str, Any]:
    row = await repo.get_worker(worker_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    return dict(row)


@router.patch("/{worker_id}")
async def patch_worker(
    worker_id: str, body: PatchBody,
    x_user_name: str | None = Header(default=None),
) -> dict[str, Any]:
    try:
        return await service.patch_desired_state(
            worker_id=worker_id, desired_state=body.desired_state,
            note=body.note, updated_by=x_user_name,
        )
    except service.WorkerNotFound:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    except service.WorkerStale:
        row = await repo.get_worker(worker_id)
        raise HTTPException(
            status_code=422,
            detail={
                "error": "worker_stale",
                "last_beat_at": row["last_beat_at"].isoformat() if row and row["last_beat_at"] else None,
            },
        )
    except service.IllegalTransition as e:
        raise HTTPException(
            status_code=422,
            detail={"error": "illegal_transition", "from": e.frm, "to": e.to},
        )
