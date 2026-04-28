"""Regression tests for dataset-path-aware API isolation."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from backend.core.db import close_db, init_db, _reset
from backend.main import app


def _write_test_video(path: Path, duration_sec: float) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        path.write_bytes(b"fake mp4")
        return

    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=16x16:r=30",
            "-t",
            f"{max(duration_sec, 0.2):.6f}",
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(path),
        ],
        check=True,
    )


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    _reset()
    db_path = Path(tempfile.mkdtemp()) / "test.db"
    monkeypatch.setattr("backend.core.db._db_path_override", str(db_path))

    from backend.core.config import settings
    from backend.datasets.services import dataset_registry as registry_mod

    monkeypatch.setattr(settings, "allowed_dataset_roots", [str(tmp_path)])
    monkeypatch.setattr(registry_mod.settings, "allowed_dataset_roots", [str(tmp_path)])
    registry_mod.dataset_registry._items.clear()
    registry_mod.dataset_registry._key_to_path.clear()
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await close_db()
    _reset()
    registry_mod.dataset_registry._items.clear()
    registry_mod.dataset_registry._key_to_path.clear()


def _write_dataset(root: Path, name: str, *, length: int, scalar_base: float) -> Path:
    ds = root / name
    (ds / "meta" / "episodes" / "chunk-000").mkdir(parents=True, exist_ok=True)
    (ds / "data" / "chunk-000").mkdir(parents=True, exist_ok=True)
    (ds / "videos" / "observation.images.cam" / "chunk-000").mkdir(parents=True, exist_ok=True)
    _write_test_video(
        ds / "videos" / "observation.images.cam" / "chunk-000" / "file-000.mp4",
        length / 30,
    )

    (ds / "meta" / "info.json").write_text(json.dumps({
        "fps": 30,
        "total_episodes": 1,
        "total_tasks": 1,
        "robot_type": f"robot_{name}",
        "features": {
            "observation.state": {"dtype": "float32", "shape": [1]},
            "action": {"dtype": "float32", "shape": [1]},
            "observation.images.cam": {"dtype": "video"},
        },
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
            "videos/observation.images.cam/chunk_index": pa.array([0], type=pa.int64()),
            "videos/observation.images.cam/file_index": pa.array([0], type=pa.int64()),
            "videos/observation.images.cam/from_timestamp": pa.array([0.0], type=pa.float32()),
            "videos/observation.images.cam/to_timestamp": pa.array([length / 30], type=pa.float32()),
            "Serial_number": pa.array([f"{name}-serial"], type=pa.string()),
        }),
        ds / "meta" / "episodes" / "chunk-000" / "file-000.parquet",
    )
    pq.write_table(
        pa.table({
            "index": pa.array(list(range(length)), type=pa.int64()),
            "timestamp": pa.array([i / 30 for i in range(length)], type=pa.float32()),
            "observation.state": pa.array(
                [[scalar_base + i] for i in range(length)],
                type=pa.list_(pa.float32()),
            ),
            "action": pa.array(
                [[scalar_base + 100 + i] for i in range(length)],
                type=pa.list_(pa.float32()),
            ),
        }),
        ds / "data" / "chunk-000" / "file-000.parquet",
    )
    return ds


@pytest.mark.asyncio
async def test_episodes_remain_path_scoped_after_loading_another_dataset(client, tmp_path):
    cell005 = _write_dataset(tmp_path, "cell005_ds", length=2, scalar_base=10)
    cell002 = _write_dataset(tmp_path, "cell002_ds", length=4, scalar_base=20)

    await client.post("/api/datasets/load", json={"path": str(cell005)})
    await client.post("/api/datasets/load", json={"path": str(cell002)})

    res005 = await client.get("/api/episodes", params={"dataset_path": str(cell005)})
    res002 = await client.get("/api/episodes", params={"dataset_path": str(cell002)})

    assert res005.status_code == 200
    assert res002.status_code == 200
    assert res005.json()[0]["length"] == 2
    assert res002.json()[0]["length"] == 4


@pytest.mark.asyncio
async def test_scalars_are_path_scoped(client, tmp_path):
    cell005 = _write_dataset(tmp_path, "cell005_ds", length=2, scalar_base=10)
    cell002 = _write_dataset(tmp_path, "cell002_ds", length=4, scalar_base=20)

    await client.post("/api/datasets/load", json={"path": str(cell005)})
    await client.post("/api/datasets/load", json={"path": str(cell002)})

    res005 = await client.get("/api/scalars/0", params={"dataset_path": str(cell005)})
    res002 = await client.get("/api/scalars/0", params={"dataset_path": str(cell002)})

    assert res005.status_code == 200
    assert res002.status_code == 200
    assert res005.json()["num_frames"] == 2
    assert res002.json()["num_frames"] == 4
    assert res005.json()["observations"]["observation.state"][0] == 10.0
    assert res002.json()["observations"]["observation.state"][0] == 20.0


@pytest.mark.asyncio
async def test_bulk_grade_is_path_scoped(client, tmp_path):
    cell005 = _write_dataset(tmp_path, "cell005_ds", length=2, scalar_base=10)
    cell002 = _write_dataset(tmp_path, "cell002_ds", length=4, scalar_base=20)

    await client.post("/api/datasets/load", json={"path": str(cell005)})
    await client.post("/api/datasets/load", json={"path": str(cell002)})
    grade_res = await client.post("/api/episodes/bulk-grade", json={
        "dataset_path": str(cell005),
        "episode_indices": [0],
        "grade": "good",
    })

    assert grade_res.status_code == 200
    res005 = await client.get("/api/episodes", params={"dataset_path": str(cell005)})
    res002 = await client.get("/api/episodes", params={"dataset_path": str(cell002)})
    assert res005.json()[0]["grade"] == "good"
    assert res002.json()[0]["grade"] is None


@pytest.mark.asyncio
async def test_video_urls_and_cache_are_dataset_key_scoped(client, tmp_path):
    cell005 = _write_dataset(tmp_path, "cell005_ds", length=2, scalar_base=10)
    cell002 = _write_dataset(tmp_path, "cell002_ds", length=4, scalar_base=20)

    load005 = await client.post("/api/datasets/load", json={"path": str(cell005)})
    load002 = await client.post("/api/datasets/load", json={"path": str(cell002)})
    key005 = load005.json()["dataset_key"]
    key002 = load002.json()["dataset_key"]

    cams005 = await client.get(f"/api/datasets/{key005}/videos/0/cameras")
    cams002 = await client.get(f"/api/datasets/{key002}/videos/0/cameras")

    assert cams005.status_code == 200
    assert cams002.status_code == 200
    assert key005 in cams005.json()[0]["url"]
    assert key002 in cams002.json()[0]["url"]
    assert cams005.json()[0]["url"] != cams002.json()[0]["url"]
    assert "/videos/file/0/0/" in cams005.json()[0]["url"]

    stream = await client.get(cams005.json()[0]["url"])
    assert stream.status_code == 200
    assert stream.headers["cache-control"] == "private, max-age=604800, immutable"
