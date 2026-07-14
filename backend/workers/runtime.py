"""Long-running worker base loop — claim job, heartbeat, observe cancel."""
from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Awaitable, Callable, Mapping
from backend.core.db import db
from backend.jobs import lifecycle
from backend.workers import repo as workers_repo

log = logging.getLogger("workers.runtime")


CancelledNormally = lifecycle.CancelledNormally


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
    job = await lifecycle.claim_next(worker_id, list(handlers.keys()))
    if job is None:
        await asyncio.sleep(idle_sleep)
        return
    await workers_repo.upsert_heartbeat(
        worker_id=worker_id, actual_state="running", pid=None,
        container_id=None, in_flight_job_id=job["id"], detail=None,
    )

    async def check_cancel() -> bool:
        return await lifecycle.heartbeat_and_observe_cancel(job["id"])

    handler = handlers[job["type"]]
    try:
        sig = inspect.signature(handler)
        if "check_cancel" in sig.parameters:
            result = await handler(job, check_cancel=check_cancel)
        else:
            result = await handler(job)
    except Exception as exc:
        await lifecycle.fail(job["id"], str(exc))
        log.exception("job %s failed", job["id"])
    else:
        if isinstance(result, CancelledNormally):
            await lifecycle.cancel_normally(job["id"], result.cleanup or "")
        else:
            if isinstance(result, Mapping):
                await lifecycle.complete(job["id"], result)
            else:
                await lifecycle.complete(job["id"])
    finally:
        await workers_repo.upsert_heartbeat(
            worker_id=worker_id, actual_state="running", pid=None,
            container_id=None, in_flight_job_id=None, detail=None,
        )


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
