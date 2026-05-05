from datetime import datetime, timedelta, timezone

import pytest
from backend.jobs import repo
from backend.core.db import db, init_db

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
async def test_enqueue_returns_id_and_default_status():
    row = await repo.enqueue(type_="convert", payload={"cell": "a/b"})
    assert row["id"] > 0
    assert row["external_id"]
    assert row["status"] == "queued"
    fetched = await repo.fetch(row["id"])
    assert fetched["external_id"] == row["external_id"]
    assert fetched["payload"] == {"cell": "a/b"}
    by_external_id = await repo.fetch_by_external_id(row["external_id"])
    assert by_external_id["id"] == row["id"]


@pytest.mark.asyncio
async def test_enqueue_rejects_duplicate_dedupe_key():
    await repo.enqueue(type_="convert", payload={}, dedupe_key="cell:a/b")
    with pytest.raises(repo.DuplicateDedupe) as ei:
        await repo.enqueue(type_="convert", payload={}, dedupe_key="cell:a/b")
    assert ei.value.existing_job_id > 0


@pytest.mark.asyncio
async def test_request_cancel_marks_running_job():
    enq = await repo.enqueue(type_="convert", payload={})
    await db.execute(
        "UPDATE jobs SET status='running', worker_id='converter' WHERE id=$1",
        enq["id"],
    )
    cancelled = await repo.request_cancel(enq["id"])
    assert cancelled["status"] == "cancel_requested"


@pytest.mark.asyncio
async def test_request_cancel_rejects_terminal_job():
    enq = await repo.enqueue(type_="convert", payload={})
    await db.execute("UPDATE jobs SET status='complete' WHERE id=$1", enq["id"])
    with pytest.raises(repo.AlreadyTerminal):
        await repo.request_cancel(enq["id"])


@pytest.mark.asyncio
async def test_list_jobs_filters_by_status_dataset_and_since():
    old_since = datetime.now(timezone.utc) - timedelta(seconds=1)
    keep = await repo.enqueue(type_="convert", payload={"dataset_id": 7})
    await repo.enqueue(type_="convert", payload={"dataset_id": 8})
    await db.execute("UPDATE jobs SET status='running' WHERE id=$1", keep["id"])

    rows = await repo.list_jobs(status="running", dataset_id=7, since=old_since)
    assert [r["id"] for r in rows] == [keep["id"]]


@pytest.mark.asyncio
async def test_external_id_is_unique():
    first = await repo.enqueue(type_="convert", payload={})
    second = await repo.enqueue(type_="convert", payload={})
    assert first["external_id"] != second["external_id"]
