"""Tests for converter validation API endpoints and status merging."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).parent.parent))

_frontend_assets = Path(__file__).parent.parent / "frontend" / "dist" / "assets"
_frontend_assets.mkdir(parents=True, exist_ok=True)

from backend.main import app


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_post_quick_validation_returns_result(client):
    with patch(
        "backend.converter.validation_service.ensure_not_running"
    ) as ensure, patch(
        "backend.converter.validation_service.run_quick_validation_sync",
        return_value={
            "status": "passed",
            "summary": "Quick passed: 5 episodes, 0 warnings",
            "checked_at": "2026-04-18T11:00:00+09:00",
        },
    ):
        resp = await client.post("/api/converter/validate/quick", json={"cell_task": "cell001/task_a"})

    assert resp.status_code == 200
    assert resp.json()["status"] == "passed"
    ensure.assert_called_once_with("cell001/task_a", "quick")


@pytest.mark.asyncio
async def test_post_full_validation_returns_409_when_already_running(client):
    from backend.converter.validation_service import ValidationAlreadyRunningError

    with patch(
        "backend.converter.validation_service.ensure_not_running",
        side_effect=ValidationAlreadyRunningError("busy"),
    ):
        resp = await client.post("/api/converter/validate/full", json={"cell_task": "cell001/task_a"})

    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_post_start_enqueues_convert_job(client):
    fake_row = {
        "id": 42,
        "type": "convert",
        "status": "queued",
        "dedupe_key": "cell001/task_a",
    }
    with patch(
        "backend.converter.router.jobs_repo.enqueue",
        new_callable=AsyncMock,
        return_value=fake_row,
    ) as enqueue:
        resp = await client.post(
            "/api/converter/start",
            json={"cell_task": "cell001/task_a"},
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == 42
    assert body["status"] == "queued"
    enqueue.assert_called_once_with(
        type_="convert",
        payload={"cell_task": "cell001/task_a"},
        dedupe_key="cell001/task_a",
    )


@pytest.mark.asyncio
async def test_post_start_returns_409_on_duplicate(client):
    from backend.jobs.repo import DuplicateDedupe

    with patch(
        "backend.converter.router.jobs_repo.enqueue",
        new_callable=AsyncMock,
        side_effect=DuplicateDedupe(existing_job_id=7),
    ):
        resp = await client.post(
            "/api/converter/start",
            json={"cell_task": "cell001/task_a"},
        )

    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "duplicate_dedupe_key"
    assert body["existing_job_id"] == 7


@pytest.mark.asyncio
async def test_post_stop_cancels_running_convert_job(client):
    with patch(
        "backend.converter.router.jobs_repo.list_jobs",
        new_callable=AsyncMock,
        return_value=[{"id": 11, "type": "convert", "status": "running"}],
    ) as list_jobs, patch(
        "backend.converter.router.jobs_repo.request_cancel",
        new_callable=AsyncMock,
        return_value={"id": 11, "status": "cancel_requested"},
    ) as cancel:
        resp = await client.post("/api/converter/stop")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "cancel_requested"
    assert body["job_id"] == 11
    list_jobs.assert_called_once_with(type_="convert", status="running", limit=1)
    cancel.assert_called_once_with(11)


@pytest.mark.asyncio
async def test_post_stop_returns_404_when_no_running_job(client):
    with patch(
        "backend.converter.router.jobs_repo.list_jobs",
        new_callable=AsyncMock,
        return_value=[],
    ):
        resp = await client.post("/api/converter/stop")

    assert resp.status_code == 404
    assert resp.json()["error"] == "no_running_convert_job"


@pytest.mark.asyncio
async def test_status_includes_validation_block(client):
    with patch("backend.converter.router.converter_service.get_status", new_callable=AsyncMock) as get_status, patch(
        "backend.converter.validation_service.read_validation_state",
        return_value={
            "cell001/task_a": {
                "quick": {
                    "status": "passed",
                    "summary": "Quick passed: 5 episodes, 0 warnings",
                    "checked_at": "2026-04-18T11:00:00+09:00",
                },
                "full": {
                    "status": "partial",
                    "summary": "Full partial: dataset OK, official loader skipped",
                    "checked_at": "2026-04-18T11:03:00+09:00",
                },
            }
        },
    ):
        from backend.converter.service import ConverterStatus, TaskProgress

        get_status.return_value = ConverterStatus(
            container_state="stopped",
            docker_available=True,
            tasks=[TaskProgress("cell001/task_a", 5, 5, 0, 0, 0)],
            summary="1 task | 5 recordings | 5 done | 0 pending | 0 failed",
        )

        resp = await client.get("/api/converter/status")

    assert resp.status_code == 200
    task = resp.json()["tasks"][0]
    assert task["validation"]["quick"]["status"] == "passed"
    assert task["validation"]["full"]["status"] == "partial"
