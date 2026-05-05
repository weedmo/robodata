"""Tests for Dataset Ops API router — mocks dataset_ops_service methods."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).parent.parent))

_frontend_assets = Path(__file__).parent.parent / "frontend" / "dist" / "assets"
_frontend_assets.mkdir(parents=True, exist_ok=True)

from backend.config import settings
from backend.main import app
from backend.services.dataset_ops_service import dataset_ops_service

_orig_roots = list(settings.allowed_dataset_roots)


@pytest.fixture(autouse=True)
def _allow_tmp_paths(tmp_path):
    """Allow tmp_path in dataset root validation for tests."""
    settings.allowed_dataset_roots = _orig_roots + [str(tmp_path), "/nonexistent"]
    yield
    settings.allowed_dataset_roots = _orig_roots


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# POST /api/datasets/split
# ---------------------------------------------------------------------------


class TestSplitDataset:
    @pytest.mark.asyncio
    async def test_split_returns_202_with_job(self, client, tmp_path):
        source = tmp_path / "source-ds"
        source.mkdir()
        source_path = f"{source.parent}/./{source.name}"

        with patch.object(
            dataset_ops_service,
            "split_dataset",
            new_callable=AsyncMock,
            return_value="abc-123",
        ) as split_dataset:
            resp = await client.post(
                "/api/datasets/split",
                json={
                    "source_path": source_path,
                    "episode_ids": [0, 1, 5],
                    "target_name": "my-split",
                },
            )

        assert resp.status_code == 202
        data = resp.json()
        assert data["job_id"] == "abc-123"
        assert data["operation"] == "split"
        assert data["status"] == "queued"
        split_dataset.assert_awaited_once_with(
            source_path=str(source.resolve()),
            episode_ids=[0, 1, 5],
            target_name="my-split",
            output_dir=None,
        )

    @pytest.mark.asyncio
    async def test_split_400_if_output_dir_outside_allowed_roots(self, client, tmp_path):
        source = tmp_path / "source-ds"
        source.mkdir()

        with patch.object(
            dataset_ops_service,
            "split_dataset",
            new_callable=AsyncMock,
        ) as split_dataset:
            resp = await client.post(
                "/api/datasets/split",
                json={
                    "source_path": str(source),
                    "episode_ids": [0],
                    "target_name": "new-split",
                    "output_dir": "/etc",
                },
            )

        assert resp.status_code == 400
        assert "allowed roots" in resp.json()["detail"]
        split_dataset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_split_404_if_source_missing(self, client):
        resp = await client.post(
            "/api/datasets/split",
            json={
                "source_path": "/nonexistent/path",
                "episode_ids": [0],
                "target_name": "new-split",
            },
        )
        assert resp.status_code == 404
        assert "Source path not found" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_split_422_if_episode_ids_empty(self, client, tmp_path):
        source = tmp_path / "source-ds"
        source.mkdir()

        resp = await client.post(
            "/api/datasets/split",
            json={
                "source_path": str(source),
                "episode_ids": [],
                "target_name": "new-split",
            },
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/datasets/split-into
# ---------------------------------------------------------------------------


class TestSplitIntoDataset:
    @pytest.mark.asyncio
    async def test_split_into_syncs_to_absolute_destination(self, client, tmp_path):
        source = tmp_path / "source-ds"
        source.mkdir()
        destination = tmp_path / "good-sync"

        with patch.object(
            dataset_ops_service,
            "sync_good_episodes",
            new_callable=AsyncMock,
            return_value="sync-job-1",
        ) as sync_good_episodes:
            resp = await client.post(
                "/api/datasets/split-into",
                json={
                    "source_path": str(source),
                    "episode_ids": [0, 1],
                    "destination_path": str(destination),
                },
            )

        assert resp.status_code == 202
        data = resp.json()
        assert data["job_id"] == "sync-job-1"
        assert data["operation"] == "sync_good_episodes"
        sync_good_episodes.assert_awaited_once_with(
            source_path=str(source.resolve()),
            episode_ids=[0, 1],
            destination_path=str(destination.resolve()),
        )

    @pytest.mark.asyncio
    async def test_split_into_rejects_relative_destination(self, client, tmp_path):
        source = tmp_path / "source-ds"
        source.mkdir()
        resp = await client.post(
            "/api/datasets/split-into",
            json={
                "source_path": str(source),
                "episode_ids": [0],
                "destination_path": "relative/path",
            },
        )

        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_split_into_404_source_missing(self, client):
        resp = await client.post(
            "/api/datasets/split-into",
            json={
                "source_path": "/nonexistent/source",
                "episode_ids": [0],
                "destination_path": "/nonexistent/target",
            },
        )
        assert resp.status_code == 404
        assert "Source path not found" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_split_into_rejects_self_merge(self, client, tmp_path):
        source = tmp_path / "source-ds"
        source.mkdir()

        resp = await client.post(
            "/api/datasets/split-into",
            json={
                "source_path": str(source),
                "episode_ids": [0],
                "destination_path": str(source),
            },
        )
        assert resp.status_code == 400
        assert "source and destination must differ" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_split_into_422_empty_episodes(self, client, tmp_path):
        source = tmp_path / "source-ds"
        source.mkdir()

        resp = await client.post(
            "/api/datasets/split-into",
            json={
                "source_path": str(source),
                "episode_ids": [],
                "destination_path": str(tmp_path / "good-sync"),
            },
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/datasets/merge
# ---------------------------------------------------------------------------


class TestMergeDatasets:
    @pytest.mark.asyncio
    async def test_merge_returns_202_with_job(self, client, tmp_path):
        src_a = tmp_path / "ds-a"
        src_b = tmp_path / "ds-b"
        src_a.mkdir()
        src_b.mkdir()
        source_paths = [f"{src_a.parent}/./{src_a.name}", f"{src_b.parent}/./{src_b.name}"]

        with patch.object(
            dataset_ops_service,
            "merge_datasets",
            new_callable=AsyncMock,
            return_value="merge-job-999",
        ) as merge_datasets:
            resp = await client.post(
                "/api/datasets/merge",
                json={
                    "source_paths": source_paths,
                    "target_name": "merged-out",
                },
            )

        assert resp.status_code == 202
        data = resp.json()
        assert data["job_id"] == "merge-job-999"
        assert data["operation"] == "merge"
        assert data["status"] == "queued"
        merge_datasets.assert_awaited_once_with(
            source_paths=[str(src_a.resolve()), str(src_b.resolve())],
            target_name="merged-out",
            output_dir=None,
        )

    @pytest.mark.asyncio
    async def test_merge_404_if_source_missing(self, client, tmp_path):
        existing = tmp_path / "ds-a"
        existing.mkdir()

        resp = await client.post(
            "/api/datasets/merge",
            json={
                "source_paths": [str(existing), "/nonexistent/ds-b"],
                "target_name": "merged-out",
            },
        )
        assert resp.status_code == 404
        assert "Source path not found" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# POST /api/datasets/delete
# ---------------------------------------------------------------------------


class TestDeleteEpisodes:
    @pytest.mark.asyncio
    async def test_delete_returns_202_with_canonical_paths(self, client, tmp_path):
        source = tmp_path / "source-ds"
        source.mkdir()
        source_path = f"{source.parent}/./{source.name}"

        with patch.object(
            dataset_ops_service,
            "delete_episodes",
            new_callable=AsyncMock,
            return_value="delete-job-123",
        ) as delete_episodes:
            resp = await client.post(
                "/api/datasets/delete",
                json={
                    "source_path": source_path,
                    "episode_ids": [2, 4],
                },
            )

        assert resp.status_code == 202
        assert resp.json() == {
            "job_id": "delete-job-123",
            "operation": "delete",
            "status": "queued",
        }
        delete_episodes.assert_awaited_once_with(
            source_path=str(source.resolve()),
            episode_ids=[2, 4],
            output_dir=None,
        )


# ---------------------------------------------------------------------------
# POST /api/datasets/stamp-cycles
# ---------------------------------------------------------------------------


class TestStampCycles:
    @pytest.mark.asyncio
    async def test_returns_202_with_job(self, client, tmp_path):
        source = tmp_path / "source-ds"
        source.mkdir()

        with patch.object(
            dataset_ops_service,
            "stamp_cycles",
            new_callable=AsyncMock,
            return_value="xyz-789",
        ) as stamp_cycles:
            resp = await client.post(
                "/api/datasets/stamp-cycles",
                json={
                    "source_path": str(source),
                    "overwrite": False,
                },
            )

        assert resp.status_code == 202
        assert resp.json() == {
            "job_id": "xyz-789",
            "operation": "stamp_cycles",
            "status": "queued",
        }
        stamp_cycles.assert_awaited_once_with(
            source_path=str(source.resolve()),
            overwrite=False,
        )

    @pytest.mark.asyncio
    async def test_404_if_source_missing(self, client):
        resp = await client.post(
            "/api/datasets/stamp-cycles",
            json={
                "source_path": "/nonexistent/missing-ds",
                "overwrite": False,
            },
        )

        assert resp.status_code == 404
        assert "Source path not found" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_rejects_path_outside_allowed_roots(self, client):
        resp = await client.post(
            "/api/datasets/stamp-cycles",
            json={
                "source_path": "/etc",
                "overwrite": False,
            },
        )

        assert resp.status_code == 400
        assert "allowed roots" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# GET /api/datasets/stamp-cycles/status
# ---------------------------------------------------------------------------


class TestStampCyclesStatus:
    @pytest.mark.asyncio
    async def test_status_returns_describe(self, client, tmp_path):
        source = tmp_path / "source-ds"
        source.mkdir()
        payload = {
            "stamped": True,
            "is_terminal_count_sample": 4,
            "unexpected": "ignored-by-response-model",
        }

        with patch(
            "backend.datasets.routers.dataset_ops.describe_stamp_state",
            return_value=payload,
        ) as describe:
            resp = await client.get(
                "/api/datasets/stamp-cycles/status",
                params={"path": str(source)},
            )

        assert resp.status_code == 200
        assert resp.json() == {
            "stamped": True,
            "is_terminal_count_sample": 4,
        }
        describe.assert_called_once_with(source)

    @pytest.mark.asyncio
    async def test_status_400_if_path_outside_allowed_roots(self, client):
        resp = await client.get(
            "/api/datasets/stamp-cycles/status",
            params={"path": "/etc"},
        )

        assert resp.status_code == 400
        assert "allowed roots" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_status_404_if_path_missing(self, client):
        resp = await client.get(
            "/api/datasets/stamp-cycles/status",
            params={"path": "/nonexistent/missing-ds"},
        )

        assert resp.status_code == 404
        assert "Source path not found" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# GET /api/datasets/ops/status/{job_id}
# ---------------------------------------------------------------------------


class TestGetJobStatus:
    @pytest.mark.asyncio
    async def test_returns_job_status(self, client):
        job = {
            "id": "job-abc",
            "operation": "sync_good_episodes",
            "status": "complete",
            "created_at": "2024-01-01T00:00:00+00:00",
            "completed_at": "2024-01-01T00:01:00+00:00",
            "error": None,
            "result_path": "/tmp/derived/my-split",
            "summary": {"mode": "merge", "created": 2, "skipped_duplicates": 1},
        }
        with patch.object(dataset_ops_service, "get_job_status", return_value=job):
            resp = await client.get("/api/datasets/ops/status/job-abc")

        assert resp.status_code == 200
        data = resp.json()
        assert data["job_id"] == "job-abc"
        assert data["operation"] == "sync_good_episodes"
        assert data["status"] == "complete"
        assert data["completed_at"] == "2024-01-01T00:01:00+00:00"
        assert data["result_path"] == "/tmp/derived/my-split"
        assert data["error"] is None
        assert data["summary"] == {"mode": "merge", "created": 2, "skipped_duplicates": 1}

    @pytest.mark.asyncio
    async def test_returns_queued_job(self, client):
        job = {
            "id": "job-xyz",
            "operation": "merge",
            "status": "queued",
            "created_at": "2024-01-01T00:00:00+00:00",
            "completed_at": None,
            "error": None,
            "result_path": None,
        }
        with patch.object(dataset_ops_service, "get_job_status", return_value=job):
            resp = await client.get("/api/datasets/ops/status/job-xyz")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "queued"
        assert data["completed_at"] is None
        assert data["result_path"] is None

    @pytest.mark.asyncio
    async def test_flattens_persistent_job_result_to_legacy_facade_schema(self, client):
        job = {
            "id": 42,
            "external_id": "public-job-42",
            "type": "sync_good_episodes",
            "status": "complete",
            "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
            "finished_at": datetime(2024, 1, 1, 0, 2, tzinfo=timezone.utc),
            "error": None,
            "result": {
                "result_path": "/tmp/derived/good-only",
                "summary": {"mode": "merge", "created": 2, "skipped_duplicates": 1},
                "internal_metric": "not-a-response-field",
            },
            "payload": {"source_path": "/tmp/source"},
        }
        with patch.object(dataset_ops_service, "get_job_status", return_value=job):
            resp = await client.get("/api/datasets/ops/status/public-job-42")

        assert resp.status_code == 200
        assert resp.json() == {
            "job_id": "public-job-42",
            "operation": "sync_good_episodes",
            "status": "complete",
            "created_at": "2024-01-01T00:00:00+00:00",
            "completed_at": "2024-01-01T00:02:00+00:00",
            "error": None,
            "result_path": "/tmp/derived/good-only",
            "summary": {"mode": "merge", "created": 2, "skipped_duplicates": 1},
        }

    @pytest.mark.asyncio
    async def test_returns_404_if_job_missing(self, client):
        with patch.object(dataset_ops_service, "get_job_status", return_value=None):
            resp = await client.get("/api/datasets/ops/status/nonexistent-job")

        assert resp.status_code == 404
        assert "Job not found" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# GET /api/datasets/browse-dirs
# ---------------------------------------------------------------------------


class TestBrowseDirs:
    @pytest.mark.asyncio
    async def test_lists_subdirectories_inside_allowed_root(self, client, tmp_path):
        # The autouse fixture adds tmp_path to allowed_dataset_roots.
        (tmp_path / "alpha").mkdir()
        (tmp_path / "beta").mkdir()
        (tmp_path / "gamma").mkdir()
        # LeRobot-looking dataset (meta/info.json present).
        (tmp_path / "alpha" / "meta").mkdir()
        (tmp_path / "alpha" / "meta" / "info.json").write_text("{}")
        # Hidden dirs must be skipped.
        (tmp_path / ".hidden").mkdir()

        resp = await client.get(f"/api/datasets/browse-dirs?path={tmp_path}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["path"] == str(tmp_path)
        names = [e["name"] for e in data["entries"]]
        assert names == ["alpha", "beta", "gamma"]
        assert data["entries"][0]["is_lerobot_dataset"] is True
        assert data["entries"][1]["is_lerobot_dataset"] is False

    @pytest.mark.asyncio
    async def test_rejects_path_outside_allowed_roots(self, client):
        resp = await client.get("/api/datasets/browse-dirs?path=/etc")
        assert resp.status_code == 400
        assert "outside allowed roots" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_parent_is_null_at_root(self, client, tmp_path):
        # tmp_path is itself an allowed root; browsing it should have no parent.
        resp = await client.get(f"/api/datasets/browse-dirs?path={tmp_path}")
        assert resp.status_code == 200
        assert resp.json()["parent"] is None

    @pytest.mark.asyncio
    async def test_parent_populated_for_subdir(self, client, tmp_path):
        (tmp_path / "child").mkdir()
        resp = await client.get(f"/api/datasets/browse-dirs?path={tmp_path / 'child'}")
        assert resp.status_code == 200
        assert resp.json()["parent"] == str(tmp_path)

    @pytest.mark.asyncio
    async def test_returns_404_for_missing_directory(self, client, tmp_path):
        resp = await client.get(f"/api/datasets/browse-dirs?path={tmp_path / 'nope'}")
        assert resp.status_code == 404


class TestDatasetSummary:
    @pytest.mark.asyncio
    async def test_returns_metadata_for_lerobot_dataset(self, client, tmp_path):
        import json

        ds = tmp_path / "ds01"
        (ds / "meta").mkdir(parents=True)
        (ds / "meta" / "info.json").write_text(
            json.dumps(
                {
                    "total_episodes": 42,
                    "robot_type": "panda",
                    "fps": 30,
                    "features": {"a": {}, "b": {}, "c": {}},
                }
            )
        )

        resp = await client.get(f"/api/datasets/summary?path={ds}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["path"] == str(ds)
        assert data["total_episodes"] == 42
        assert data["robot_type"] == "panda"
        assert data["fps"] == 30
        assert data["features_count"] == 3

    @pytest.mark.asyncio
    async def test_handles_missing_optional_fields(self, client, tmp_path):
        import json

        ds = tmp_path / "ds02"
        (ds / "meta").mkdir(parents=True)
        (ds / "meta" / "info.json").write_text(json.dumps({}))

        resp = await client.get(f"/api/datasets/summary?path={ds}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_episodes"] == 0
        assert data["robot_type"] is None
        assert data["fps"] == 0
        assert data["features_count"] == 0

    @pytest.mark.asyncio
    async def test_reads_info_with_trailing_nul_bytes(self, client, tmp_path):
        ds = tmp_path / "ds03"
        (ds / "meta").mkdir(parents=True)
        (ds / "meta" / "info.json").write_bytes(
            b'{"total_episodes": 7, "fps": 15, "features": {"cam": {}}}\x00\x00'
        )

        resp = await client.get(f"/api/datasets/summary?path={ds}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_episodes"] == 7
        assert data["fps"] == 15
        assert data["features_count"] == 1

    @pytest.mark.asyncio
    async def test_returns_422_for_malformed_numeric_metadata(self, client, tmp_path):
        import json

        ds = tmp_path / "ds04"
        (ds / "meta").mkdir(parents=True)
        (ds / "meta" / "info.json").write_text(
            json.dumps(
                {
                    "total_episodes": "forty-two",
                    "fps": 30,
                }
            )
        )

        resp = await client.get(f"/api/datasets/summary?path={ds}")
        assert resp.status_code == 422
        assert "Invalid total_episodes in info.json" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_returns_404_if_not_lerobot_dataset(self, client, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()

        resp = await client.get(f"/api/datasets/summary?path={plain}")
        assert resp.status_code == 404
        assert "Not a LeRobot dataset" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_returns_404_if_path_missing(self, client, tmp_path):
        resp = await client.get(f"/api/datasets/summary?path={tmp_path / 'nope'}")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_rejects_path_outside_allowed_roots(self, client):
        resp = await client.get("/api/datasets/summary?path=/etc")
        assert resp.status_code == 400
        assert "outside allowed roots" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_returns_500_if_info_read_fails(self, client, tmp_path):
        ds = tmp_path / "ds05"
        (ds / "meta").mkdir(parents=True)
        (ds / "meta" / "info.json").write_text("{}")

        with patch(
            "backend.datasets.routers.dataset_ops.read_info",
            side_effect=ValueError("bad info"),
        ):
            resp = await client.get(f"/api/datasets/summary?path={ds}")

        assert resp.status_code == 500
        assert "Failed to read info.json" in resp.json()["detail"]
