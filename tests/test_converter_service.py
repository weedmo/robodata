"""Tests for converter progress and container status logic."""

import asyncio
import threading
import time
from unittest.mock import AsyncMock, call, patch

import pytest

import backend.converter.service as svc

from backend.converter.service import (
    ContainerStateInfo,
    DockerServiceStatus,
    TaskProgress,
    build_progress,
    get_status,
    _compose_cmd,
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

    def test_mark_failed_recordings_retryable_moves_failures_to_retry_queue(self, tmp_path):
        state_file = tmp_path / "convert_state.json"
        state_file.write_text(
            """
            {
              "cell001/task_a": {
                "converted_count": 4,
                "failed_serials": ["s1", "s2"],
                "transient_failed": {
                  "old": {
                    "attempt_count": 1,
                    "first_failed_at": 1,
                    "next_retry_at": 2,
                    "last_error": "network"
                  }
                }
              }
            }
            """,
            encoding="utf-8",
        )

        with patch("backend.converter.service.STATE_FILE", state_file):
            moved = svc.mark_failed_recordings_retryable("cell001/task_a")
            state = svc.read_state()

        assert moved == 2
        entry = state["cell001/task_a"]
        assert entry["failed_serials"] == []
        assert set(entry["transient_failed"]) == {"old", "s1", "s2"}
        assert entry["transient_failed"]["s1"]["next_retry_at"] == 0


class TestGetStatus:
    @pytest.fixture(autouse=True)
    def _reset_progress_cache(self):
        import backend.converter.service as svc

        svc._progress_cache = None
        svc._progress_refresh_task = None
        yield
        task = svc._progress_refresh_task
        if task is not None and not task.done():
            task.cancel()
        svc._progress_cache = None
        svc._progress_refresh_task = None

    @pytest.mark.asyncio
    async def test_docker_unavailable_still_reports_progress(self):
        fake_tasks = [TaskProgress("cell001/task_a", 10, 4, 6, 0, 0)]
        with patch(
            "backend.converter.service.check_docker",
            new_callable=AsyncMock,
            return_value=False,
        ), patch(
            "backend.converter.service._cached_progress",
            new_callable=AsyncMock,
            return_value=(fake_tasks, "1 tasks | 10 recordings | 4 done | 6 pending | 0 failed"),
        ):
            status = await get_status()

        assert status.docker_available is False
        assert status.container_state == "stopped"
        # The queue accepts work regardless of Docker reachability — the worker
        # picks it up on its own schedule. task_start_available stays True so
        # the UI can keep enqueueing.
        assert status.task_start_available is True
        assert status.tasks == fake_tasks
        assert "4 done" in status.summary

    @pytest.mark.asyncio
    async def test_status_includes_compose_service_health(self):
        fake_services = [
            DockerServiceStatus("app", "running", True, "Up 2 minutes"),
            DockerServiceStatus("converter", "exited", False, "Exited"),
        ]
        with patch(
            "backend.converter.service.check_docker",
            new_callable=AsyncMock,
            return_value=False,
        ), patch(
            "backend.converter.service._cached_progress",
            new_callable=AsyncMock,
            return_value=([], "0 tasks"),
        ), patch(
            "backend.converter.service.list_docker_services",
            return_value=fake_services,
        ):
            status = await get_status()

        assert status.docker_services == fake_services

    @pytest.mark.asyncio
    async def test_stopped_container_uses_progress_snapshot(self):
        fake_tasks = [TaskProgress("a/b", 10, 5, 3, 2, 0)]
        with patch("backend.converter.service.check_docker", new_callable=AsyncMock, return_value=True), patch(
            "backend.converter.service.get_container_state_info",
            new_callable=AsyncMock,
            return_value=ContainerStateInfo(status="stopped"),
        ), patch(
            "backend.converter.service._cached_progress",
            new_callable=AsyncMock,
            return_value=(fake_tasks, "Total: 1 task"),
        ):
            status = await get_status()

        assert status.docker_available is True
        assert status.container_state == "stopped"
        assert status.task_start_available is True
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
                "backend.converter.service._cached_progress",
                new_callable=AsyncMock,
                return_value=(fake_tasks, "snap"),
            ):
                svc._progress_cache = None
                status = await get_status()
            assert status.container_state == "building"
            # task_start_available stays True even during build — clicking
            # Convert just enqueues into the jobs queue, which is independent
            # of the converter image build state.
            assert status.task_start_available is True
            assert status.tasks == fake_tasks
        finally:
            svc._build_lock.release()
            svc._progress_cache = None

    @pytest.mark.asyncio
    async def test_cached_progress_cold_refresh_does_not_block_callers(self):
        """A cold NAS scan must run once in background without holding HTTP callers."""
        import backend.converter.service as svc

        calls: list[int] = []
        release = threading.Event()

        def slow_build():
            calls.append(1)
            release.wait(timeout=2)
            return [TaskProgress("a/b", 1, 0, 1, 0, 0)], "1 task"

        with patch("backend.converter.service.build_progress", side_effect=slow_build):
            started = time.monotonic()
            results = await asyncio.wait_for(
                asyncio.gather(
                    svc._cached_progress(),
                    svc._cached_progress(),
                    svc._cached_progress(),
                ),
                timeout=0.25,
            )
            elapsed = time.monotonic() - started

            assert elapsed < 0.25
            assert results == [
                ([], "Progress scan in progress"),
                ([], "Progress scan in progress"),
                ([], "Progress scan in progress"),
            ]
            assert len(calls) == 1

            release.set()
            assert svc._progress_refresh_task is not None
            await svc._progress_refresh_task

        tasks, summary = await svc._cached_progress()
        assert tasks == [TaskProgress("a/b", 1, 0, 1, 0, 0)]
        assert summary == "1 task"

    @pytest.mark.asyncio
    async def test_cached_progress_returns_stale_snapshot_while_refreshing(self):
        import backend.converter.service as svc

        stale_tasks = [TaskProgress("old/task", 2, 1, 1, 0, 0)]
        svc._progress_cache = (0.0, stale_tasks, "stale")
        release = threading.Event()

        def slow_build():
            release.wait(timeout=2)
            return [TaskProgress("new/task", 3, 2, 1, 0, 0)], "fresh"

        with patch("backend.converter.service.build_progress", side_effect=slow_build):
            result = await asyncio.wait_for(svc._cached_progress(), timeout=0.25)
            assert result == (stale_tasks, "stale")

            release.set()
            assert svc._progress_refresh_task is not None
            await svc._progress_refresh_task

        assert (await svc._cached_progress())[1] == "fresh"


class TestStartConverter:
    def test_compose_cmd_uses_unified_stack_with_convert_profile_and_env_file(self):
        cmd = _compose_cmd("ps")

        assert cmd[:4] == ["docker", "compose", "--env-file", str(svc.COMPOSE_ENV_FILE)]
        assert cmd[4:8] == ["-p", svc.PROJECT_NAME, "-f", str(svc.COMPOSE_FILE)]
        assert cmd[8:10] == ["--profile", svc.COMPOSE_PROFILE]
        assert cmd[-1] == "ps"

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
            call(
                [
                    "docker",
                    "compose",
                    "up",
                    "-d",
                    "--build",
                    "converter",
                ],
                timeout=30.0,
            ),
        ]


class TestContainerStateHelpers:
    def test_parse_container_state_returns_stopped_for_malformed_json(self):
        info = svc._parse_container_state("{not-json}")

        assert info == ContainerStateInfo(status="stopped")

    def test_parse_container_state_returns_stopped_for_non_object_json(self):
        info = svc._parse_container_state('["running"]')

        assert info == ContainerStateInfo(status="stopped")
