"""REST surface for the unified jobs queue."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from backend.jobs import repo


router = APIRouter(prefix="/api/jobs", tags=["jobs"])


class EnqueueBody(BaseModel):
    type: Literal["convert", "split", "merge", "delete", "sync_good_episodes", "stamp_cycles"]
    payload: dict[str, Any] = Field(default_factory=dict)
    dedupe_key: str | None = None


@router.post("", status_code=201)
async def post_job(
    body: EnqueueBody,
    x_user_name: str | None = Header(default=None),
) -> dict[str, Any]:
    try:
        row = await repo.enqueue(
            type_=body.type,
            payload=body.payload,
            dedupe_key=body.dedupe_key,
            requested_by=x_user_name,
        )
    except repo.DuplicateDedupe as e:
        raise HTTPException(
            status_code=409,
            detail={"error": "duplicate_dedupe_key", "existing_job_id": e.existing_job_id},
        )
    return dict(row)


@router.get("/{job_id}")
async def get_job(job_id: int) -> dict[str, Any]:
    row = await repo.fetch(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    return dict(row)


@router.get("")
async def list_jobs(
    type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    dataset_id: int | None = Query(default=None),
    since: datetime | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    rows = await repo.list_jobs(
        type_=type, status=status, dataset_id=dataset_id, since=since, limit=limit,
    )
    return [dict(r) for r in rows]


@router.post("/{job_id}/cancel", status_code=202)
async def cancel_job(job_id: int) -> dict[str, Any]:
    try:
        row = await repo.request_cancel(job_id)
    except LookupError:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    except repo.AlreadyTerminal as e:
        raise HTTPException(
            status_code=409,
            detail={"error": "already_terminal", "current_status": e.current_status},
        )
    return dict(row)
