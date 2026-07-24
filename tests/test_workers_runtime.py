import asyncio

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
        worker_id="converter", handlers={"convert": handler},
        idle_sleep=0,
    )
    assert handler_calls == []
    job = await jobs_repo.fetch(enq["id"])
    assert job["status"] == "queued"


@pytest.mark.asyncio
async def test_runtime_does_not_claim_conversion_when_mutations_disabled(
    monkeypatch
):
    from backend.core import config

    monkeypatch.setattr(config.settings, "conversion_mutations_enabled", False)
    enq = await jobs_repo.enqueue(type_="convert", payload={})
    handler_calls = []

    async def handler(job):
        handler_calls.append(job["id"])

    await runtime.tick(
        worker_id="converter",
        handlers={"convert": handler},
        idle_sleep=0,
    )

    assert handler_calls == []
    assert (await jobs_repo.fetch(enq["id"]))["status"] == "queued"
    worker = await workers_repo.get_worker("converter")
    assert worker["actual_state"] == "paused"
    assert worker["detail"] == {
        "reason": "conversion mutations disabled by operator configuration"
    }


@pytest.mark.asyncio
async def test_runtime_still_runs_curation_when_conversion_mutations_disabled(
    monkeypatch
):
    from backend.core import config

    monkeypatch.setattr(config.settings, "conversion_mutations_enabled", False)
    enq = await jobs_repo.enqueue(type_="split", payload={})
    handler_calls = []

    async def handler(job):
        handler_calls.append(job["id"])

    await runtime.tick(
        worker_id="curation-worker",
        handlers={"split": handler},
        idle_sleep=0,
    )

    assert handler_calls == [enq["id"]]
    assert (await jobs_repo.fetch(enq["id"]))["status"] == "complete"


@pytest.mark.asyncio
async def test_run_forever_does_not_requeue_conversion_when_mutations_disabled(
    monkeypatch,
):
    from backend.core import config

    monkeypatch.setattr(config.settings, "conversion_mutations_enabled", False)
    requeue_calls = []

    async def requeue_abandoned(worker_id, types):
        requeue_calls.append((worker_id, types))
        return 0

    async def stop_after_startup(**kwargs):
        raise asyncio.CancelledError

    async def handler(job):
        return None

    monkeypatch.setattr(runtime.lifecycle, "requeue_abandoned", requeue_abandoned)
    monkeypatch.setattr(runtime, "tick", stop_after_startup)

    with pytest.raises(asyncio.CancelledError):
        await runtime.run_forever(
            worker_id="converter",
            handlers={"convert": handler},
            idle_sleep=0,
        )

    assert requeue_calls == []


@pytest.mark.asyncio
async def test_run_forever_still_requeues_non_conversion_when_mutations_disabled(
    monkeypatch,
):
    from backend.core import config

    monkeypatch.setattr(config.settings, "conversion_mutations_enabled", False)
    requeue_calls = []

    async def requeue_abandoned(worker_id, types):
        requeue_calls.append((worker_id, types))
        return 0

    async def stop_after_startup(**kwargs):
        raise asyncio.CancelledError

    async def handler(job):
        return None

    monkeypatch.setattr(runtime.lifecycle, "requeue_abandoned", requeue_abandoned)
    monkeypatch.setattr(runtime, "tick", stop_after_startup)

    with pytest.raises(asyncio.CancelledError):
        await runtime.run_forever(
            worker_id="curation-worker",
            handlers={"split": handler},
            idle_sleep=0,
        )

    assert requeue_calls == [("curation-worker", ["split"])]


@pytest.mark.asyncio
async def test_runtime_does_not_invoke_handler_for_cancelled_queued_job():
    enq = await jobs_repo.enqueue(type_="convert", payload={"cell": "a/b"})
    cancelled = await jobs_repo.request_cancel(enq["id"])
    assert cancelled["status"] == "cancelled"

    handler_calls = []

    async def handler(job):
        handler_calls.append(job["id"])

    await runtime.tick(
        worker_id="converter",
        handlers={"convert": handler},
        idle_sleep=0,
    )

    assert handler_calls == []
    job = await jobs_repo.fetch(enq["id"])
    assert job["status"] == "cancelled"
    assert job["finished_at"] is not None


@pytest.mark.asyncio
async def test_runtime_runs_handler_and_marks_complete():
    enq = await jobs_repo.enqueue(type_="convert", payload={"x": 1})
    async def handler(job):
        assert job["payload"]["x"] == 1
    await runtime.tick(
        worker_id="converter", handlers={"convert": handler},
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
        worker_id="converter", handlers={"convert": handler},
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
        worker_id="converter", handlers={"convert": handler},
        idle_sleep=0,
    )
    job = await jobs_repo.fetch(enq["id"])
    assert chunks_seen == ["a"]
    assert job["status"] == "cancelled"
    assert "partial removed" in (job["error"] or "")
    assert job["heartbeat_at"] is not None
    worker = await workers_repo.get_worker("converter")
    assert worker["in_flight_job_id"] is None


@pytest.mark.asyncio
async def test_runtime_dispatches_mapping_by_job_type_and_stores_result():
    split = await jobs_repo.enqueue(type_="split", payload={"x": "split"})
    merge = await jobs_repo.enqueue(type_="merge", payload={"x": "merge"})
    calls = []

    async def split_handler(job):
        calls.append(("split", job["payload"]["x"]))
        return {"result_path": "/tmp/split"}

    async def merge_handler(job):
        calls.append(("merge", job["payload"]["x"]))
        return {"result_path": "/tmp/merge"}

    await runtime.tick(
        worker_id="curation-worker",
        handlers={"split": split_handler, "merge": merge_handler},
        idle_sleep=0,
    )
    await runtime.tick(
        worker_id="curation-worker",
        handlers={"split": split_handler, "merge": merge_handler},
        idle_sleep=0,
    )

    assert calls == [("split", "split"), ("merge", "merge")]
    split_job = await jobs_repo.fetch(split["id"])
    merge_job = await jobs_repo.fetch(merge["id"])
    assert split_job["result"] == {"result_path": "/tmp/split"}
    assert merge_job["result"] == {"result_path": "/tmp/merge"}
