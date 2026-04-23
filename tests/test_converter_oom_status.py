"""Tests for converter container OOM/exited status exposure."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from backend.converter.service import (
    ContainerStateInfo,
    ConverterStatus,
    TaskProgress,
    get_container_state,
    get_container_state_info,
    get_status,
)


@pytest.mark.asyncio
async def test_get_container_state_info_parses_exit_code_oom_and_finished_at():
    inspect_state = (
        '{"Status": "exited", "ExitCode": 137, "OOMKilled": true, '
        '"FinishedAt": "2026-04-22T10:11:12Z"}'
    )
    with patch("backend.converter.service._run", new_callable=AsyncMock, return_value=(0, inspect_state, "")):
        info = await get_container_state_info()

    assert info == ContainerStateInfo(
        status="exited",
        exit_code=137,
        oom_killed=True,
        finished_at="2026-04-22T10:11:12Z",
    )


@pytest.mark.asyncio
async def test_get_container_state_info_returns_stopped_when_container_is_missing():
    with patch("backend.converter.service._run", new_callable=AsyncMock, return_value=(1, "", "No such container")):
        info = await get_container_state_info()

    assert info.status == "stopped"
    assert info.exit_code is None
    assert info.oom_killed is False
    assert info.finished_at is None


@pytest.mark.asyncio
async def test_get_container_state_still_returns_raw_status_string():
    with patch(
        "backend.converter.service.get_container_state_info",
        new_callable=AsyncMock,
        return_value=ContainerStateInfo(status="running"),
    ):
        state = await get_container_state()

    assert state == "running"


@pytest.mark.asyncio
async def test_get_container_state_info_ignores_zeroed_running_exit_fields():
    inspect_state = (
        '{"Status": "running", "ExitCode": 0, "OOMKilled": false, '
        '"FinishedAt": "0001-01-01T00:00:00Z"}'
    )
    with patch("backend.converter.service._run", new_callable=AsyncMock, return_value=(0, inspect_state, "")):
        info = await get_container_state_info()

    assert info.status == "running"
    assert info.exit_code is None
    assert info.finished_at is None
    assert info.oom_killed is False


@pytest.mark.asyncio
async def test_get_status_normalizes_exited_to_stopped_without_dropping_fields():
    fake_tasks = [TaskProgress("cell001/task_a", 10, 8, 1, 1, 0)]
    with patch("backend.converter.service.check_docker", new_callable=AsyncMock, return_value=True), patch(
        "backend.converter.service.get_container_state_info",
        new_callable=AsyncMock,
        return_value=ContainerStateInfo(
            status="exited",
            exit_code=137,
            oom_killed=True,
            finished_at="2026-04-22T10:11:12Z",
        ),
    ), patch(
        "backend.converter.service.build_progress",
        return_value=(fake_tasks, "1 task | 10 recordings | 8 done | 1 pending | 1 failed"),
    ):
        status = await get_status()

    assert status == ConverterStatus(
        container_state="stopped",
        docker_available=True,
        tasks=fake_tasks,
        summary="1 task | 10 recordings | 8 done | 1 pending | 1 failed",
        exit_code=137,
        oom_killed=True,
        finished_at="2026-04-22T10:11:12Z",
    )


@pytest.mark.asyncio
async def test_get_status_normalizes_dead_to_stopped_without_dropping_fields():
    fake_tasks = [TaskProgress("cell001/task_a", 10, 8, 1, 1, 0)]
    with patch("backend.converter.service.check_docker", new_callable=AsyncMock, return_value=True), patch(
        "backend.converter.service.get_container_state_info",
        new_callable=AsyncMock,
        return_value=ContainerStateInfo(
            status="dead",
            exit_code=137,
            oom_killed=True,
            finished_at="2026-04-22T10:11:12Z",
        ),
    ), patch(
        "backend.converter.service.build_progress",
        return_value=(fake_tasks, "1 task | 10 recordings | 8 done | 1 pending | 1 failed"),
    ):
        status = await get_status()

    assert status == ConverterStatus(
        container_state="stopped",
        docker_available=True,
        tasks=fake_tasks,
        summary="1 task | 10 recordings | 8 done | 1 pending | 1 failed",
        exit_code=137,
        oom_killed=True,
        finished_at="2026-04-22T10:11:12Z",
    )


@pytest.mark.asyncio
async def test_status_route_includes_oom_fields():
    from backend.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        with patch("backend.converter.router.converter_service.get_status", new_callable=AsyncMock) as get_status:
            get_status.return_value = ConverterStatus(
                container_state="stopped",
                docker_available=True,
                tasks=[TaskProgress("cell001/task_a", 5, 5, 0, 0, 0)],
                summary="1 task | 5 recordings | 5 done | 0 pending | 0 failed",
                exit_code=137,
                oom_killed=True,
                finished_at="2026-04-22T10:11:12Z",
            )

            resp = await ac.get("/api/converter/status")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["exit_code"] == 137
    assert payload["oom_killed"] is True
    assert payload["finished_at"] == "2026-04-22T10:11:12Z"
