"""Long-running worker base loop — claim job, heartbeat, observe cancel."""
from __future__ import annotations
import asyncio, inspect, json, logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping
from backend.core.db import db
from backend.workers import repo as workers_repo

log = logging.getLogger("workers.runtime")


@dataclass
class CancelledNormally:
    cleanup: str = ""


JobResult = None | CancelledNormally | Mapping[str, Any]
JobHandler = Callable[..., Awaitable[JobResult]]


async def tick(
    *, worker_id: str, handlers: Mapping[str, JobHandler], idle_sleep: float = 1.0,
) -> None:
    """Single iteration: heartbeat, check desired_state, claim one job, dispatch by type."""
    if not handlers:
        await asyncio.sleep(idle_sleep)
        return
    await workers_repo.upsert_heartbeat(
        worker_id=worker_id, actual_state="running", pid=None,
        container_id=None, in_flight_job_id=None, detail=None,
    )
    desired = await db.fetch_one(
        "SELECT desired_state FROM worker_controls WHERE worker_id=$1", worker_id,
    )
    if desired is None or desired["desired_state"] != "running":
        await asyncio.sleep(idle_sleep)
        return
    job = await _claim(worker_id, list(handlers.keys()))
    if job is None:
        await asyncio.sleep(idle_sleep)
        return
    await workers_repo.upsert_heartbeat(
        worker_id=worker_id, actual_state="running", pid=None,
        container_id=None, in_flight_job_id=job["id"], detail=None,
    )

    async def check_cancel() -> bool:
        await db.execute(
            "UPDATE jobs SET heartbeat_at=NOW(), updated_at=NOW() WHERE id=$1",
            job["id"],
        )
        row = await db.fetch_one("SELECT status FROM jobs WHERE id=$1", job["id"])
        return bool(row and row["status"] == "cancel_requested")

    handler = handlers[job["type"]]
    try:
        sig = inspect.signature(handler)
        if "check_cancel" in sig.parameters:
            result = await handler(job, check_cancel=check_cancel)
        else:
            result = await handler(job)
    except Exception as exc:
        await db.execute(
            "UPDATE jobs SET status='failed', finished_at=NOW(), updated_at=NOW(), "
            "error=$2 WHERE id=$1", job["id"], str(exc),
        )
        log.exception("job %s failed", job["id"])
    else:
        if isinstance(result, CancelledNormally):
            await db.execute(
                "UPDATE jobs SET status='cancelled', finished_at=NOW(), updated_at=NOW(), "
                "error=$2 WHERE id=$1", job["id"], result.cleanup or "",
            )
        else:
            if isinstance(result, Mapping):
                await db.execute(
                    "UPDATE jobs SET status='complete', finished_at=NOW(), updated_at=NOW(), "
                    "result=$2::jsonb WHERE id=$1", job["id"], json.dumps(dict(result)),
                )
            else:
                await db.execute(
                    "UPDATE jobs SET status='complete', finished_at=NOW(), updated_at=NOW() "
                    "WHERE id=$1", job["id"],
                )
    finally:
        await workers_repo.upsert_heartbeat(
            worker_id=worker_id, actual_state="running", pid=None,
            container_id=None, in_flight_job_id=None, detail=None,
        )


async def _claim(worker_id: str, types: list[str]) -> dict | None:
    async with db.transaction():
        # Wrap `types` in an outer list so the DB facade's _pack_params heuristic
        # (which unpacks a single list/tuple positional arg) leaves the inner
        # list intact for asyncpg to bind as the $1 text[] array parameter.
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
            "heartbeat_at=NOW(), updated_at=NOW() WHERE id=$1", row["id"], worker_id,
        )
        return _decode_claimed_job(row)


def _decode_claimed_job(row: Mapping[str, Any]) -> dict[str, Any]:
    job = dict(row)
    if isinstance(job.get("payload"), str):
        job["payload"] = json.loads(job["payload"])
    return job


async def run_forever(
    *, worker_id: str, handlers: Mapping[str, JobHandler], idle_sleep: float = 1.0,
) -> None:  # pragma: no cover — ops loop
    while True:
        try:
            await tick(
                worker_id=worker_id, handlers=handlers, idle_sleep=idle_sleep,
            )
        except Exception:
            log.exception("worker %s tick crashed", worker_id)
            await asyncio.sleep(idle_sleep)
