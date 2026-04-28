"""API integration tests against real LeRobot v3.0 datasets using FastAPI TestClient."""

from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from backend.main import app
from backend.core.config import settings
from backend.datasets.services.dataset_registry import dataset_registry

BASIC_AIC = "/tmp/hf-mounts/Phy-lab/dataset/basic_aic_cheetcode_dataset"
HOJUN = "/tmp/hf-mounts/Phy-lab/dataset/hojun"

pytestmark = pytest.mark.skipif(
    not Path(BASIC_AIC).exists() or not Path(HOJUN).exists(),
    reason="real LeRobot fixture datasets are not available",
)


@pytest.fixture(autouse=True)
def reset_registry(monkeypatch):
    """Reset path-keyed dataset registry per test."""
    roots = list(settings.allowed_dataset_roots)
    for path in (BASIC_AIC, HOJUN):
        root = str(Path(path).parent)
        if root not in roots:
            roots.append(root)
    monkeypatch.setattr(settings, "allowed_dataset_roots", roots)
    dataset_registry._items.clear()
    dataset_registry._key_to_path.clear()
    yield
    dataset_registry._items.clear()
    dataset_registry._key_to_path.clear()


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class TestHealth:
    @pytest.mark.asyncio
    async def test_health_returns_ok(self, client):
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# POST /api/datasets/load
# ---------------------------------------------------------------------------

class TestLoadDatasetAPI:
    @pytest.mark.asyncio
    async def test_load_basic_aic(self, client, reset_registry):
        resp = await client.post("/api/datasets/load", json={"path": BASIC_AIC})
        assert resp.status_code == 200
        data = resp.json()
        assert data["fps"] == 20
        assert data["total_episodes"] == 40
        assert data["total_tasks"] == 2
        assert data["robot_type"] == "ur5e"
        assert "features" in data

    @pytest.mark.asyncio
    async def test_load_hojun(self, client, reset_registry):
        resp = await client.post("/api/datasets/load", json={"path": HOJUN})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_tasks"] == 1

    @pytest.mark.asyncio
    async def test_load_missing_out_of_root_returns_400(self, client, reset_registry, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "allowed_dataset_roots", [str(tmp_path)])
        out_of_root_path = tmp_path.parent / f"{tmp_path.name}-outside" / "missing-dataset"

        resp = await client.post("/api/datasets/load", json={"path": str(out_of_root_path)})
        assert resp.status_code == 400
        assert "not under any allowed root" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_load_disallowed_path_returns_400(self, client, reset_registry):
        resp = await client.post("/api/datasets/load", json={"path": "/etc/passwd"})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GET /api/datasets/info
# ---------------------------------------------------------------------------

class TestGetInfoAPI:
    @pytest.mark.asyncio
    async def test_info_after_load(self, client, reset_registry):
        await client.post("/api/datasets/load", json={"path": BASIC_AIC})
        resp = await client.get("/api/datasets/info", params={"dataset_path": BASIC_AIC})
        assert resp.status_code == 200
        data = resp.json()
        assert data["fps"] == 20
        assert data["robot_type"] == "ur5e"

    @pytest.mark.asyncio
    async def test_info_before_load_returns_400(self, client):
        resp = await client.get("/api/datasets/info")
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/episodes
# ---------------------------------------------------------------------------

class TestEpisodesAPI:
    @pytest.mark.asyncio
    async def test_list_episodes(self, client, reset_registry):
        await client.post("/api/datasets/load", json={"path": BASIC_AIC})
        resp = await client.get("/api/episodes", params={"dataset_path": BASIC_AIC})
        assert resp.status_code == 200
        episodes = resp.json()
        assert len(episodes) == 40

    @pytest.mark.asyncio
    async def test_episode_schema(self, client, reset_registry):
        await client.post("/api/datasets/load", json={"path": BASIC_AIC})
        resp = await client.get("/api/episodes", params={"dataset_path": BASIC_AIC})
        ep = resp.json()[0]
        assert "episode_index" in ep
        assert "length" in ep
        assert "task_index" in ep
        assert "task_instruction" in ep
        assert "grade" in ep
        assert "tags" in ep

    @pytest.mark.asyncio
    async def test_get_single_episode(self, client, reset_registry):
        await client.post("/api/datasets/load", json={"path": BASIC_AIC})
        resp = await client.get("/api/episodes/0", params={"dataset_path": BASIC_AIC})
        assert resp.status_code == 200
        assert resp.json()["episode_index"] == 0

    @pytest.mark.asyncio
    async def test_get_nonexistent_episode_returns_404(self, client, reset_registry):
        await client.post("/api/datasets/load", json={"path": BASIC_AIC})
        resp = await client.get("/api/episodes/9999", params={"dataset_path": BASIC_AIC})
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /api/episodes/{episode_index}
# ---------------------------------------------------------------------------

class TestUpdateEpisodeAPI:
    @pytest.mark.asyncio
    async def test_update_episode_grade(self, client, writable_basic_aic, reset_registry):
        await client.post("/api/datasets/load", json={"path": str(writable_basic_aic)})
        resp = await client.patch(
            "/api/episodes/0",
            json={"dataset_path": str(writable_basic_aic), "grade": "good", "tags": ["test"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["grade"] == "good"
        assert data["tags"] == ["test"]

    @pytest.mark.asyncio
    async def test_update_invalid_grade_returns_422(self, client, writable_basic_aic, reset_registry):
        await client.post("/api/datasets/load", json={"path": str(writable_basic_aic)})
        resp = await client.patch("/api/episodes/0", json={"dataset_path": str(writable_basic_aic), "grade": "Z"})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_update_nonexistent_episode_returns_404(self, client, writable_basic_aic, reset_registry):
        await client.post("/api/datasets/load", json={"path": str(writable_basic_aic)})
        resp = await client.patch(
            "/api/episodes/9999",
            json={"dataset_path": str(writable_basic_aic), "grade": "good", "tags": []},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/tasks
# ---------------------------------------------------------------------------

class TestTasksAPI:
    @pytest.mark.asyncio
    async def test_list_tasks(self, client, reset_registry):
        await client.post("/api/datasets/load", json={"path": BASIC_AIC})
        resp = await client.get("/api/tasks", params={"dataset_path": BASIC_AIC})
        assert resp.status_code == 200
        tasks = resp.json()
        assert len(tasks) == 2

    @pytest.mark.asyncio
    async def test_task_schema(self, client, reset_registry):
        await client.post("/api/datasets/load", json={"path": BASIC_AIC})
        resp = await client.get("/api/tasks", params={"dataset_path": BASIC_AIC})
        t = resp.json()[0]
        assert "task_index" in t
        assert "task_instruction" in t


# ---------------------------------------------------------------------------
# PATCH /api/tasks/{task_index}
# ---------------------------------------------------------------------------

class TestUpdateTaskAPI:
    @pytest.mark.asyncio
    async def test_update_task_instruction(self, client, writable_basic_aic, reset_registry):
        await client.post("/api/datasets/load", json={"path": str(writable_basic_aic)})
        resp = await client.patch(
            "/api/tasks/0",
            json={"dataset_path": str(writable_basic_aic), "task_instruction": "updated task"},
        )
        assert resp.status_code == 200
        assert resp.json()["task_instruction"] == "updated task"

    @pytest.mark.asyncio
    async def test_update_nonexistent_task_returns_404(self, client, writable_basic_aic, reset_registry):
        await client.post("/api/datasets/load", json={"path": str(writable_basic_aic)})
        resp = await client.patch(
            "/api/tasks/9999",
            json={"dataset_path": str(writable_basic_aic), "task_instruction": "nope"},
        )
        assert resp.status_code == 404
