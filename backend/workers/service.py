"""Worker control state-machine."""
from __future__ import annotations
from backend.workers import repo

STALE_TTL_SECONDS = 30
_VALID_STATES = {"running", "paused", "draining", "stopped"}


class WorkerNotFound(Exception): ...
class WorkerStale(Exception): ...
class IllegalTransition(Exception):
    def __init__(self, frm: str, to: str) -> None:
        super().__init__(f"{frm} → {to}")
        self.frm, self.to = frm, to


def _allowed(frm: str, to: str, *, has_note: bool) -> bool:
    if to not in _VALID_STATES: return False
    if frm == to: return True
    if frm == "running"  and to in {"paused", "draining", "stopped"}: return to != "stopped" or has_note
    if frm == "paused"   and to in {"running", "draining", "stopped"}: return to != "stopped" or has_note
    if frm == "draining" and to in {"running", "stopped"}:             return to != "stopped" or has_note
    if frm == "stopped"  and to == "running":                          return has_note
    return False


async def patch_desired_state(
    *, worker_id: str, desired_state: str,
    note: str | None, updated_by: str | None,
) -> dict:
    current = await repo.get_worker(worker_id)
    if current is None:
        raise WorkerNotFound(worker_id)
    if await repo.is_stale(worker_id, ttl_seconds=STALE_TTL_SECONDS):
        raise WorkerStale(worker_id)
    frm = current["desired_state"]
    if not _allowed(frm, desired_state, has_note=bool(note)):
        raise IllegalTransition(frm, desired_state)
    # draining → running only when no in-flight job
    if frm == "draining" and desired_state == "running" and current["in_flight_job_id"]:
        raise IllegalTransition(frm, desired_state)
    await repo.set_desired_state(
        worker_id=worker_id, desired_state=desired_state,
        updated_by=updated_by, note=note,
    )
    fresh = await repo.get_worker(worker_id)
    return dict(fresh) if fresh else {}
