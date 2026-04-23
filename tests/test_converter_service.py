"""Tests for converter progress and container status logic."""

from unittest.mock import AsyncMock, call, patch

import pytest

import backend.converter.service as svc

from backend.converter.service import (
    ContainerStateInfo,
    TaskProgress,
    build_progress,
    get_status,
    start_converter,
)


class TestBuildProgress:
    def test_uses_actual_output_when_state_overreports(self):
        with patch(
            "backend.converter.service.scan_raw_totals",
            return_value={"cell005/Amore_dualpick/spray_clean": 216},
        ), patch(
            "backend.converter.service.read_state",
            return_value={
                "cell005/Amore_dualpick/spray_clean": {
                    "converted_count": 216,
                    "failed_serials": [],
                    "transient_failed": {"retry-me": {}},
                },
            },
        ), patch(
            "backend.converter.service._count_output_episodes",
            return_value=170,
        ):
            tasks, summary = build_progress()

        assert tasks == [
            TaskProgress(
                "cell005/Amore_dualpick/spray_clean",
                216,
                170,
                46,
                0,
                1,
            ),
        ]
        assert summary == "1 tasks | 216 recordings | 170 done | 46 pending | 0 failed"

    def test_falls_back_to_state_when_output_is_unavailable(self):
        with patch(
            "backend.converter.service.scan_raw_totals",
            return_value={"cell001/task_a": 10},
        ), patch(
            "backend.converter.service.read_state",
            return_value={
                "cell001/task_a": {
                    "converted_count": 4,
                    "failed_serials": ["s1"],
                    "transient_failed": {},
                },
            },
        ), patch(
            "backend.converter.service._count_output_episodes",
            return_value=None,
        ):
            tasks, summary = build_progress()

        assert tasks == [TaskProgress("cell001/task_a", 10, 4, 5, 1, 0)]
        assert summary == "1 tasks | 10 recordings | 4 done | 5 pending | 1 failed"


class TestGetStatus:
    @pytest.fixture(autouse=True)
    def _reset_progress_cache(self):
        import backend.converter.service as svc

        svc._progress_cache = None
        yield
        svc._progress_cache = None

    @pytest.mark.asyncio
    async def test_docker_unavailable_still_reports_progress(self):
        fake_tasks = [TaskProgress("cell001/task_a", 10, 4, 6, 0, 0)]
        with patch(
            "backend.converter.service.check_docker",
            new_callable=AsyncMock,
            return_value=False,
        ), patch(
            "backend.converter.service.build_progress",
            return_value=(fake_tasks, "1 tasks | 10 recordings | 4 done | 6 pending | 0 failed"),
        ):
            status = await get_status()

        assert status.docker_available is False
        assert status.container_state == "unknown"
        assert status.tasks == fake_tasks
        assert "Docker is not available" not in status.summary
        assert "4 done" in status.summary

    @pytest.mark.asyncio
    async def test_stopped_container_uses_progress_snapshot(self):
        fake_tasks = [TaskProgress("a/b", 10, 5, 3, 2, 0)]
        with patch("backend.converter.service.check_docker", new_callable=AsyncMock, return_value=True), patch(
            "backend.converter.service.get_container_state_info",
            new_callable=AsyncMock,
            return_value=ContainerStateInfo(status="stopped"),
        ), patch(
            "backend.converter.service.build_progress",
            return_value=(fake_tasks, "Total: 1 task"),
        ):
            status = await get_status()

        assert status.docker_available is True
        assert status.container_state == "stopped"
        assert status.tasks == fake_tasks

    @pytest.mark.asyncio
    async def test_building_state_still_reports_progress(self):
        import backend.converter.service as svc

        fake_tasks = [TaskProgress("cell/x", 5, 1, 4, 0, 0)]
        await svc._build_lock.acquire()
        try:
            with patch(
                "backend.converter.service.check_docker",
                new_callable=AsyncMock,
                return_value=True,
            ), patch(
                "backend.converter.service.build_progress",
                return_value=(fake_tasks, "snap"),
            ):
                svc._progress_cache = None
                status = await get_status()
            assert status.container_state == "building"
            assert status.tasks == fake_tasks
        finally:
            svc._build_lock.release()
            svc._progress_cache = None

    @pytest.mark.asyncio
    async def test_cached_progress_collapses_concurrent_scans(self):
        """Concurrent get_status() callers must funnel through a single scan.

        The scan runs via asyncio.to_thread, so a time.sleep inside the
        stubbed build_progress blocks the worker thread long enough that
        the second and third awaiters race into the inner critical section
        and must hit the cache populated by the first.
        """
        import asyncio
        import time as _time

        import backend.converter.service as svc

        calls: list[int] = []

        def slow_build():
            calls.append(1)
            _time.sleep(0.05)
            return [TaskProgress("a/b", 1, 0, 1, 0, 0)], "1 task"

        with patch(
            "backend.converter.service.check_docker",
            new_callable=AsyncMock,
            return_value=False,
        ), patch(
            "backend.converter.service.build_progress",
            side_effect=slow_build,
        ):
            results = await asyncio.gather(
                get_status(), get_status(), get_status()
            )

        assert len(calls) == 1
        assert all(r.tasks and r.tasks[0].cell_task == "a/b" for r in results)


class TestStartConverter:
    @pytest.mark.asyncio
    async def test_allows_dead_container_to_restart(self):
        run_mock = AsyncMock(side_effect=[
            (0, "", ""),
            (0, "started", ""),
        ])

        with patch("backend.converter.service.get_container_state", new_callable=AsyncMock, return_value="dead"), patch(
            "backend.converter.service._run",
            run_mock,
        ), patch(
            "backend.converter.service._compose_cmd",
            side_effect=lambda *args: ["docker", "compose", *args],
        ):
            ok, msg = await start_converter()

        assert ok is True
        assert msg == "started"
        assert run_mock.await_args_list == [
            call(["docker", "rm", "-f", svc.CONTAINER_NAME], timeout=10.0),
            call(["docker", "compose", "run", "-d", "--build", "--name", svc.CONTAINER_NAME, "convert-server", "python3", "/app/auto_converter.py"], timeout=30.0),
        ]


class TestContainerStateHelpers:
    def test_parse_container_state_returns_stopped_for_malformed_json(self):
        info = svc._parse_container_state("{not-json}")

        assert info == ContainerStateInfo(status="stopped")

    def test_parse_container_state_returns_stopped_for_non_object_json(self):
        info = svc._parse_container_state('["running"]')

        assert info == ContainerStateInfo(status="stopped")
