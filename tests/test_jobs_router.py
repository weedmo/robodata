import json
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




async def _job_count() -> int:
    row = await db.fetch_one("SELECT COUNT(*) AS count FROM jobs")
    return int(row["count"])


def _patch_allowed_roots(monkeypatch, *roots: str) -> None:
    from backend.core import config as core_config
    from backend import config as compat_config
    from backend.datasets.routers import dataset_ops as dataset_ops_router

    allowed = [str(root) for root in roots]
    monkeypatch.setattr(core_config.settings, "allowed_dataset_roots", allowed)
    monkeypatch.setattr(compat_config.settings, "allowed_dataset_roots", allowed)
    monkeypatch.setattr(dataset_ops_router.settings, "allowed_dataset_roots", allowed)

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
async def test_post_jobs_accepts_bronze_silver_batch():
    async with _client() as ac:
        r = await ac.post(
            "/api/jobs",
            json={
                "type": "bronze_silver_batch",
                "payload": {"data_root": "/mnt/synology/data/data_div/2026"},
            },
        )
    assert r.status_code == 201
    assert r.json()["type"] == "bronze_silver_batch"


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


@pytest.mark.asyncio
async def test_post_jobs_rejects_invalid_dataset_operation_before_persisting():
    async with _client() as ac:
        r = await ac.post(
            "/api/jobs",
            json={"type": "split", "payload": {"source_path": "/tmp/source", "target_name": "out"}},
        )
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_dataset_operation_payload"
    assert r.json()["operation"] == "split"
    assert r.json()["issues"]
    assert await _job_count() == 0


@pytest.mark.asyncio
async def test_post_jobs_rejects_outside_root_dataset_operation_before_persisting(monkeypatch, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _patch_allowed_roots(monkeypatch, str(allowed))

    async with _client() as ac:
        r = await ac.post(
            "/api/jobs",
            json={
                "type": "split",
                "payload": {
                    "source_path": str(outside),
                    "episode_ids": [1],
                    "target_name": "out",
                },
            },
        )
    assert r.status_code == 400
    assert r.json() == {
        "error": "dataset_operation_path_policy",
        "operation": "split",
        "path": str(outside),
    }
    assert await _job_count() == 0


@pytest.mark.asyncio
async def test_post_jobs_rejects_missing_source_dataset_operation_before_persisting(monkeypatch, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    missing = allowed / "missing"
    _patch_allowed_roots(monkeypatch, str(allowed))

    async with _client() as ac:
        r = await ac.post(
            "/api/jobs",
            json={
                "type": "split",
                "payload": {
                    "source_path": str(missing),
                    "episode_ids": [1],
                    "target_name": "out",
                },
            },
        )
    assert r.status_code == 404
    assert r.json() == {
        "error": "dataset_operation_source_missing",
        "operation": "split",
        "path": str(missing.resolve()),
    }
    assert await _job_count() == 0


@pytest.mark.asyncio
async def test_post_jobs_valid_dataset_operation_uses_canonical_payload_and_dedupe(monkeypatch, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _patch_allowed_roots(monkeypatch, str(tmp_path))

    async with _client() as ac:
        r = await ac.post(
            "/api/jobs",
            json={
                "type": "split",
                "payload": {
                    "source_path": str(source),
                    "episode_ids": ["1", 2],
                    "target_name": "out",
                    "output_dir": str(output_dir),
                    "ignored": "not-persisted",
                },
                "dedupe_key": "caller-supplied",
            },
        )
    assert r.status_code == 201
    body = r.json()
    assert body["dedupe_key"] == f"split:{source.resolve()}:out"

    persisted = await db.fetch_one("SELECT payload, dedupe_key FROM jobs WHERE id=$1", body["id"])
    persisted_payload = persisted["payload"]
    if isinstance(persisted_payload, str):
        persisted_payload = json.loads(persisted_payload)
    assert persisted["dedupe_key"] == f"split:{source.resolve()}:out"
    assert persisted_payload == {
        "source_path": str(source.resolve()),
        "episode_ids": [1, 2],
        "target_name": "out",
        "output_dir": str(output_dir.resolve()),
    }
