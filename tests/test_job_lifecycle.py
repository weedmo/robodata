import inspect

import pytest

from backend.core.db import db, init_db
from backend.jobs import lifecycle
from backend.jobs import repo as jobs_repo
from backend.workers import runtime

pytestmark = pytest.mark.usefixtures("_point_settings_at_test_db")


@pytest.fixture(autouse=True)
async def clean_jobs():
    await init_db()
    await db.execute("DELETE FROM worker_heartbeats")
    await db.execute("DELETE FROM jobs")
    yield
    await db.execute("DELETE FROM worker_heartbeats")
    await db.execute("DELETE FROM jobs")


@pytest.mark.asyncio
async def test_claim_next_sets_running_fields_and_decodes_payload():
    enq = await jobs_repo.enqueue(type_="convert", payload={"cell": "a/b"})

    job = await lifecycle.claim_next("converter", ["convert"])

    assert job == {"id": enq["id"], "type": "convert", "payload": {"cell": "a/b"}}
    fetched = await jobs_repo.fetch(enq["id"])
    assert fetched["status"] == "running"
    assert fetched["worker_id"] == "converter"
    assert fetched["started_at"] is not None
    assert fetched["heartbeat_at"] is not None


@pytest.mark.asyncio
async def test_requeue_abandoned_recovers_only_running_jobs_for_worker_and_type():
    abandoned = await jobs_repo.enqueue(type_="convert", payload={})
    other_worker = await jobs_repo.enqueue(type_="convert", payload={})
    other_type = await jobs_repo.enqueue(type_="split", payload={})
    cancel_requested = await jobs_repo.enqueue(type_="convert", payload={})
    await db.execute(
        "UPDATE jobs SET status='running', worker_id='converter', "
        "started_at=NOW(), heartbeat_at=NOW() WHERE id=$1",
        abandoned["id"],
    )
    await db.execute(
        "UPDATE jobs SET status='running', worker_id='curation-worker', "
        "started_at=NOW(), heartbeat_at=NOW() WHERE id=$1",
        other_worker["id"],
    )
    await db.execute(
        "UPDATE jobs SET status='running', worker_id='converter', "
        "started_at=NOW(), heartbeat_at=NOW() WHERE id=$1",
        other_type["id"],
    )
    await db.execute(
        "UPDATE jobs SET status='cancel_requested', worker_id='converter', "
        "started_at=NOW(), heartbeat_at=NOW() WHERE id=$1",
        cancel_requested["id"],
    )

    count = await lifecycle.requeue_abandoned("converter", ["convert"])

    assert count == 1
    recovered = await jobs_repo.fetch(abandoned["id"])
    assert recovered["status"] == "queued"
    assert recovered["worker_id"] is None
    assert recovered["started_at"] is None
    assert recovered["heartbeat_at"] is None
    assert recovered["attempts"] == 1
    assert (await jobs_repo.fetch(other_worker["id"]))["status"] == "running"
    assert (await jobs_repo.fetch(other_type["id"]))["status"] == "running"
    assert (await jobs_repo.fetch(cancel_requested["id"]))["status"] == "cancel_requested"


@pytest.mark.asyncio
async def test_heartbeat_and_progress_update_metadata_without_status_change():
    enq = await jobs_repo.enqueue(type_="convert", payload={})
    await lifecycle.claim_next("converter", ["convert"])

    await lifecycle.update_progress(enq["id"], {"phase": "converting"})
    assert await lifecycle.heartbeat_and_observe_cancel(enq["id"]) is False

    fetched = await jobs_repo.fetch(enq["id"])
    assert fetched["status"] == "running"
    assert fetched["progress"] == {"phase": "converting"}
    assert fetched["heartbeat_at"] is not None


@pytest.mark.asyncio
async def test_request_cancel_marks_active_jobs_and_rejects_terminal_jobs():
    queued = await jobs_repo.enqueue(type_="convert", payload={})
    running = await jobs_repo.enqueue(type_="convert", payload={})
    terminal = await jobs_repo.enqueue(type_="convert", payload={})
    await db.execute("UPDATE jobs SET status='running' WHERE id=$1", running["id"])
    await db.execute("UPDATE jobs SET status='complete' WHERE id=$1", terminal["id"])

    assert (await lifecycle.request_cancel(queued["id"]))["status"] == "cancelled"
    assert (await lifecycle.request_cancel(running["id"]))["status"] == "cancel_requested"
    assert await lifecycle.cancel_requested(running["id"]) is True
    with pytest.raises(lifecycle.AlreadyTerminal):
        await lifecycle.request_cancel(terminal["id"])


@pytest.mark.asyncio
async def test_request_cancel_is_idempotent_after_cancel_was_requested():
    enq = await jobs_repo.enqueue(type_="convert", payload={})
    await db.execute("UPDATE jobs SET status='running' WHERE id=$1", enq["id"])

    first = await lifecycle.request_cancel(enq["id"])
    second = await lifecycle.request_cancel(enq["id"])

    assert first["status"] == "cancel_requested"
    assert second == {"id": enq["id"], "status": "cancel_requested"}


@pytest.mark.asyncio
async def test_cancelling_queued_job_releases_dedupe_key():
    first = await jobs_repo.enqueue(type_="convert", payload={}, dedupe_key="cell:a/b")

    cancelled = await lifecycle.request_cancel(first["id"])
    replacement = await jobs_repo.enqueue(type_="convert", payload={}, dedupe_key="cell:a/b")

    assert cancelled["status"] == "cancelled"
    assert replacement["id"] != first["id"]


@pytest.mark.asyncio
async def test_request_cancel_reports_terminal_status_when_update_loses_race(monkeypatch):
    responses = iter([None, None, {"status": "complete"}])

    async def fake_fetch_one(*args, **kwargs):
        return next(responses)

    monkeypatch.setattr(lifecycle.db, "fetch_one", fake_fetch_one)

    with pytest.raises(lifecycle.AlreadyTerminal) as excinfo:
        await lifecycle.request_cancel(42)

    assert excinfo.value.current_status == "complete"


@pytest.mark.asyncio
async def test_complete_encodes_optional_mapping_result():
    enq = await jobs_repo.enqueue(type_="split", payload={})
    await lifecycle.claim_next("curation-worker", ["split"])

    await lifecycle.complete(enq["id"], {"result_path": "/tmp/out", "count": 2})

    fetched = await jobs_repo.fetch(enq["id"])
    assert fetched["status"] == "complete"
    assert fetched["result"] == {"result_path": "/tmp/out", "count": 2}
    assert fetched["finished_at"] is not None


@pytest.mark.asyncio
async def test_fail_and_cancel_normally_store_terminal_state():
    failed = await jobs_repo.enqueue(type_="convert", payload={})
    cancelled = await jobs_repo.enqueue(type_="convert", payload={})
    await lifecycle.claim_next("converter", ["convert"])
    await lifecycle.claim_next("converter", ["convert"])

    await lifecycle.fail(failed["id"], "boom")
    await lifecycle.cancel_normally(cancelled["id"], "partial output removed")

    failed_job = await jobs_repo.fetch(failed["id"])
    cancelled_job = await jobs_repo.fetch(cancelled["id"])
    assert failed_job["status"] == "failed"
    assert failed_job["error"] == "boom"
    assert cancelled_job["status"] == "cancelled"
    assert cancelled_job["error"] == "partial output removed"


@pytest.mark.asyncio
async def test_late_cancel_wins_over_complete_and_fail():
    completing = await jobs_repo.enqueue(type_="convert", payload={})
    failing = await jobs_repo.enqueue(type_="convert", payload={})
    await lifecycle.claim_next("converter", ["convert"])
    await lifecycle.claim_next("converter", ["convert"])
    await lifecycle.request_cancel(completing["id"])
    await lifecycle.request_cancel(failing["id"])

    await lifecycle.complete(completing["id"], {"result_path": "/tmp/out"})
    await lifecycle.fail(failing["id"], "late failure")

    completed_job = await jobs_repo.fetch(completing["id"])
    failed_job = await jobs_repo.fetch(failing["id"])
    assert completed_job["status"] == "cancelled"
    assert completed_job["result"] is None
    assert failed_job["status"] == "cancelled"
    assert failed_job["error"] is None


@pytest.mark.asyncio
async def test_terminal_jobs_are_not_overwritten_by_late_writers():
    enq = await jobs_repo.enqueue(type_="convert", payload={})
    await lifecycle.claim_next("converter", ["convert"])
    await lifecycle.complete(enq["id"], {"done": True})

    await lifecycle.fail(enq["id"], "too late")
    await lifecycle.cancel_normally(enq["id"], "too late")

    fetched = await jobs_repo.fetch(enq["id"])
    assert fetched["status"] == "complete"
    assert fetched["result"] == {"done": True}
    assert fetched["error"] is None


def test_runtime_contains_no_direct_job_transition_sql():
    source = inspect.getsource(runtime)

    assert "UPDATE jobs SET status" not in source
    assert "UPDATE jobs SET heartbeat_at" not in source
    assert "SELECT id, type, payload FROM jobs" not in source
