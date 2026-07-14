"""SQL access for the unified jobs queue."""
from __future__ import annotations
import json
import uuid
from datetime import datetime
from typing import Any, Mapping

import asyncpg

from backend.core.db import db
from backend.jobs import lifecycle


class DuplicateDedupe(Exception):
    def __init__(self, existing_job_id: int) -> None:
        super().__init__(f"duplicate dedupe key for job {existing_job_id}")
        self.existing_job_id = existing_job_id


AlreadyTerminal = lifecycle.AlreadyTerminal


async def enqueue(
    *,
    type_: str,
    payload: Mapping[str, Any],
    dedupe_key: str | None = None,
    requested_by: str | None = None,
) -> Mapping[str, Any]:
    if dedupe_key is not None:
        existing = await db.fetch_one(
            "SELECT id FROM jobs "
            "WHERE type = $1 AND dedupe_key = $2 "
            "AND status IN ('queued', 'running', 'cancel_requested')",
            type_, dedupe_key,
        )
        if existing is not None:
            raise DuplicateDedupe(existing_job_id=existing["id"])
    external_id = str(uuid.uuid4())
    try:
        row = await db.fetch_one(
            "INSERT INTO jobs (external_id, type, payload, dedupe_key) "
            "VALUES ($1, $2, $3::jsonb, $4) "
            "RETURNING id, external_id, type, status, dedupe_key, created_at",
            external_id, type_, _to_jsonb(payload), dedupe_key,
        )
    except asyncpg.UniqueViolationError:
        existing = await db.fetch_one(
            "SELECT id FROM jobs "
            "WHERE type = $1 AND dedupe_key = $2 "
            "AND status IN ('queued', 'running', 'cancel_requested')",
            type_, dedupe_key,
        )
        if existing is not None:
            raise DuplicateDedupe(existing_job_id=existing["id"])
        raise
    assert row is not None
    return dict(row)


async def fetch(job_id: int) -> Mapping[str, Any] | None:
    row = await db.fetch_one(
        "SELECT id, external_id, type, status, payload, progress, result, error, "
        "       attempts, worker_id, dedupe_key, created_at, updated_at, "
        "       started_at, heartbeat_at, cancel_requested_at, finished_at "
        "FROM jobs WHERE id = $1",
        job_id,
    )
    return _decode_job(row) if row is not None else None


async def fetch_by_external_id(external_id: str) -> Mapping[str, Any] | None:
    row = await db.fetch_one(
        "SELECT id, external_id, type, status, payload, progress, result, error, "
        "       attempts, worker_id, dedupe_key, created_at, updated_at, "
        "       started_at, heartbeat_at, cancel_requested_at, finished_at "
        "FROM jobs WHERE external_id = $1",
        external_id,
    )
    return _decode_job(row) if row is not None else None


async def list_jobs(
    *,
    type_: str | None = None,
    status: str | None = None,
    dataset_id: int | None = None,
    since: datetime | None = None,
    limit: int = 100,
) -> list[Mapping[str, Any]]:
    clauses: list[str] = []
    args: list[Any] = []
    if type_ is not None:
        args.append(type_)
        clauses.append(f"type = ${len(args)}")
    if status is not None:
        args.append(status)
        clauses.append(f"status = ${len(args)}")
    if dataset_id is not None:
        args.append(str(dataset_id))
        clauses.append(f"payload->>'dataset_id' = ${len(args)}")
    if since is not None:
        args.append(since)
        clauses.append(f"updated_at >= ${len(args)}")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    args.append(limit)
    rows = await db.fetch_all(
        f"SELECT id, external_id, type, status, payload, progress, result, created_at, updated_at, "
        f"       started_at, heartbeat_at, cancel_requested_at, finished_at, error "
        f"FROM jobs {where} "
        f"ORDER BY id DESC LIMIT ${len(args)}",
        *args,
    )
    return [_decode_job(row) for row in rows]


async def update_progress(job_id: int, progress: Mapping[str, Any]) -> None:
    await lifecycle.update_progress(job_id, progress)


async def request_cancel(job_id: int) -> Mapping[str, Any]:
    return await lifecycle.request_cancel(job_id)


def _to_jsonb(payload: Mapping[str, Any]) -> str:
    return json.dumps(dict(payload))


def _decode_job(row: Mapping[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    for key in ("payload", "progress", "result"):
        value = decoded.get(key)
        if isinstance(value, str):
            decoded[key] = json.loads(value)
    return decoded
