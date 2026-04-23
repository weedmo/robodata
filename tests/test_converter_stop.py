"""Tests for stopping the converter container stack."""

from unittest.mock import AsyncMock, call, patch

import pytest

import backend.converter.service as svc


class TestStopConverter:
    @pytest.mark.asyncio
    async def test_calls_docker_rm_before_compose_down(self):
        run_mock = AsyncMock(side_effect=[
            (0, "removed", ""),
            (0, "", ""),
        ])

        with patch("backend.converter.service._run", run_mock), patch(
            "backend.converter.service._compose_cmd",
            side_effect=lambda *args: ["docker", "compose", *args],
        ):
            ok, msg = await svc.stop_converter()

        assert ok is True
        assert msg == "stopped"
        assert run_mock.await_args_list == [
            call(["docker", "rm", "-f", svc.CONTAINER_NAME], timeout=15.0),
            call(["docker", "compose", "down"], timeout=30.0),
        ]

    @pytest.mark.asyncio
    async def test_ignores_no_such_container_when_removing_one_off_container(self):
        run_mock = AsyncMock(side_effect=[
            (1, "", "Error: No such container: convert-server"),
            (0, "down", ""),
        ])

        with patch("backend.converter.service._run", run_mock), patch(
            "backend.converter.service._compose_cmd",
            side_effect=lambda *args: ["docker", "compose", *args],
        ):
            ok, _ = await svc.stop_converter()

        assert ok is True
        assert run_mock.await_args_list == [
            call(["docker", "rm", "-f", svc.CONTAINER_NAME], timeout=15.0),
            call(["docker", "compose", "down"], timeout=30.0),
        ]

    @pytest.mark.asyncio
    async def test_ignores_duplicate_missing_container_lines_and_warnings(self):
        run_mock = AsyncMock(side_effect=[
            (
                1,
                "",
                'time="2026-04-22T14:00:00Z" level=warning msg="compose v1 is deprecated"\n'
                "Error response from daemon: No such container: convert-server\n"
                "Error response from daemon: No such container: convert-server",
            ),
            (0, "down", ""),
        ])

        with patch("backend.converter.service._run", run_mock), patch(
            "backend.converter.service._compose_cmd",
            side_effect=lambda *args: ["docker", "compose", *args],
        ):
            ok, msg = await svc.stop_converter()

        assert ok is True
        assert msg == "down"
        assert run_mock.await_args_list == [
            call(["docker", "rm", "-f", svc.CONTAINER_NAME], timeout=15.0),
            call(["docker", "compose", "down"], timeout=30.0),
        ]

    @pytest.mark.asyncio
    async def test_reports_rm_failure_when_no_such_container_is_mixed_with_other_errors(self):
        run_mock = AsyncMock(side_effect=[
            (
                1,
                "",
                "Error response from daemon: No such container: convert-server\npermission denied",
            ),
            (0, "down", ""),
        ])

        with patch("backend.converter.service._run", run_mock), patch(
            "backend.converter.service._compose_cmd",
            side_effect=lambda *args: ["docker", "compose", *args],
        ):
            ok, msg = await svc.stop_converter()

        assert ok is False
        assert "permission denied" in msg
        assert run_mock.await_args_list == [
            call(["docker", "rm", "-f", svc.CONTAINER_NAME], timeout=15.0),
            call(["docker", "compose", "down"], timeout=30.0),
        ]

    @pytest.mark.asyncio
    async def test_reports_compose_down_failure_after_rm_succeeds(self):
        run_mock = AsyncMock(side_effect=[
            (0, "removed", ""),
            (1, "", "permission denied"),
        ])

        with patch("backend.converter.service._run", run_mock), patch(
            "backend.converter.service._compose_cmd",
            side_effect=lambda *args: ["docker", "compose", *args],
        ):
            ok, msg = await svc.stop_converter()

        assert ok is False
        assert msg == "permission denied"
        assert run_mock.await_args_list == [
            call(["docker", "rm", "-f", svc.CONTAINER_NAME], timeout=15.0),
            call(["docker", "compose", "down"], timeout=30.0),
        ]
