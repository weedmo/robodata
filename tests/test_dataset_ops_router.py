"""Tests for Dataset Ops API router — queues unified jobs."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).parent.parent))

_frontend_assets = Path(__file__).parent.parent / "frontend" / "dist" / "assets"
_frontend_assets.mkdir(parents=True, exist_ok=True)

from backend.config import settings
from backend.main import app
from backend.jobs import repo as jobs_repo

_orig_roots = list(settings.allowed_dataset_roots)


@pytest.fixture(autouse=True)
def _allow_tmp_paths(tmp_path):
    settings.allowed_dataset_roots = _orig_roots + [str(tmp_path), "/nonexistent"]
    yield
    settings.allowed_dataset_roots = _orig_roots


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


def _queued(external_id: str = "job-ext-1") -> dict:
    return {"id": 123, "external_id": external_id, "status": "queued"}


class TestDatasetJobEnqueue:
    @pytest.mark.asyncio
    async def test_split_enqueues_job_with_canonical_payload(self, client, tmp_path):
        source = tmp_path / "source-ds"
        source.mkdir()
        with patch.object(jobs_repo, "enqueue", new_callable=AsyncMock, return_value=_queued()) as enqueue:
            resp = await client.post(
                "/api/datasets/split",
                json={"source_path": f"{source.parent}/./{source.name}", "episode_ids": [0, 1], "target_name": "my-split"},
            )

        assert resp.status_code == 202
        assert resp.json() == {"job_id": "job-ext-1", "operation": "split", "status": "queued"}
        enqueue.assert_awaited_once_with(
            type_="split",
            payload={"source_path": str(source.resolve()), "episode_ids": [0, 1], "target_name": "my-split", "output_dir": None},
            dedupe_key=f"split:{source.resolve()}:my-split",
        )

    @pytest.mark.asyncio
    async def test_split_into_enqueues_sync_good_episodes(self, client, tmp_path):
        source = tmp_path / "source-ds"
        destination = tmp_path / "good-sync"
        source.mkdir()
        with patch.object(jobs_repo, "enqueue", new_callable=AsyncMock, return_value=_queued("sync-ext")) as enqueue:
            resp = await client.post(
                "/api/datasets/split-into",
                json={"source_path": str(source), "episode_ids": [0], "destination_path": str(destination)},
            )

        assert resp.status_code == 202
        assert resp.json()["job_id"] == "sync-ext"
        assert resp.json()["operation"] == "sync_good_episodes"
        enqueue.assert_awaited_once_with(
            type_="sync_good_episodes",
            payload={"source_path": str(source.resolve()), "episode_ids": [0], "destination_path": str(destination.resolve())},
            dedupe_key=f"sync_good_episodes:{source.resolve()}:{destination.resolve()}",
        )

    @pytest.mark.asyncio
    async def test_merge_enqueues_job(self, client, tmp_path):
        src_a = tmp_path / "a"
        src_b = tmp_path / "b"
        src_a.mkdir()
        src_b.mkdir()
        with patch.object(jobs_repo, "enqueue", new_callable=AsyncMock, return_value=_queued("merge-ext")) as enqueue:
            resp = await client.post(
                "/api/datasets/merge",
                json={"source_paths": [str(src_a), str(src_b)], "target_name": "merged"},
            )

        assert resp.status_code == 202
        assert resp.json()["operation"] == "merge"
        enqueue.assert_awaited_once_with(
            type_="merge",
            payload={"source_paths": [str(src_a.resolve()), str(src_b.resolve())], "target_name": "merged", "output_dir": None},
            dedupe_key=f"merge:{src_a.resolve()},{src_b.resolve()}:merged",
        )

    @pytest.mark.asyncio
    async def test_delete_enqueues_job(self, client, tmp_path):
        source = tmp_path / "source-ds"
        source.mkdir()
        with patch.object(jobs_repo, "enqueue", new_callable=AsyncMock, return_value=_queued("delete-ext")) as enqueue:
            resp = await client.post(
                "/api/datasets/delete",
                json={"source_path": str(source), "episode_ids": [2, 4]},
            )

        assert resp.status_code == 202
        assert resp.json()["operation"] == "delete"
        enqueue.assert_awaited_once_with(
            type_="delete",
            payload={"source_path": str(source.resolve()), "episode_ids": [2, 4], "output_dir": None},
            dedupe_key=f"delete:{source.resolve()}:2,4",
        )

    @pytest.mark.asyncio
    async def test_stamp_cycles_enqueues_job(self, client, tmp_path):
        source = tmp_path / "source-ds"
        source.mkdir()
        with patch.object(jobs_repo, "enqueue", new_callable=AsyncMock, return_value=_queued("stamp-ext")) as enqueue:
            resp = await client.post(
                "/api/datasets/stamp-cycles",
                json={"source_path": str(source), "overwrite": False},
            )

        assert resp.status_code == 202
        assert resp.json() == {"job_id": "stamp-ext", "operation": "stamp_cycles", "status": "queued"}
        enqueue.assert_awaited_once_with(
            type_="stamp_cycles",
            payload={"source_path": str(source.resolve()), "overwrite": False},
            dedupe_key=f"stamp_cycles:{source.resolve()}",
        )

    @pytest.mark.asyncio
    async def test_duplicate_dedupe_returns_409(self, client, tmp_path):
        source = tmp_path / "source-ds"
        source.mkdir()
        with patch.object(jobs_repo, "enqueue", new_callable=AsyncMock, side_effect=jobs_repo.DuplicateDedupe(99)):
            resp = await client.post(
                "/api/datasets/stamp-cycles",
                json={"source_path": str(source), "overwrite": False},
            )
        assert resp.status_code == 409
        assert resp.json() == {"error": "duplicate_dedupe_key", "existing_job_id": 99}


class TestDatasetJobStatus:
    @pytest.mark.asyncio
    async def test_get_status_flattens_persistent_job_result(self, client):
        job = {
            "id": 10,
            "external_id": "ext-10",
            "type": "sync_good_episodes",
            "status": "complete",
            "created_at": "2024-01-01T00:00:00+00:00",
            "finished_at": "2024-01-01T00:01:00+00:00",
            "error": None,
            "result": {"result_path": "/tmp/out", "summary": {"mode": "merge", "created": 2, "skipped_duplicates": 1}},
        }
        with patch.object(jobs_repo, "fetch_by_external_id", new_callable=AsyncMock, return_value=job):
            resp = await client.get("/api/datasets/ops/status/ext-10")

        assert resp.status_code == 200
        assert resp.json()["job_id"] == "ext-10"
        assert resp.json()["operation"] == "sync_good_episodes"
        assert resp.json()["result_path"] == "/tmp/out"
        assert resp.json()["summary"] == {"mode": "merge", "created": 2, "skipped_duplicates": 1}

    @pytest.mark.asyncio
    async def test_get_status_404_if_missing(self, client):
        with patch.object(jobs_repo, "fetch_by_external_id", new_callable=AsyncMock, return_value=None):
            resp = await client.get("/api/datasets/ops/status/missing")
        assert resp.status_code == 404
