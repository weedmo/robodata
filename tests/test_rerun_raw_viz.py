"""Tests for raw rosbag visualization wiring into the curation web app.

The app backend has no ROS stack, so it triggers the raw viz inside the
converter container, streaming to the shared rerun gRPC sink.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.converter import service as converter_service
from backend.datasets.routers import rerun as rerun_router


class TestBuildRawVizCommand:
    def test_builds_docker_exec_streaming_to_sink(self):
        cmd = converter_service._build_raw_viz_command(
            "cell006/Mamonde_toner_sy/20260226_170029",
            "rerun+grpc://rerun:9876",
        )
        joined = " ".join(cmd)
        assert cmd[:3] == ["docker", "exec", converter_service.CONTAINER_NAME]
        assert "conversion.rerun_viz" in joined
        assert "--recording-dir" in joined
        assert str(converter_service.RAW_BASE) in joined
        assert "20260226_170029" in joined
        assert "--connect" in joined
        assert "rerun+grpc://rerun:9876" in joined

    def test_rejects_path_traversal(self):
        with pytest.raises(ValueError):
            converter_service._build_raw_viz_command(
                "cell006/../../etc/20260226_170029", "rerun+grpc://rerun:9876"
            )

    def test_rejects_invalid_serial(self):
        with pytest.raises(ValueError):
            converter_service._build_raw_viz_command(
                "cell006/task/not-a-serial", "rerun+grpc://rerun:9876"
            )

    def test_rejects_absolute_path(self):
        with pytest.raises(ValueError):
            converter_service._build_raw_viz_command(
                "/etc/20260226_170029", "rerun+grpc://rerun:9876"
            )


class TestVisualizeRawRoute:
    @pytest.mark.asyncio
    async def test_route_triggers_converter_and_returns_ok(self, monkeypatch):
        app = FastAPI()
        app.include_router(rerun_router.router)
        monkeypatch.setattr(
            rerun_router.converter_service,
            "visualize_raw_recording",
            AsyncMock(return_value=(True, "Recording 20260226_170029: 0 raw warning(s)")),
        )

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/rerun/visualize-raw",
                params={"recording": "cell006/Mamonde_toner_sy/20260226_170029"},
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    @pytest.mark.asyncio
    async def test_route_returns_400_on_invalid_recording(self, monkeypatch):
        app = FastAPI()
        app.include_router(rerun_router.router)
        monkeypatch.setattr(
            rerun_router.converter_service,
            "visualize_raw_recording",
            AsyncMock(side_effect=ValueError("invalid recording path")),
        )

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/rerun/visualize-raw",
                params={"recording": "../../etc/passwd"},
            )

        assert resp.status_code == 400


class TestListRecordings:
    def _make(self, base, task, serial, *, with_mcap=True):
        from pathlib import Path

        d = Path(base) / task / serial
        d.mkdir(parents=True)
        (d / "metacard.json").write_text('{"task_name": "pinksponge"}')
        if with_mcap:
            (d / f"{serial}_0.mcap").write_bytes(b"")

    def test_lists_recordings_with_mcap_under_task(self, tmp_path, monkeypatch):
        monkeypatch.setattr(converter_service, "RAW_BASE", tmp_path)
        self._make(tmp_path, "cell006/pinksponge", "20260226_170029")
        self._make(tmp_path, "cell006/pinksponge", "20260226_164701")
        self._make(tmp_path, "cell006/pinksponge", "20260226_999999", with_mcap=False)

        recs = converter_service.list_recordings("cell006/pinksponge")

        serials = {r["serial"] for r in recs}
        assert serials == {"20260226_170029", "20260226_164701"}
        one = next(r for r in recs if r["serial"] == "20260226_170029")
        assert one["recording"] == "cell006/pinksponge/20260226_170029"
        assert one["task_name"] == "pinksponge"

    def test_rejects_task_traversal(self, tmp_path, monkeypatch):
        monkeypatch.setattr(converter_service, "RAW_BASE", tmp_path)
        with pytest.raises(ValueError):
            converter_service.list_recordings("../../etc")
