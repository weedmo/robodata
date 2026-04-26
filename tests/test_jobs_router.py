from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app
from backend.core.db import db, init_db

pytestmark = pytest.mark.usefixtures("_point_settings_at_test_db")


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
async def clean():
    await init_db()
    await db.execute("DELETE FROM worker_heartbeats")
    await db.execute("DELETE FROM jobs")
    yield
    await db.execute("DELETE FROM worker_heartbeats")
    await db.execute("DELETE FROM jobs")


@pytest.mark.asyncio
async def test_post_jobs_creates_queued():
    async with _client() as ac:
        r = await ac.post(
            "/api/jobs",
            json={"type": "convert", "payload": {"cell": "a/b"}},
            headers={"X-User-Name": "tester"},
        )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "queued"
    assert body["type"] == "convert"
    assert body["id"] > 0


@pytest.mark.asyncio
async def test_post_jobs_409_on_duplicate_dedupe():
    async with _client() as ac:
        first = await ac.post(
            "/api/jobs",
            json={"type": "convert", "payload": {}, "dedupe_key": "k1"},
        )
        assert first.status_code == 201
        dup = await ac.post(
            "/api/jobs",
            json={"type": "convert", "payload": {}, "dedupe_key": "k1"},
        )
    assert dup.status_code == 409
    assert dup.json()["error"] == "duplicate_dedupe_key"
    assert dup.json()["existing_job_id"] == first.json()["id"]


@pytest.mark.asyncio
async def test_get_job_by_id():
    async with _client() as ac:
        created = (await ac.post("/api/jobs", json={"type": "convert", "payload": {}})).json()
        r = await ac.get(f"/api/jobs/{created['id']}")
    assert r.status_code == 200
    assert r.json()["id"] == created["id"]


@pytest.mark.asyncio
async def test_get_jobs_filters_running_since():
    since = datetime.now(timezone.utc)
    async with _client() as ac:
        created = (await ac.post(
            "/api/jobs",
            json={"type": "convert", "payload": {"dataset_id": 7}},
        )).json()
        await db.execute("UPDATE jobs SET status='running' WHERE id=$1", created["id"])
        r = await ac.get("/api/jobs", params={"status": "running", "dataset_id": 7, "since": since.isoformat()})
    assert r.status_code == 200
    assert [job["id"] for job in r.json()] == [created["id"]]


@pytest.mark.asyncio
async def test_post_cancel_running_job():
    async with _client() as ac:
        created = (await ac.post("/api/jobs", json={"type": "convert", "payload": {}})).json()
        await db.execute("UPDATE jobs SET status='running' WHERE id=$1", created["id"])
        r = await ac.post(f"/api/jobs/{created['id']}/cancel")
    assert r.status_code == 202
    assert r.json()["status"] == "cancel_requested"


@pytest.mark.asyncio
async def test_post_cancel_409_on_terminal():
    async with _client() as ac:
        created = (await ac.post("/api/jobs", json={"type": "convert", "payload": {}})).json()
        await db.execute("UPDATE jobs SET status='complete' WHERE id=$1", created["id"])
        r = await ac.post(f"/api/jobs/{created['id']}/cancel")
    assert r.status_code == 409
    assert r.json()["error"] == "already_terminal"
