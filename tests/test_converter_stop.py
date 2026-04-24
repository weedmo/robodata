"""Tests for stopping the converter without tearing down the full stack."""

from unittest.mock import AsyncMock, call, patch

import pytest

import backend.converter.service as svc


class TestStopConverter:
    @pytest.mark.asyncio
    async def test_removes_container_then_stops_only_converter_service(self):
        run_mock = AsyncMock(
            side_effect=[
                (0, "removed", ""),
                (0, "stopped", ""),
            ]
        )

        with patch("backend.converter.service._run", run_mock), patch(
            "backend.converter.service._compose_cmd",
            side_effect=lambda *args: ["docker", "compose", *args],
        ):
            ok, msg = await svc.stop_converter()

        assert ok is True
        assert msg == "stopped"
        assert run_mock.await_args_list == [
            call(["docker", "rm", "-f", svc.CONTAINER_NAME], timeout=15.0),
            call(["docker", "compose", "stop", "converter"], timeout=30.0),
        ]

    @pytest.mark.asyncio
    async def test_ignores_missing_container_when_cleanup_is_otherwise_successful(self):
        run_mock = AsyncMock(
            side_effect=[
                (
                    1,
                    "",
                    f'Error response from daemon: No such container: "{svc.CONTAINER_NAME}"',
                ),
                (0, "stopped", ""),
            ]
        )

        with patch("backend.converter.service._run", run_mock), patch(
            "backend.converter.service._compose_cmd",
            side_effect=lambda *args: ["docker", "compose", *args],
        ):
            ok, msg = await svc.stop_converter()

        assert ok is True
        assert msg == "stopped"

    @pytest.mark.asyncio
    async def test_reports_container_cleanup_failure_when_other_errors_are_present(self):
        mixed_error = (
            f'Error response from daemon: No such container: "{svc.CONTAINER_NAME}"\n'
            "permission denied"
        )
        run_mock = AsyncMock(
            side_effect=[
                (1, "", mixed_error),
                (0, "stopped", ""),
            ]
        )

        with patch("backend.converter.service._run", run_mock), patch(
            "backend.converter.service._compose_cmd",
            side_effect=lambda *args: ["docker", "compose", *args],
        ):
            ok, msg = await svc.stop_converter()

        assert ok is False
        assert "permission denied" in msg
        assert "stopped" not in msg

    @pytest.mark.asyncio
    async def test_reports_converter_service_stop_failure_after_container_cleanup(self):
        run_mock = AsyncMock(
            side_effect=[
                (0, "removed", ""),
                (1, "", "failed to stop converter service"),
            ]
        )

        with patch("backend.converter.service._run", run_mock), patch(
            "backend.converter.service._compose_cmd",
            side_effect=lambda *args: ["docker", "compose", *args],
        ):
            ok, msg = await svc.stop_converter()

        assert ok is False
        assert msg == "failed to stop converter service"
