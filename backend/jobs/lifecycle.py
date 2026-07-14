"""Lifecycle policy for rows in the unified jobs queue."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from backend.core.db import db


_TERMINAL = {"complete", "failed", "cancelled"}


class AlreadyTerminal(Exception):
    def __init__(self, current_status: str) -> None:
        super().__init__(f"job already terminal: {current_status}")
        self.current_status = current_status


@dataclass
class CancelledNormally:
    cleanup: str = ""


async def claim_next(worker_id: str, types: list[str]) -> dict[str, Any] | None:
    async with db.transaction():
        # Wrap `types` in an outer list so the DB facade's _pack_params heuristic
        # leaves the inner list intact for asyncpg to bind as the $1 text[].
        row = await db.fetch_one(
            "SELECT id, type, payload FROM jobs "
            "WHERE status='queued' AND type::text = ANY($1::text[]) "
            "ORDER BY created_at "
            "FOR UPDATE SKIP LOCKED LIMIT 1",
            [types],
        )
        if row is None:
            return None
        await db.execute(
            "UPDATE jobs SET status='running', worker_id=$2, started_at=NOW(), "
            "heartbeat_at=NOW(), updated_at=NOW() WHERE id=$1",
            row["id"],
            worker_id,
        )
        return _decode_claimed_job(row)


async def heartbeat(job_id: int) -> None:
    await db.execute(
        "UPDATE jobs SET heartbeat_at=NOW(), updated_at=NOW() WHERE id=$1",
        job_id,
    )


async def update_progress(job_id: int, progress: Mapping[str, Any]) -> None:
    await db.execute(
        "UPDATE jobs "
        "SET progress = $2::jsonb, heartbeat_at = NOW(), updated_at = NOW() "
        "WHERE id = $1 AND status IN ('running', 'cancel_requested')",
        job_id,
        _to_jsonb(progress),
    )


async def cancel_requested(job_id: int) -> bool:
    row = await db.fetch_one("SELECT status FROM jobs WHERE id=$1", job_id)
    return bool(row and row["status"] == "cancel_requested")


async def heartbeat_and_observe_cancel(job_id: int) -> bool:
    await heartbeat(job_id)
    return await cancel_requested(job_id)


async def request_cancel(job_id: int) -> Mapping[str, Any]:
    updated = await db.fetch_one(
        "UPDATE jobs "
        "SET status = 'cancelled', cancel_requested_at = NOW(), finished_at = NOW(), "
        "updated_at = NOW() "
        "WHERE id = $1 AND status = 'queued' "
        "RETURNING id, status",
        job_id,
    )
    if updated is not None:
        return dict(updated)

    updated = await db.fetch_one(
        "UPDATE jobs "
        "SET status = 'cancel_requested', cancel_requested_at = NOW(), updated_at = NOW() "
        "WHERE id = $1 AND status = 'running' "
        "RETURNING id, status",
        job_id,
    )
    if updated is not None:
        return dict(updated)

    row = await db.fetch_one("SELECT status FROM jobs WHERE id = $1", job_id)
    if row is None:
        raise LookupError(job_id)
    if row["status"] in _TERMINAL:
        raise AlreadyTerminal(current_status=row["status"])
    return {"id": job_id, "status": str(row["status"])}


async def complete(job_id: int, result: Mapping[str, Any] | None = None) -> None:
    if result is None:
        await db.execute(
            "UPDATE jobs SET "
            "status = CASE WHEN status='cancel_requested' "
            "THEN 'cancelled'::job_status ELSE 'complete'::job_status END, "
            "finished_at=NOW(), updated_at=NOW() "
            "WHERE id=$1 AND status IN ('running', 'cancel_requested')",
            job_id,
        )
        return
    await db.execute(
        "UPDATE jobs SET "
        "status = CASE WHEN status='cancel_requested' "
        "THEN 'cancelled'::job_status ELSE 'complete'::job_status END, "
        "finished_at=NOW(), updated_at=NOW(), "
        "result = CASE WHEN status='running' THEN $2::jsonb ELSE result END "
        "WHERE id=$1 AND status IN ('running', 'cancel_requested')",
        job_id,
        _to_jsonb(result),
    )


async def fail(job_id: int, error: str) -> None:
    await db.execute(
        "UPDATE jobs SET "
        "status = CASE WHEN status='cancel_requested' "
        "THEN 'cancelled'::job_status ELSE 'failed'::job_status END, "
        "finished_at=NOW(), updated_at=NOW(), "
        "error = CASE WHEN status='running' THEN $2 ELSE error END "
        "WHERE id=$1 AND status IN ('running', 'cancel_requested')",
        job_id,
        error,
    )


async def cancel_normally(job_id: int, cleanup: str = "") -> None:
    await db.execute(
        "UPDATE jobs SET status='cancelled', finished_at=NOW(), updated_at=NOW(), "
        "error=$2 WHERE id=$1 AND status IN ('running', 'cancel_requested')",
        job_id,
        cleanup,
    )


def _to_jsonb(payload: Mapping[str, Any]) -> str:
    return json.dumps(dict(payload))


def _decode_claimed_job(row: Mapping[str, Any]) -> dict[str, Any]:
    job = dict(row)
    if isinstance(job.get("payload"), str):
        job["payload"] = json.loads(job["payload"])
    return job
