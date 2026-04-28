"""Tests for grade-reason feature: DB migration, service, router."""

from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from backend.core.db import _reset, close_db, get_db, init_db
from backend.main import app

pytestmark = pytest.mark.usefixtures("_point_settings_at_test_db")


@pytest_asyncio.fixture(autouse=True)
async def reset_db():
    _reset()
    await init_db()
    db = await get_db()
    await db.execute(
        "TRUNCATE TABLE jobs, dataset_stats, episode_serials, datasets, annotations "
        "RESTART IDENTITY CASCADE"
    )
    await db.commit()
    yield
    await close_db()


@pytest_asyncio.fixture
async def client():
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            yield ac


class TestReasonColumn:
    """Reason now lives on the Postgres-backed annotations table."""

    @pytest.mark.asyncio
    async def test_fresh_init_annotations_has_reason_column(self):
        db = await get_db()
        async with db.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'annotations'
            ORDER BY ordinal_position
            """
        ) as cursor:
            rows = await cursor.fetchall()
        col_names = [row["column_name"] for row in rows]
        assert "reason" in col_names

    @pytest.mark.asyncio
    async def test_schema_versions_contains_v1(self):
        db = await get_db()
        async with db.execute(
            "SELECT version FROM schema_versions ORDER BY version DESC LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
        assert row["version"] == 1


class TestSchemas:
    def test_episode_response_includes_reason(self):
        from backend.datasets.schemas import Episode
        ep = Episode(
            episode_index=0, length=100, task_index=0,
            dataset_from_index=0, dataset_to_index=100,
            grade="bad", reason="camera shake",
        )
        assert ep.reason == "camera shake"
        assert ep.model_dump()["reason"] == "camera shake"

    def test_episode_response_default_reason_is_none(self):
        from backend.datasets.schemas import Episode
        ep = Episode(episode_index=0, length=0, task_index=0)
        assert ep.reason is None

    def test_update_requires_reason_for_bad(self):
        from pydantic import ValidationError

        from backend.datasets.schemas import EpisodeUpdate
        with pytest.raises(ValidationError):
            EpisodeUpdate(dataset_path="/tmp/ds", grade="bad", tags=[])
        with pytest.raises(ValidationError):
            EpisodeUpdate(dataset_path="/tmp/ds", grade="bad", tags=[], reason="   ")
        # Non-empty reason is fine
        EpisodeUpdate(dataset_path="/tmp/ds", grade="bad", tags=[], reason="too dark")

    def test_update_requires_reason_for_normal(self):
        from pydantic import ValidationError

        from backend.datasets.schemas import EpisodeUpdate
        with pytest.raises(ValidationError):
            EpisodeUpdate(dataset_path="/tmp/ds", grade="normal", tags=[])
        EpisodeUpdate(dataset_path="/tmp/ds", grade="normal", tags=[], reason="acceptable but slow")

    def test_update_good_does_not_require_reason(self):
        from backend.datasets.schemas import EpisodeUpdate
        u = EpisodeUpdate(dataset_path="/tmp/ds", grade="good", tags=[])
        assert u.reason is None

    def test_update_good_clears_reason_when_provided(self):
        # Reason supplied with grade=good should be allowed but ignored downstream;
        # at the schema level we accept it (service layer will null it out).
        from backend.datasets.schemas import EpisodeUpdate
        u = EpisodeUpdate(dataset_path="/tmp/ds", grade="good", tags=[], reason="ignored")
        assert u.grade == "good"

    def test_bulk_grade_requires_reason_for_bad(self):
        from pydantic import ValidationError

        from backend.datasets.schemas import BulkGradeRequest
        with pytest.raises(ValidationError):
            BulkGradeRequest(dataset_path="/tmp/ds", episode_indices=[0, 1], grade="bad")
        BulkGradeRequest(dataset_path="/tmp/ds", episode_indices=[0, 1], grade="bad", reason="bad batch")

    def test_bulk_grade_good_does_not_require_reason(self):
        from backend.datasets.schemas import BulkGradeRequest
        BulkGradeRequest(dataset_path="/tmp/ds", episode_indices=[0], grade="good")


import json as _json

import pyarrow as pa
import pyarrow.parquet as pq


def _create_mock_dataset(root: Path) -> Path:
    ds = root / "mock_ds"
    (ds / "meta" / "episodes" / "chunk-000").mkdir(parents=True, exist_ok=True)
    (ds / "data" / "chunk-000").mkdir(parents=True, exist_ok=True)
    (ds / "meta" / "info.json").write_text(_json.dumps({
        "fps": 30, "total_episodes": 3, "total_tasks": 1,
        "robot_type": "test_robot", "features": {},
    }))
    pq.write_table(
        pa.table({
            "task_index": pa.array([0], type=pa.int64()),
            "task": pa.array(["test"], type=pa.string()),
        }),
        ds / "meta" / "tasks.parquet",
    )
    pq.write_table(
        pa.table({
            "episode_index": pa.array([0, 1, 2], type=pa.int64()),
            "task_index": pa.array([0, 0, 0], type=pa.int64()),
            "data/chunk_index": pa.array([0, 0, 0], type=pa.int64()),
            "data/file_index": pa.array([0, 0, 0], type=pa.int64()),
            "dataset_from_index": pa.array([0, 100, 200], type=pa.int64()),
            "dataset_to_index": pa.array([100, 200, 300], type=pa.int64()),
            "Serial_number": pa.array(
                [
                    "MOCK_REASON_0",
                    "MOCK_REASON_1",
                    "MOCK_REASON_2",
                ],
                type=pa.string(),
            ),
        }),
        ds / "meta" / "episodes" / "chunk-000" / "file-000.parquet",
    )
    pq.write_table(
        pa.table({
            "episode_index": pa.array([0, 1, 2], type=pa.int64()),
            "timestamp": pa.array([0.0, 0.0, 0.0], type=pa.float32()),
        }),
        ds / "data" / "chunk-000" / "file-000.parquet",
    )
    return ds


@pytest_asyncio.fixture
async def loaded_service(tmp_path):
    """Create a fresh EpisodeService pointing at a mock dataset."""
    from backend.core.config import settings
    from backend.datasets.services.dataset_service import DatasetService
    from backend.datasets.services.episode_service import EpisodeService

    ds_path = _create_mock_dataset(tmp_path)
    original_roots = settings.allowed_dataset_roots

    # Replace module-level singletons
    import backend.datasets.services.dataset_service as ds_mod
    import backend.datasets.services.episode_service as ep_mod
    import backend.datasets.services.dataset_registry as registry_mod
    import backend.datasets.routers.episodes as episodes_router

    original_dataset_service = ds_mod.dataset_service
    original_episode_dataset_service = ep_mod.dataset_service
    original_episode_service = ep_mod.episode_service
    original_router_episode_service = episodes_router.episode_service
    original_registry_roots = registry_mod.settings.allowed_dataset_roots
    allowed_roots = original_roots
    if str(ds_path.parent) not in allowed_roots:
        allowed_roots = original_roots + [str(ds_path.parent)]
    settings.allowed_dataset_roots = allowed_roots
    ds_mod.settings.allowed_dataset_roots = allowed_roots
    ep_mod.settings.allowed_dataset_roots = allowed_roots
    registry_mod.settings.allowed_dataset_roots = allowed_roots
    registry_mod.dataset_registry._items.clear()
    registry_mod.dataset_registry._key_to_path.clear()
    try:
        ds_mod.dataset_service = DatasetService()
        ds_mod.dataset_service.load_dataset(str(ds_path))
        ep_mod.dataset_service = ds_mod.dataset_service
        ep_mod.episode_service = EpisodeService()
        ep_mod.episode_service.dataset_path_for_tests = str(ds_path)
        episodes_router.episode_service = ep_mod.episode_service
        yield ep_mod.episode_service
    finally:
        settings.allowed_dataset_roots = original_roots
        ds_mod.settings.allowed_dataset_roots = original_roots
        ep_mod.settings.allowed_dataset_roots = original_roots
        registry_mod.settings.allowed_dataset_roots = original_registry_roots
        registry_mod.dataset_registry._items.clear()
        registry_mod.dataset_registry._key_to_path.clear()
        ds_mod.dataset_service = original_dataset_service
        ep_mod.dataset_service = original_episode_dataset_service
        ep_mod.episode_service = original_episode_service
        episodes_router.episode_service = original_router_episode_service


class TestEpisodeServiceReason:
    @pytest.mark.asyncio
    async def test_update_persists_reason(self, loaded_service):
        await loaded_service.update_episode(0, "bad", [], reason="motor jitter")
        ep = await loaded_service.get_episode(0)
        assert ep["grade"] == "bad"
        assert ep["reason"] == "motor jitter"

    @pytest.mark.asyncio
    async def test_switch_to_good_clears_reason(self, loaded_service):
        await loaded_service.update_episode(0, "bad", [], reason="too dark")
        await loaded_service.update_episode(0, "good", [], reason=None)
        ep = await loaded_service.get_episode(0)
        assert ep["grade"] == "good"
        assert ep["reason"] is None

    @pytest.mark.asyncio
    async def test_bulk_grade_applies_same_reason(self, loaded_service):
        await loaded_service.bulk_grade([0, 1, 2], "bad", reason="bad batch")
        for idx in (0, 1, 2):
            ep = await loaded_service.get_episode(idx)
            assert ep["grade"] == "bad"
            assert ep["reason"] == "bad batch"

    @pytest.mark.asyncio
    async def test_parquet_does_not_get_reason_column(self, loaded_service, tmp_path):
        await loaded_service.update_episode(0, "bad", [], reason="should-not-appear")
        # Read the parquet directly
        ds = next(tmp_path.glob("mock_ds"))
        pq_file = ds / "meta" / "episodes" / "chunk-000" / "file-000.parquet"
        table = pq.read_table(pq_file)
        assert "reason" not in table.schema.names


class TestRouter:
    @pytest.mark.asyncio
    async def test_patch_with_reason_persists(self, client, loaded_service):
        dataset_path = loaded_service.dataset_path_for_tests
        r = await client.patch(
            "/api/episodes/0",
            json={"dataset_path": dataset_path, "grade": "bad", "tags": [], "reason": "lighting bad"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["grade"] == "bad"
        assert body["reason"] == "lighting bad"

    @pytest.mark.asyncio
    async def test_patch_bad_without_reason_rejected(self, client, loaded_service):
        r = await client.patch(
            "/api/episodes/0",
            json={"dataset_path": loaded_service.dataset_path_for_tests, "grade": "bad", "tags": []},
        )
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_patch_good_clears_reason(self, client, loaded_service):
        dataset_path = loaded_service.dataset_path_for_tests
        r = await client.patch(
            "/api/episodes/0",
            json={"dataset_path": dataset_path, "grade": "bad", "tags": [], "reason": "x"},
        )
        assert r.status_code == 200
        r = await client.patch("/api/episodes/0", json={"dataset_path": dataset_path, "grade": "good", "tags": []})
        assert r.status_code == 200
        assert r.json()["reason"] is None

    @pytest.mark.asyncio
    async def test_bulk_grade_with_reason(self, client, loaded_service):
        dataset_path = loaded_service.dataset_path_for_tests
        r = await client.post(
            "/api/episodes/bulk-grade",
            json={
                "dataset_path": dataset_path,
                "episode_indices": [0, 1],
                "grade": "bad",
                "reason": "batch fail",
            },
        )
        assert r.status_code == 200
        assert r.json()["updated"] == 2
        # Confirm via GET
        r = await client.get("/api/episodes/0", params={"dataset_path": dataset_path})
        assert r.json()["reason"] == "batch fail"

    @pytest.mark.asyncio
    async def test_bulk_grade_bad_without_reason_rejected(self, client, loaded_service):
        r = await client.post(
            "/api/episodes/bulk-grade",
            json={
                "dataset_path": loaded_service.dataset_path_for_tests,
                "episode_indices": [0, 1],
                "grade": "bad",
            },
        )
        assert r.status_code == 422

    async def test_bulk_grade_unmapped_episode_returns_400(self, client, loaded_service):
        r = await client.post(
            "/api/episodes/bulk-grade",
            json={
                "dataset_path": loaded_service.dataset_path_for_tests,
                "episode_indices": [-1],
                "grade": "good",
            },
        )
        assert r.status_code == 400
        assert "no serial_number" in r.json()["detail"]
