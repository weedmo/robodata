"""Tests for episode annotations stored in SQLite DB instead of JSON sidecar."""

import json
import tempfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import pytest_asyncio

from backend.core.db import get_db, init_db, close_db, _reset


@pytest_asyncio.fixture(autouse=True)
async def tmp_db(monkeypatch):
    """Point DB to a temp file for each test."""
    _reset()  # clear any stale connection from prior tests
    tmp = Path(tempfile.mkdtemp()) / "test.db"
    monkeypatch.setattr("backend.core.db._db_path_override", str(tmp))
    await init_db()
    yield tmp
    await close_db()
    _reset()
    tmp.unlink(missing_ok=True)


def _create_mock_dataset(root: Path) -> Path:
    """Create a minimal LeRobot v3.0 dataset under *root* and return its path."""
    ds = root / "mock_ds"
    (ds / "meta" / "episodes" / "chunk-000").mkdir(parents=True, exist_ok=True)
    (ds / "data" / "chunk-000").mkdir(parents=True, exist_ok=True)

    # info.json
    info = {
        "fps": 30,
        "total_episodes": 3,
        "total_tasks": 1,
        "robot_type": "test_robot",
        "features": {},
    }
    (ds / "meta" / "info.json").write_text(json.dumps(info))

    # tasks.parquet
    tasks_table = pa.table({
        "task_index": pa.array([0], type=pa.int64()),
        "task": pa.array(["test task"], type=pa.string()),
    })
    pq.write_table(tasks_table, ds / "meta" / "tasks.parquet")

    # episodes parquet
    ep_table = pa.table({
        "episode_index": pa.array([0, 1, 2], type=pa.int64()),
        "task_index": pa.array([0, 0, 0], type=pa.int64()),
        "data/chunk_index": pa.array([0, 0, 0], type=pa.int64()),
        "data/file_index": pa.array([0, 0, 0], type=pa.int64()),
        "dataset_from_index": pa.array([0, 100, 200], type=pa.int64()),
        "dataset_to_index": pa.array([100, 200, 300], type=pa.int64()),
        "Serial_number": pa.array(
            [
                "MOCK_20260101_000000_000000",
                "MOCK_20260101_000001_000000",
                "MOCK_20260101_000002_000000",
            ],
            type=pa.string(),
        ),
    })
    pq.write_table(ep_table, ds / "meta" / "episodes" / "chunk-000" / "file-000.parquet")

    # data parquet (minimal)
    data_table = pa.table({
        "episode_index": pa.array([0, 1, 2], type=pa.int64()),
        "timestamp": pa.array([0.0, 0.0, 0.0], type=pa.float32()),
    })
    pq.write_table(data_table, ds / "data" / "chunk-000" / "file-000.parquet")

    return ds


def _create_duplicate_serial_dataset(root: Path) -> Path:
    ds = _create_mock_dataset(root)
    ep_table = pa.table({
        "episode_index": pa.array([0, 1, 2], type=pa.int64()),
        "task_index": pa.array([0, 0, 0], type=pa.int64()),
        "data/chunk_index": pa.array([0, 0, 0], type=pa.int64()),
        "data/file_index": pa.array([0, 0, 0], type=pa.int64()),
        "dataset_from_index": pa.array([0, 100, 200], type=pa.int64()),
        "dataset_to_index": pa.array([100, 200, 300], type=pa.int64()),
        "Serial_number": pa.array(["S-A", "S-DUP", "S-DUP"], type=pa.string()),
    })
    pq.write_table(
        ep_table,
        ds / "meta" / "episodes" / "chunk-000" / "file-000.parquet",
    )
    return ds


def _make_services(dataset_path: Path):
    """Create fresh DatasetContext + EpisodeService pointing at dataset_path."""
    from backend.datasets.services.episode_service import EpisodeService

    return _make_ctx(dataset_path), EpisodeService()


def _make_ctx(dataset_path: Path):
    from backend.core.config import settings
    import backend.datasets.services.dataset_registry as registry_mod
    from backend.datasets.services.dataset_registry import DatasetRegistry

    dataset_root = str(dataset_path.parent)
    for settings_obj in (settings, registry_mod.settings):
        if dataset_root not in settings_obj.allowed_dataset_roots:
            settings_obj.allowed_dataset_roots = settings_obj.allowed_dataset_roots + [dataset_root]
    return DatasetRegistry(max_size=4).get(dataset_path)


@pytest.fixture
def mock_dataset(tmp_path):
    return _create_mock_dataset(tmp_path)


@pytest.fixture
def services(mock_dataset):
    return _make_services(mock_dataset)


class TestUpdateEpisode:
    @pytest.mark.asyncio
    async def test_writes_grade_and_tags_to_db(self, tmp_db, services):
        await init_db()
        ctx, es = services

        result = await es.update_episode(ctx, episode_index=0, grade="good", tags=["clean"])
        assert result["grade"] == "good"
        assert result["tags"] == ["clean"]

        # Verify directly in DB
        db = await get_db()
        async with db.execute(
            """SELECT a.grade, a.tags
               FROM annotations a
               JOIN episode_serials es ON es.serial_number = a.serial_number
               WHERE es.episode_index = 0"""
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "good"
        assert json.loads(row[1]) == ["clean"]

    @pytest.mark.asyncio
    async def test_updates_dataset_stats(self, tmp_db, services):
        await init_db()
        ctx, es = services

        await es.update_episode(ctx, episode_index=0, grade="good", tags=[])
        await es.update_episode(ctx, episode_index=1, grade="bad", tags=[])

        db = await get_db()
        dataset_id_path = str(ctx.dataset_path.resolve())
        async with db.execute("SELECT id FROM datasets WHERE path = ?", (dataset_id_path,)) as cursor:
            row = await cursor.fetchone()
        dataset_id = row[0]

        async with db.execute(
            "SELECT graded_count, good_count, bad_count FROM dataset_stats WHERE dataset_id = ?",
            (dataset_id,),
        ) as cursor:
            stats = await cursor.fetchone()
        assert stats[0] == 2  # graded_count
        assert stats[1] == 1  # good_count
        assert stats[2] == 1  # bad_count

    @pytest.mark.asyncio
    async def test_update_keeps_episode_parquet_byte_unchanged(
        self, tmp_db, services, monkeypatch,
    ):
        await init_db()
        ctx, es = services
        parquet_path = ctx.episode_parquet_files[0]
        before_bytes = parquet_path.read_bytes()
        before_mtime_ns = parquet_path.stat().st_mtime_ns

        def fail_write_back(*_args, **_kwargs):
            pytest.fail("DB-authoritative annotation updates must not write parquet")

        monkeypatch.setattr(
            "backend.datasets.services.episode_service.pq.write_table",
            fail_write_back,
        )

        result = await es.update_episode(ctx, episode_index=0, grade="good", tags=["clean"])

        assert result["grade"] == "good"
        assert result["tags"] == ["clean"]
        assert parquet_path.read_bytes() == before_bytes
        assert parquet_path.stat().st_mtime_ns == before_mtime_ns

        db = await get_db()
        async with db.execute(
            """SELECT a.grade, a.tags
               FROM annotations a
               JOIN episode_serials es ON es.serial_number = a.serial_number
               WHERE es.episode_index = 0"""
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "good"
        assert json.loads(row[1]) == ["clean"]


class TestBulkGrade:
    @pytest.mark.asyncio
    async def test_writes_to_db(self, tmp_db, services):
        await init_db()
        ctx, es = services

        # First set tags on episode 0
        await es.update_episode(ctx, episode_index=0, grade=None, tags=["important"])

        # Bulk grade episodes 0 and 1
        count = await es.bulk_grade(ctx, episode_indices=[0, 1], grade="good")
        assert count == 2

        db = await get_db()
        async with db.execute(
            """SELECT es.episode_index, a.grade, a.tags
               FROM annotations a
               JOIN episode_serials es ON es.serial_number = a.serial_number
               ORDER BY es.episode_index"""
        ) as cursor:
            rows = await cursor.fetchall()

        results = {r[0]: {"grade": r[1], "tags": json.loads(r[2]) if r[2] else []} for r in rows}
        assert results[0]["grade"] == "good"
        assert results[0]["tags"] == ["important"]  # tags preserved
        assert results[1]["grade"] == "good"

    @pytest.mark.asyncio
    async def test_bulk_grade_keeps_episode_parquet_byte_unchanged(
        self, tmp_db, services, monkeypatch,
    ):
        await init_db()
        ctx, es = services
        parquet_path = ctx.episode_parquet_files[0]
        before_bytes = parquet_path.read_bytes()
        before_mtime_ns = parquet_path.stat().st_mtime_ns

        def fail_write_back(*_args, **_kwargs):
            pytest.fail("DB-authoritative bulk grading must not write parquet")

        monkeypatch.setattr(
            "backend.datasets.services.episode_service.pq.write_table",
            fail_write_back,
        )

        count = await es.bulk_grade(ctx, episode_indices=[0, 1], grade="good")

        assert count == 2
        assert parquet_path.read_bytes() == before_bytes
        assert parquet_path.stat().st_mtime_ns == before_mtime_ns

    @pytest.mark.asyncio
    async def test_duplicate_serial_episode_falls_back_to_parquet_serial(
        self, tmp_db, tmp_path, monkeypatch,
    ):
        await init_db()
        ds_path = _create_duplicate_serial_dataset(tmp_path)
        ctx, es = _make_services(ds_path)

        await es.get_episodes(ctx)
        assert ctx.episodes_cache is not None

        count = await es.bulk_grade(ctx, [1], "bad", reason="split recording")
        assert count == 1
        assert ctx.episodes_cache is None

        episodes = await es.get_episodes(ctx)
        by_idx = {ep["episode_index"]: ep for ep in episodes}
        assert by_idx[1]["grade"] == "bad"
        assert by_idx[1]["reason"] == "split recording"
        assert by_idx[2]["grade"] == "bad"
        assert by_idx[2]["reason"] == "split recording"


class TestGetEpisodes:
    @pytest.mark.asyncio
    async def test_reads_annotations_from_db(self, tmp_db, services):
        await init_db()
        ctx, es = services

        # Write annotation to DB
        await es.update_episode(ctx, episode_index=1, grade="normal", tags=["review"])

        # Clear cache to force re-read from DB
        ctx.episodes_cache = None

        episodes = await es.get_episodes(ctx)
        ep1 = next(e for e in episodes if e["episode_index"] == 1)
        assert ep1["grade"] == "normal"
        assert ep1["tags"] == ["review"]

        # Unannotated episodes should have no grade
        ep0 = next(e for e in episodes if e["episode_index"] == 0)
        assert ep0["grade"] is None


class TestSidecarMigration:
    @pytest.mark.asyncio
    async def test_migrates_sidecar_to_db(self, tmp_db, mock_dataset, monkeypatch):
        await init_db()

        # Create a legacy sidecar JSON file
        from backend.datasets.services.episode_service import _sidecar_file
        sidecar_path = _sidecar_file(mock_dataset)
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        sidecar_data = {
            "0": {"grade": "good", "tags": ["migrated"]},
            "2": {"grade": "bad", "tags": ["noisy", "collision"]},
        }
        sidecar_path.write_text(json.dumps(sidecar_data))

        # Create services and load
        ctx, es = _make_services(mock_dataset)

        episodes = await es.get_episodes(ctx)

        # Verify annotations came through
        ep0 = next(e for e in episodes if e["episode_index"] == 0)
        assert ep0["grade"] == "good"
        assert ep0["tags"] == ["migrated"]

        ep2 = next(e for e in episodes if e["episode_index"] == 2)
        assert ep2["grade"] == "bad"
        assert "noisy" in ep2["tags"]
        assert "collision" in ep2["tags"]

        # Verify data is in the DB
        db = await get_db()
        async with db.execute("SELECT COUNT(*) FROM annotations") as cursor:
            row = await cursor.fetchone()
        assert row[0] == 2

        # Clean up sidecar
        sidecar_path.unlink(missing_ok=True)


class TestEpisodeServiceContextIsolation:
    @pytest.mark.asyncio
    async def test_get_episode_uses_passed_context(self, tmp_path):
        from backend.datasets.services.episode_service import EpisodeService

        ds = _create_mock_dataset(tmp_path)
        ctx = _make_ctx(ds)
        ep = await EpisodeService().get_episode(ctx, episode_index=0)
        assert ep["episode_index"] == 0

    @pytest.mark.asyncio
    async def test_two_contexts_do_not_share_episode_cache(self, tmp_path):
        from backend.datasets.services.episode_service import EpisodeService

        ds_a = _create_mock_dataset(tmp_path / "a")
        ds_b = _create_mock_dataset(tmp_path / "b")
        pq.write_table(
            pa.table({
                "episode_index": pa.array([0, 1, 2], type=pa.int64()),
                "task_index": pa.array([0, 0, 0], type=pa.int64()),
                "data/chunk_index": pa.array([0, 0, 0], type=pa.int64()),
                "data/file_index": pa.array([0, 0, 0], type=pa.int64()),
                "dataset_from_index": pa.array([0, 100, 200], type=pa.int64()),
                "dataset_to_index": pa.array([100, 200, 300], type=pa.int64()),
                "Serial_number": pa.array(["B-0", "B-1", "B-2"], type=pa.string()),
            }),
            ds_b / "meta" / "episodes" / "chunk-000" / "file-000.parquet",
        )
        ctx_a = _make_ctx(ds_a)
        ctx_b = _make_ctx(ds_b)
        service = EpisodeService()

        await service.update_episode(ctx_a, episode_index=0, grade="good", tags=[])
        ep_a = await service.get_episode(ctx_a, episode_index=0)
        ep_b = await service.get_episode(ctx_b, episode_index=0)

        assert ep_a["grade"] == "good"
        assert ep_b.get("grade") in (None, "")
