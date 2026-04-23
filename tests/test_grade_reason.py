"""Tests for grade-reason feature: DB migration, service, router."""

import tempfile
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

from backend.core.db import _reset, close_db, get_db, init_db


@pytest_asyncio.fixture(autouse=True)
async def tmp_db(monkeypatch):
    _reset()
    tmp = Path(tempfile.mkdtemp()) / "test.db"
    monkeypatch.setattr("backend.core.db._db_path_override", str(tmp))
    yield tmp
    await close_db()
    _reset()
    tmp.unlink(missing_ok=True)


class TestReasonColumn:
    """Reason now lives on annotations (schema v4). The v1→v2 in-place
    migration test is obsolete because v4 refuses to upgrade a DB that
    still holds episode_annotations rows — the operator must run
    scripts/reset_db first, so reason preservation across that path is
    handled by the reset-then-reannotate contract.
    """

    @pytest.mark.asyncio
    async def test_fresh_init_annotations_has_reason_column(self, tmp_db):
        await init_db()
        db = await get_db()
        async with db.execute("PRAGMA table_info(annotations)") as cursor:
            rows = await cursor.fetchall()
        col_names = [r[1] for r in rows]
        assert "reason" in col_names

    @pytest.mark.asyncio
    async def test_user_version_is_4(self, tmp_db):
        await init_db()
        db = await get_db()
        async with db.execute("PRAGMA user_version") as cursor:
            row = await cursor.fetchone()
        assert row[0] == 4


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
            EpisodeUpdate(grade="bad", tags=[])
        with pytest.raises(ValidationError):
            EpisodeUpdate(grade="bad", tags=[], reason="   ")
        # Non-empty reason is fine
        EpisodeUpdate(grade="bad", tags=[], reason="too dark")

    def test_update_requires_reason_for_normal(self):
        from pydantic import ValidationError

        from backend.datasets.schemas import EpisodeUpdate
        with pytest.raises(ValidationError):
            EpisodeUpdate(grade="normal", tags=[])
        EpisodeUpdate(grade="normal", tags=[], reason="acceptable but slow")

    def test_update_good_does_not_require_reason(self):
        from backend.datasets.schemas import EpisodeUpdate
        u = EpisodeUpdate(grade="good", tags=[])
        assert u.reason is None

    def test_update_good_clears_reason_when_provided(self):
        # Reason supplied with grade=good should be allowed but ignored downstream;
        # at the schema level we accept it (service layer will null it out).
        from backend.datasets.schemas import EpisodeUpdate
        u = EpisodeUpdate(grade="good", tags=[], reason="ignored")
        assert u.grade == "good"

    def test_bulk_grade_requires_reason_for_bad(self):
        from pydantic import ValidationError

        from backend.datasets.schemas import BulkGradeRequest
        with pytest.raises(ValidationError):
            BulkGradeRequest(episode_indices=[0, 1], grade="bad")
        BulkGradeRequest(episode_indices=[0, 1], grade="bad", reason="bad batch")

    def test_bulk_grade_good_does_not_require_reason(self):
        from backend.datasets.schemas import BulkGradeRequest
        BulkGradeRequest(episode_indices=[0], grade="good")


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
async def loaded_service(tmp_db, tmp_path):
    """Create a fresh EpisodeService pointing at a mock dataset."""
    from backend.core.config import settings
    from backend.datasets.services.dataset_service import DatasetService
    from backend.datasets.services.episode_service import EpisodeService

    await init_db()

    ds_path = _create_mock_dataset(tmp_path)
    original_roots = settings.allowed_dataset_roots
    if str(ds_path.parent) not in original_roots:
        settings.allowed_dataset_roots = original_roots + [str(ds_path.parent)]

    # Replace module-level singletons
    import backend.datasets.services.dataset_service as ds_mod
    import backend.datasets.services.episode_service as ep_mod
    ds_mod.dataset_service = DatasetService()
    ds_mod.dataset_service.load_dataset(str(ds_path))
    ep_mod.dataset_service = ds_mod.dataset_service
    ep_mod.episode_service = EpisodeService()
    yield ep_mod.episode_service
    settings.allowed_dataset_roots = original_roots


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
    async def test_patch_with_reason_persists(self, loaded_service):
        from fastapi.testclient import TestClient

        from backend.main import app
        with TestClient(app) as client:
            r = client.patch(
                "/api/episodes/0",
                json={"grade": "bad", "tags": [], "reason": "lighting bad"},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["grade"] == "bad"
            assert body["reason"] == "lighting bad"

    @pytest.mark.asyncio
    async def test_patch_bad_without_reason_rejected(self, loaded_service):
        from fastapi.testclient import TestClient

        from backend.main import app
        with TestClient(app) as client:
            r = client.patch("/api/episodes/0", json={"grade": "bad", "tags": []})
            assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_patch_good_clears_reason(self, loaded_service):
        from fastapi.testclient import TestClient

        from backend.main import app
        with TestClient(app) as client:
            r = client.patch("/api/episodes/0", json={"grade": "bad", "tags": [], "reason": "x"})
            assert r.status_code == 200
            r = client.patch("/api/episodes/0", json={"grade": "good", "tags": []})
            assert r.status_code == 200
            assert r.json()["reason"] is None

    @pytest.mark.asyncio
    async def test_bulk_grade_with_reason(self, loaded_service):
        from fastapi.testclient import TestClient

        from backend.main import app
        with TestClient(app) as client:
            r = client.post(
                "/api/episodes/bulk-grade",
                json={"episode_indices": [0, 1], "grade": "bad", "reason": "batch fail"},
            )
            assert r.status_code == 200
            assert r.json()["updated"] == 2
            # Confirm via GET
            r = client.get("/api/episodes/0")
            assert r.json()["reason"] == "batch fail"

    @pytest.mark.asyncio
    async def test_bulk_grade_bad_without_reason_rejected(self, loaded_service):
        from fastapi.testclient import TestClient

        from backend.main import app
        with TestClient(app) as client:
            r = client.post(
                "/api/episodes/bulk-grade",
                json={"episode_indices": [0, 1], "grade": "bad"},
            )
            assert r.status_code == 422
