from datetime import datetime, timedelta, timezone

import pytest
from backend.workers import repo
from backend.core.db import db, init_db

pytestmark = pytest.mark.usefixtures("_point_settings_at_test_db")


@pytest.fixture(autouse=True)
async def clean_heartbeats():
    await init_db()
    await db.execute("DELETE FROM worker_heartbeats")
    await db.execute(
        "UPDATE worker_controls "
        "SET desired_state='running', note=NULL, updated_by=NULL"
    )
    yield
    await db.execute("DELETE FROM worker_heartbeats")


@pytest.mark.asyncio
async def test_list_workers_includes_seeded_rows():
    rows = await repo.list_workers()
    ids = {r["worker_id"] for r in rows}
    assert {"converter", "curation-worker"} <= ids


@pytest.mark.asyncio
async def test_upsert_heartbeat_and_read():
    await repo.upsert_heartbeat(
        worker_id="converter",
        actual_state="running",
        pid=42,
        container_id="abc",
        in_flight_job_id=None,
        detail={"note": "hi"},
    )
    row = await repo.get_worker("converter")
    assert row["actual_state"] == "running"
    assert row["pid"] == 42
    assert row["detail"] == {"note": "hi"}
    assert row["last_beat_at"] is not None


@pytest.mark.asyncio
async def test_set_desired_state_records_actor():
    await repo.set_desired_state(
        worker_id="converter", desired_state="paused",
        updated_by="tester", note="manual pause",
    )
    row = await repo.get_worker("converter")
    assert row["desired_state"] == "paused"
    assert row["updated_by"] == "tester"
    assert row["note"] == "manual pause"


@pytest.mark.asyncio
async def test_is_stale_when_heartbeat_old():
    old = datetime.now(timezone.utc) - timedelta(seconds=60)
    await db.execute(
        "INSERT INTO worker_heartbeats(worker_id, actual_state, last_beat_at) "
        "VALUES('converter','running',$1) "
        "ON CONFLICT(worker_id) DO UPDATE SET last_beat_at=$1", old,
    )
    assert await repo.is_stale("converter", ttl_seconds=30) is True
    fresh = datetime.now(timezone.utc)
    await db.execute(
        "UPDATE worker_heartbeats SET last_beat_at=$1 WHERE worker_id='converter'", fresh,
    )
    assert await repo.is_stale("converter", ttl_seconds=30) is False
