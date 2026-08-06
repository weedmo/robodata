import asyncio
import json
import threading
import time
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from backend.core.config import settings
from backend.datasets.services import cell_service

_FRONTEND_ASSETS = Path(__file__).resolve().parents[1] / "frontend" / "dist" / "assets"
_FRONTEND_ASSETS.mkdir(parents=True, exist_ok=True)

from backend.main import app


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def configured_sources(tmp_path, monkeypatch):
    base = tmp_path / "data_div" / "2026_1"
    source_a = base / "lerobot"
    source_b = base / "lerobot_test"
    ignored = base / "not_registered"

    for source, cell_names in (
        (source_a, ("cell001", "cell002")),
        (source_b, ("cell101",)),
        (ignored, ("cell999",)),
    ):
        for cell_name in cell_names:
            meta = source / cell_name / "dataset_a" / "meta"
            meta.mkdir(parents=True)
            (meta / "info.json").write_text(
                json.dumps(
                    {
                        "fps": 30,
                        "total_episodes": 10,
                        "robot_type": "ur5e",
                        "features": {},
                        "total_tasks": 1,
                    }
                ),
                encoding="utf-8",
            )

    monkeypatch.setattr(settings, "dataset_root_base", str(base), raising=False)
    monkeypatch.setattr(settings, "dataset_sources", ["lerobot", "lerobot_test"], raising=False)
    monkeypatch.setattr(settings, "allowed_dataset_roots", [str(source_a), str(source_b)])

    return {
        "base": base,
        "lerobot": source_a,
        "lerobot_test": source_b,
    }


class TestCellsAPI:
    @pytest.mark.asyncio
    async def test_list_sources_scan_does_not_block_event_loop(self, client, monkeypatch):
        """A slow NAS discovery must not delay unrelated async API handlers."""
        release = threading.Event()

        def slow_scan(*_args, **_kwargs):
            release.wait(timeout=1)
            return []

        monkeypatch.setattr("backend.datasets.routers.cells.list_dataset_sources", slow_scan)
        timer = threading.Timer(0.5, release.set)
        timer.start()
        started = time.monotonic()
        source_request = asyncio.create_task(client.get("/api/cells/sources"))
        await asyncio.sleep(0)
        health = await client.get("/api/health")
        elapsed = time.monotonic() - started
        release.set()
        sources = await source_request
        timer.cancel()

        assert health.status_code == 200
        assert sources.status_code == 200
        assert elapsed < 0.25

    @pytest.mark.asyncio
    async def test_list_cells_scan_does_not_block_event_loop(
        self, client, configured_sources, monkeypatch
    ):
        release = threading.Event()

        def slow_scan(*_args, **_kwargs):
            release.wait(timeout=1)
            return []

        monkeypatch.setattr("backend.datasets.routers.cells.scan_cells", slow_scan)
        timer = threading.Timer(0.5, release.set)
        timer.start()
        started = time.monotonic()
        cells_request = asyncio.create_task(
            client.get("/api/cells", params={"root": str(configured_sources["lerobot"])})
        )
        await asyncio.sleep(0)
        health = await client.get("/api/health")
        elapsed = time.monotonic() - started
        release.set()
        cells = await cells_request
        timer.cancel()

        assert health.status_code == 200
        assert cells.status_code == 200
        assert elapsed < 0.25

    @pytest.mark.asyncio
    async def test_list_sources_returns_only_registered_sources(self, client, configured_sources):
        resp = await client.get("/api/cells/sources")

        assert resp.status_code == 200
        payload = resp.json()
        assert [item["name"] for item in payload] == ["lerobot", "lerobot_test"]
        assert [item["cell_count"] for item in payload] == [2, 1]
        assert [item["path"] for item in payload] == [
            str(configured_sources["lerobot"].resolve()),
            str(configured_sources["lerobot_test"].resolve()),
        ]

    @pytest.mark.asyncio
    async def test_list_cells_filters_to_requested_root(self, client, configured_sources):
        resp = await client.get("/api/cells", params={"root": str(configured_sources["lerobot_test"])})

        assert resp.status_code == 200
        payload = resp.json()
        assert [item["name"] for item in payload] == ["cell101"]
        assert payload[0]["mount_root"] == str(configured_sources["lerobot_test"].resolve())

    @pytest.mark.asyncio
    async def test_dataset_scan_skips_unreadable_sibling(self, tmp_path, monkeypatch):
        source_root = tmp_path / "lerobot_test"
        meta = source_root / "valid_dataset" / "meta"
        meta.mkdir(parents=True)
        (meta / "info.json").write_text(
            json.dumps(
                {
                    "fps": 30,
                    "total_episodes": 1,
                    "robot_type": "ur5e",
                    "features": {},
                    "total_tasks": 1,
                }
            ),
            encoding="utf-8",
        )
        unreadable = source_root / "rollout-type1-cycle1-260513"
        unreadable.mkdir()
        unreadable.chmod(0)

        async def noop_upsert(_cell_name, _datasets):
            return None

        monkeypatch.setattr(cell_service, "_upsert_datasets_to_db", noop_upsert)

        try:
            datasets = await cell_service.get_datasets_in_cell(str(source_root))
        finally:
            unreadable.chmod(0o700)

        assert [dataset.name for dataset in datasets] == ["valid_dataset"]

    @pytest.mark.asyncio
    async def test_dataset_scan_self_recovers_when_grade_metadata_unreadable(self, tmp_path, monkeypatch):
        source_root = tmp_path / "lerobot_test"
        meta = source_root / "valid_dataset" / "meta"
        meta.mkdir(parents=True)
        (meta / "info.json").write_text(
            json.dumps(
                {
                    "fps": 30,
                    "total_episodes": 1,
                    "robot_type": "ur5e",
                    "features": {},
                    "total_tasks": 1,
                }
            ),
            encoding="utf-8",
        )

        async def fail_count_grades(_dataset_dir, _fps):
            raise PermissionError("metadata temporarily unavailable")

        async def noop_upsert(_cell_name, _datasets):
            return None

        monkeypatch.setattr(cell_service, "_count_grades", fail_count_grades)
        monkeypatch.setattr(cell_service, "_upsert_datasets_to_db", noop_upsert)

        datasets = await cell_service.get_datasets_in_cell(str(source_root))

        assert [dataset.name for dataset in datasets] == ["valid_dataset"]
        assert datasets[0].graded_count == 0
        assert datasets[0].total_duration_sec == 0.0
