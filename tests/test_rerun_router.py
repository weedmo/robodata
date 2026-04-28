"""Tests for rerun availability handling."""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.datasets.routers import rerun as rerun_router


class TestRerunRouter:
    @pytest.mark.asyncio
    async def test_visualize_returns_503_when_rerun_is_unavailable(self, monkeypatch):
        app = FastAPI()
        app.include_router(rerun_router.router)
        monkeypatch.setattr(
            rerun_router.rerun_service,
            "visualize_episode",
            AsyncMock(side_effect=RuntimeError("Rerun viewer is not available")),
        )

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/rerun/visualize/12",
                params={"dataset_path": "/tmp/dataset"},
            )

        assert response.status_code == 503
        assert response.json() == {"detail": "Rerun viewer is not available"}
