import pytest
from backend.workers import runtime
from backend.workers import repo as workers_repo
from backend.jobs import repo as jobs_repo
from backend.core.db import db, init_db

pytestmark = pytest.mark.usefixtures("_point_settings_at_test_db")


@pytest.fixture(autouse=True)
async def clean():
    await init_db()
    await db.execute("DELETE FROM worker_heartbeats")
    await db.execute("DELETE FROM jobs")
    await db.execute("UPDATE worker_controls SET desired_state='running', note=NULL")
    yield
    await db.execute("DELETE FROM worker_heartbeats")
    await db.execute("DELETE FROM jobs")


@pytest.mark.asyncio
async def test_runtime_skips_when_paused():
    enq = await jobs_repo.enqueue(type_="convert", payload={})
    await db.execute("UPDATE worker_controls SET desired_state='paused' WHERE worker_id='converter'")
    handler_calls = []
    async def handler(job):
        handler_calls.append(job["id"])
    await runtime.tick(
        worker_id="converter", supported_types=["convert"], handler=handler,
        idle_sleep=0,
    )
    assert handler_calls == []
    job = await jobs_repo.fetch(enq["id"])
    assert job["status"] == "queued"


@pytest.mark.asyncio
async def test_runtime_runs_handler_and_marks_complete():
    enq = await jobs_repo.enqueue(type_="convert", payload={"x": 1})
    async def handler(job):
        assert job["payload"]["x"] == 1
    await runtime.tick(
        worker_id="converter", supported_types=["convert"], handler=handler,
        idle_sleep=0,
    )
    job = await jobs_repo.fetch(enq["id"])
    assert job["status"] == "complete"
    worker = await workers_repo.get_worker("converter")
    assert worker["in_flight_job_id"] is None


@pytest.mark.asyncio
async def test_runtime_marks_failed_on_handler_exception():
    enq = await jobs_repo.enqueue(type_="convert", payload={})
    async def handler(job):
        raise RuntimeError("boom")
    await runtime.tick(
        worker_id="converter", supported_types=["convert"], handler=handler,
        idle_sleep=0,
    )
    job = await jobs_repo.fetch(enq["id"])
    assert job["status"] == "failed"
    assert "boom" in job["error"]
    worker = await workers_repo.get_worker("converter")
    assert worker["in_flight_job_id"] is None


@pytest.mark.asyncio
async def test_runtime_handler_can_check_cancel_via_callback():
    enq = await jobs_repo.enqueue(type_="convert", payload={})
    chunks_seen = []
    async def handler(job, *, check_cancel):
        chunks_seen.append("a")
        await jobs_repo.request_cancel(job["id"])
        if await check_cancel():
            return runtime.CancelledNormally(cleanup="partial removed")
        chunks_seen.append("b")
    await runtime.tick(
        worker_id="converter", supported_types=["convert"], handler=handler,
        idle_sleep=0,
    )
    job = await jobs_repo.fetch(enq["id"])
    assert chunks_seen == ["a"]
    assert job["status"] == "cancelled"
    assert "partial removed" in (job["error"] or "")
    assert job["heartbeat_at"] is not None
    worker = await workers_repo.get_worker("converter")
    assert worker["in_flight_job_id"] is None
