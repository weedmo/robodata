import pytest
from datetime import datetime, timedelta, timezone
from httpx import ASGITransport, AsyncClient
from backend.main import app
from backend.core.db import db, init_db

pytestmark = pytest.mark.usefixtures("_point_settings_at_test_db")


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
async def reset():
    await init_db()
    await db.execute("DELETE FROM worker_heartbeats")
    await db.execute(
        "UPDATE worker_controls "
        "SET desired_state='running', note=NULL, updated_by=NULL"
    )
    yield
    await db.execute("DELETE FROM worker_heartbeats")
    await db.execute(
        "UPDATE worker_controls "
        "SET desired_state='running', note=NULL, updated_by=NULL"
    )


async def _seed_fresh_heartbeat(worker_id: str, state: str = "running") -> None:
    await db.execute(
        "INSERT INTO worker_heartbeats(worker_id, actual_state) VALUES($1,$2) "
        "ON CONFLICT(worker_id) DO UPDATE SET actual_state=$2, last_beat_at=NOW()",
        worker_id, state,
    )


@pytest.mark.asyncio
async def test_get_workers_lists_two_seeded():
    async with _client() as ac:
        r = await ac.get("/api/workers")
    assert r.status_code == 200
    ids = {w["worker_id"] for w in r.json()}
    assert {"converter", "curation-worker"} <= ids


@pytest.mark.asyncio
async def test_patch_worker_running_to_paused():
    await _seed_fresh_heartbeat("converter")
    async with _client() as ac:
        r = await ac.patch(
            "/api/workers/converter",
            json={"desired_state": "paused", "note": "smoke"},
            headers={"X-User-Name": "tester"},
        )
    assert r.status_code == 200
    assert r.json()["desired_state"] == "paused"


@pytest.mark.asyncio
async def test_patch_worker_rejects_stale():
    old = datetime.now(timezone.utc) - timedelta(seconds=60)
    await db.execute(
        "INSERT INTO worker_heartbeats(worker_id, actual_state, last_beat_at) "
        "VALUES('converter','running',$1) "
        "ON CONFLICT(worker_id) DO UPDATE SET last_beat_at=$1", old,
    )
    async with _client() as ac:
        r = await ac.patch("/api/workers/converter", json={"desired_state": "paused"})
    assert r.status_code == 422
    assert r.json()["error"] == "worker_stale"


@pytest.mark.asyncio
async def test_patch_worker_illegal_transition_stopped_to_running_requires_note():
    await _seed_fresh_heartbeat("converter")
    await db.execute("UPDATE worker_controls SET desired_state='stopped' WHERE worker_id='converter'")
    async with _client() as ac:
        r = await ac.patch("/api/workers/converter", json={"desired_state": "running"})
    assert r.status_code == 422
    assert r.json()["error"] == "illegal_transition"
