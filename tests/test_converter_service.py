"""Tests for converter progress and container status logic."""

from unittest.mock import AsyncMock, patch

import pytest

from backend.converter.service import TaskProgress, build_progress, get_status


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
            "backend.converter.service.get_container_state",
            new_callable=AsyncMock,
            return_value="stopped",
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
        """Multiple get_status() calls within TTL must only scan once."""
        import backend.converter.service as svc

        calls: list[int] = []

        def counting_build():
            calls.append(1)
            return [TaskProgress("a/b", 1, 0, 1, 0, 0)], "1 task"

        with patch(
            "backend.converter.service.check_docker",
            new_callable=AsyncMock,
            return_value=False,
        ), patch(
            "backend.converter.service.build_progress",
            side_effect=counting_build,
        ):
            svc._progress_cache = None
            await get_status()
            await get_status()
            await get_status()

        assert len(calls) == 1
        svc._progress_cache = None
