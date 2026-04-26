"""Access layer for worker_controls and worker_heartbeats."""
from __future__ import annotations
import json
from typing import Any, Mapping
from backend.core.db import db


async def list_workers() -> list[Mapping[str, Any]]:
    rows = await db.fetch_all(
        "SELECT c.worker_id, c.desired_state, c.updated_by, c.updated_at, c.note, "
        "       h.actual_state, h.pid, h.container_id, h.last_beat_at, "
        "       h.in_flight_job_id, h.detail "
        "FROM worker_controls c "
        "LEFT JOIN worker_heartbeats h USING (worker_id) "
        "ORDER BY c.worker_id"
    )
    return [_decode_worker(row) for row in rows]


async def get_worker(worker_id: str) -> Mapping[str, Any] | None:
    row = await db.fetch_one(
        "SELECT c.worker_id, c.desired_state, c.updated_by, c.updated_at, c.note, "
        "       h.actual_state, h.pid, h.container_id, h.last_beat_at, "
        "       h.in_flight_job_id, h.detail "
        "FROM worker_controls c "
        "LEFT JOIN worker_heartbeats h USING (worker_id) "
        "WHERE c.worker_id = $1",
        worker_id,
    )
    return _decode_worker(row) if row is not None else None


async def set_desired_state(
    *, worker_id: str, desired_state: str,
    updated_by: str | None, note: str | None,
) -> None:
    await db.execute(
        "UPDATE worker_controls "
        "SET desired_state=$2, updated_by=$3, note=$4, updated_at=NOW() "
        "WHERE worker_id=$1",
        worker_id, desired_state, updated_by, note,
    )


async def upsert_heartbeat(
    *, worker_id: str, actual_state: str, pid: int | None,
    container_id: str | None, in_flight_job_id: int | None,
    detail: Mapping[str, Any] | None,
) -> None:
    await db.execute(
        "INSERT INTO worker_heartbeats "
        "(worker_id, actual_state, pid, container_id, in_flight_job_id, detail) "
        "VALUES($1,$2,$3,$4,$5,$6::jsonb) "
        "ON CONFLICT(worker_id) DO UPDATE "
        "SET actual_state=excluded.actual_state, pid=excluded.pid, "
        "    container_id=excluded.container_id, "
        "    in_flight_job_id=excluded.in_flight_job_id, "
        "    detail=excluded.detail, last_beat_at=NOW()",
        worker_id, actual_state, pid, container_id, in_flight_job_id,
        json.dumps(dict(detail or {})),
    )


async def is_stale(worker_id: str, *, ttl_seconds: int) -> bool:
    row = await db.fetch_one(
        "SELECT last_beat_at < NOW() - ($2 * interval '1 second') AS stale "
        "FROM worker_heartbeats WHERE worker_id = $1",
        worker_id, ttl_seconds,
    )
    if row is None:
        return True
    return bool(row["stale"])


def _decode_worker(row: Mapping[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    if isinstance(decoded.get("detail"), str):
        decoded["detail"] = json.loads(decoded["detail"])
    return decoded
