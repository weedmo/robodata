"""API-level regression tests for dataset cache refresh.

Covers the contract that ``/api/datasets/load`` and ``/api/datasets/info``:
- automatically return refreshed counts after the underlying dataset
  metadata changes at the same path, without restart, and
- return HTTP 409 when the stable-load barrier rejects a context loaded
  from mixed old/new metadata.

These tests mount the datasets router into a minimal FastAPI app so we
do not require the full ``backend.main.app`` startup (DB, etc.).
"""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


def _make_min_dataset(root: Path, name: str, *, length: int = 10) -> Path:
    ds = root / name
    (ds / "meta" / "episodes" / "chunk-000").mkdir(parents=True, exist_ok=True)
    (ds / "data" / "chunk-000").mkdir(parents=True, exist_ok=True)
    (ds / "meta" / "info.json").write_text(json.dumps({
        "fps": 30,
        "total_episodes": 1,
        "total_tasks": 1,
        "robot_type": "test_robot",
        "features": {},
    }))
    pq.write_table(
        pa.table({
            "task_index": pa.array([0], type=pa.int64()),
            "task": pa.array([f"task {name}"], type=pa.string()),
        }),
        ds / "meta" / "tasks.parquet",
    )
    pq.write_table(
        pa.table({
            "episode_index": pa.array([0], type=pa.int64()),
            "length": pa.array([length], type=pa.int64()),
            "task_index": pa.array([0], type=pa.int64()),
            "data/chunk_index": pa.array([0], type=pa.int64()),
            "data/file_index": pa.array([0], type=pa.int64()),
            "dataset_from_index": pa.array([0], type=pa.int64()),
            "dataset_to_index": pa.array([length], type=pa.int64()),
            "Serial_number": pa.array([f"{name}-serial"], type=pa.string()),
        }),
        ds / "meta" / "episodes" / "chunk-000" / "file-000.parquet",
    )
    return ds


def _rewrite_to_two_episodes(ds: Path, name: str) -> None:
    (ds / "meta" / "info.json").write_text(json.dumps({
        "fps": 30,
        "total_episodes": 2,
        "total_tasks": 1,
        "robot_type": "test_robot",
        "features": {},
    }))
    pq.write_table(
        pa.table({
            "episode_index": pa.array([0, 1], type=pa.int64()),
            "length": pa.array([10, 11], type=pa.int64()),
            "task_index": pa.array([0, 0], type=pa.int64()),
            "data/chunk_index": pa.array([0, 0], type=pa.int64()),
            "data/file_index": pa.array([0, 0], type=pa.int64()),
            "dataset_from_index": pa.array([0, 10], type=pa.int64()),
            "dataset_to_index": pa.array([10, 21], type=pa.int64()),
            "Serial_number": pa.array([f"{name}-a", f"{name}-b"], type=pa.string()),
        }),
        ds / "meta" / "episodes" / "chunk-000" / "file-000.parquet",
    )


def _bump_mtime(path: Path) -> None:
    import os
    st = path.stat()
    new_ts = st.st_mtime + 2.0
    os.utime(path, (new_ts, new_ts))


@pytest.fixture
def isolated_registry_app(tmp_path, monkeypatch):
    """A minimal FastAPI app that mounts only the datasets router and an
    isolated DatasetRegistry pointing at ``tmp_path``.
    """
    from backend.core.config import settings
    from backend.datasets.routers import datasets as datasets_router_mod
    from backend.datasets.routers import episodes as episodes_router_mod
    from backend.datasets.services import dataset_registry as registry_mod

    monkeypatch.setattr(settings, "allowed_dataset_roots", [str(tmp_path)])

    fresh_registry = registry_mod.DatasetRegistry(max_size=8)
    monkeypatch.setattr(datasets_router_mod, "dataset_registry", fresh_registry)
    monkeypatch.setattr(episodes_router_mod, "dataset_registry", fresh_registry)

    app = FastAPI()
    app.include_router(datasets_router_mod.router)
    app.include_router(episodes_router_mod.router)
    return app, fresh_registry


@pytest_asyncio.fixture
async def client(isolated_registry_app):
    app, _ = isolated_registry_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_load_endpoint_refreshes_after_metadata_change(client, tmp_path):
    ds = _make_min_dataset(tmp_path, "cellA", length=10)

    first = await client.post("/api/datasets/load", json={"path": str(ds)})
    assert first.status_code == 200, first.text
    assert first.json()["total_episodes"] == 1

    _rewrite_to_two_episodes(ds, "cellA")
    _bump_mtime(ds / "meta" / "info.json")
    _bump_mtime(ds / "meta" / "episodes" / "chunk-000" / "file-000.parquet")

    second = await client.post("/api/datasets/load", json={"path": str(ds)})
    assert second.status_code == 200, second.text
    assert second.json()["total_episodes"] == 2


@pytest.mark.asyncio
async def test_info_endpoint_refreshes_after_metadata_change(client, tmp_path):
    ds = _make_min_dataset(tmp_path, "cellB", length=10)

    first = await client.get("/api/datasets/info", params={"dataset_path": str(ds)})
    assert first.status_code == 200, first.text
    assert first.json()["total_episodes"] == 1

    _rewrite_to_two_episodes(ds, "cellB")
    _bump_mtime(ds / "meta" / "info.json")
    _bump_mtime(ds / "meta" / "episodes" / "chunk-000" / "file-000.parquet")

    second = await client.get("/api/datasets/info", params={"dataset_path": str(ds)})
    assert second.status_code == 200, second.text
    assert second.json()["total_episodes"] == 2


@pytest.mark.asyncio
async def test_load_endpoint_returns_409_on_unstable_metadata(client, tmp_path, monkeypatch):
    """If the stable-load barrier raises ``RuntimeError`` because the
    fingerprint keeps changing across both attempts, the API must surface
    HTTP 409 rather than a generic 500.
    """
    from backend.datasets.services import dataset_registry as registry_mod

    ds = _make_min_dataset(tmp_path, "cellC", length=10)

    real_fingerprint = registry_mod._fingerprint_dataset
    counter = {"n": 0}

    def always_flaky(root: Path):
        counter["n"] += 1
        fp = real_fingerprint(root)
        return tuple(list(fp) + [(f"__synthetic_{counter['n']}__", 0, 0)])

    monkeypatch.setattr(registry_mod, "_fingerprint_dataset", always_flaky)

    resp = await client.post("/api/datasets/load", json={"path": str(ds)})
    assert resp.status_code == 409, resp.text
    assert "Dataset metadata changed while loading" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_info_endpoint_returns_409_on_unstable_metadata(client, tmp_path, monkeypatch):
    from backend.datasets.services import dataset_registry as registry_mod

    ds = _make_min_dataset(tmp_path, "cellD", length=10)

    real_fingerprint = registry_mod._fingerprint_dataset
    counter = {"n": 0}

    def always_flaky(root: Path):
        counter["n"] += 1
        fp = real_fingerprint(root)
        return tuple(list(fp) + [(f"__synthetic_{counter['n']}__", 0, 0)])

    monkeypatch.setattr(registry_mod, "_fingerprint_dataset", always_flaky)

    resp = await client.get("/api/datasets/info", params={"dataset_path": str(ds)})
    assert resp.status_code == 409, resp.text
    assert "Dataset metadata changed while loading" in resp.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint",
    ["load", "info", "episodes", "update-episode", "bulk-grade"],
)
async def test_dataset_endpoint_rejects_missing_episode_metadata(
    client, tmp_path, endpoint,
):
    ds = _make_min_dataset(tmp_path, f"missing-{endpoint}", length=10)
    (ds / "meta" / "episodes" / "chunk-000" / "file-000.parquet").unlink()

    if endpoint == "load":
        resp = await client.post("/api/datasets/load", json={"path": str(ds)})
    elif endpoint == "info":
        resp = await client.get(
            "/api/datasets/info",
            params={"dataset_path": str(ds)},
        )
    elif endpoint == "episodes":
        resp = await client.get(
            "/api/episodes",
            params={"dataset_path": str(ds)},
        )
    elif endpoint == "update-episode":
        resp = await client.patch(
            "/api/episodes/0",
            json={"dataset_path": str(ds), "grade": "good"},
        )
    else:
        resp = await client.post(
            "/api/episodes/bulk-grade",
            json={
                "dataset_path": str(ds),
                "episode_indices": [0],
                "grade": "good",
            },
        )

    assert resp.status_code == 409, resp.text
    assert "episode metadata" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_load_endpoint_still_404_on_missing_path(client, tmp_path):
    resp = await client.post(
        "/api/datasets/load",
        json={"path": str(tmp_path / "does_not_exist")},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_info_endpoint_still_400_on_disallowed_path(client, tmp_path, monkeypatch):
    from backend.core.config import settings

    monkeypatch.setattr(settings, "allowed_dataset_roots", [str(tmp_path / "allowed")])
    resp = await client.get(
        "/api/datasets/info",
        params={"dataset_path": str(tmp_path)},
    )
    assert resp.status_code == 400
