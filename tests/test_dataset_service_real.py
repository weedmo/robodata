"""Tests for DatasetContext against real LeRobot v3.0 datasets at /tmp/hf-mounts/Phy-lab/dataset."""

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from backend.core.config import settings
from backend.datasets.services.dataset_registry import DatasetRegistry


def _load_ctx(dataset_path: Path):
    root = str(dataset_path.parent)
    if root not in settings.allowed_dataset_roots:
        settings.allowed_dataset_roots = settings.allowed_dataset_roots + [root]
    return DatasetRegistry(max_size=4).get(dataset_path)


def _write_temp_dataset_info(root: Path, total_episodes: int) -> None:
    meta_dir = root / "meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / "info.json").write_text(
        json.dumps(
            {
                "codebase_version": "v3.0",
                "robot_type": "test_robot",
                "total_episodes": total_episodes,
                "total_tasks": 0,
                "fps": 30,
                "features": {},
            }
        ),
        encoding="utf-8",
    )


def _write_episode_chunk(root: Path, chunk_index: int, file_index: int, serial_type: pa.DataType) -> None:
    chunk_dir = root / "meta" / "episodes" / f"chunk-{chunk_index:03d}"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    start = chunk_index * 2
    table = pa.table(
        {
            "episode_index": pa.array([start, start + 1], type=pa.int64()),
            "task_index": pa.array([0, 0], type=pa.int64()),
            "length": pa.array([10, 11], type=pa.int64()),
            "data/chunk_index": pa.array([0, 0], type=pa.int64()),
            "data/file_index": pa.array([0, 0], type=pa.int64()),
            "dataset_from_index": pa.array([start * 10, (start + 1) * 10], type=pa.int64()),
            "dataset_to_index": pa.array([(start + 1) * 10, (start + 2) * 10], type=pa.int64()),
            "Serial_number": pa.array(
                [f"serial-{start}", f"serial-{start + 1}"],
                type=serial_type,
            ),
        }
    )
    pq.write_table(table, chunk_dir / f"file-{file_index:03d}.parquet")


# ---------------------------------------------------------------------------
# load_dataset
# ---------------------------------------------------------------------------

class TestLoadDataset:
    def test_loads_basic_aic_without_error(self, basic_aic_path):
        ds = _load_ctx(basic_aic_path)
        assert ds.dataset_path == basic_aic_path.resolve()

    def test_loads_hojun_without_error(self, hojun_path):
        ds = _load_ctx(hojun_path)
        assert ds.dataset_path == hojun_path.resolve()

    def test_rejects_path_outside_allowed_roots(self, tmp_path):
        with pytest.raises(ValueError, match="not under any allowed root"):
            DatasetRegistry(max_size=1).get(tmp_path)

    def test_rejects_nonexistent_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "allowed_dataset_roots", settings.allowed_dataset_roots + [str(tmp_path)])

        with pytest.raises(FileNotFoundError):
            DatasetRegistry(max_size=1).get(tmp_path / "nonexistent")

    def test_get_by_key_unknown_raises(self):
        with pytest.raises(KeyError, match="Unknown dataset_key"):
            DatasetRegistry(max_size=1).get_by_key("missing")


# ---------------------------------------------------------------------------
# get_info
# ---------------------------------------------------------------------------

class TestLoadDatasetRobustness:
    def test_loads_json_with_trailing_null_bytes(self, hojun_path):
        """hojun info.json has trailing null bytes — load should handle gracefully."""
        ds = _load_ctx(hojun_path)
        info = ds.get_info()
        assert info["robot_type"] == "ur5e"

    def test_loads_episode_parquet_chunks_with_mixed_string_width(self, tmp_path, monkeypatch):
        dataset_path = tmp_path / "mixed_string_width"
        _write_temp_dataset_info(dataset_path, total_episodes=4)
        _write_episode_chunk(dataset_path, chunk_index=0, file_index=0, serial_type=pa.string())
        _write_episode_chunk(dataset_path, chunk_index=1, file_index=1, serial_type=pa.large_string())
        monkeypatch.setattr(settings, "allowed_dataset_roots", settings.allowed_dataset_roots + [str(tmp_path)])

        ds = _load_ctx(dataset_path)

        episodes = ds.get_episodes()
        assert len(episodes) == 4
        assert [episode["episode_index"] for episode in episodes] == [0, 1, 2, 3]
        assert [episode["Serial_number"] for episode in episodes] == [
            "serial-0",
            "serial-1",
            "serial-2",
            "serial-3",
        ]

    def test_still_fails_for_incompatible_episode_field_types(self, tmp_path, monkeypatch):
        dataset_path = tmp_path / "incompatible_schema"
        _write_temp_dataset_info(dataset_path, total_episodes=4)
        _write_episode_chunk(dataset_path, chunk_index=0, file_index=0, serial_type=pa.string())
        chunk_dir = dataset_path / "meta" / "episodes" / "chunk-001"
        chunk_dir.mkdir(parents=True, exist_ok=True)
        incompatible_table = pa.table(
            {
                "episode_index": pa.array([2, 3], type=pa.int64()),
                "task_index": pa.array([0, 0], type=pa.int64()),
                "length": pa.array([12, 13], type=pa.int64()),
                "data/chunk_index": pa.array([0, 0], type=pa.int64()),
                "data/file_index": pa.array([0, 0], type=pa.int64()),
                "dataset_from_index": pa.array([20, 30], type=pa.int64()),
                "dataset_to_index": pa.array([30, 40], type=pa.int64()),
                "Serial_number": pa.array([200, 300], type=pa.int64()),
            }
        )
        pq.write_table(incompatible_table, chunk_dir / "file-001.parquet")
        monkeypatch.setattr(settings, "allowed_dataset_roots", settings.allowed_dataset_roots + [str(tmp_path)])

        with pytest.raises(pa.ArrowTypeError, match="Serial_number"):
            _load_ctx(dataset_path)


class TestGetInfo:
    def test_basic_aic_info_fields(self, basic_aic_path):
        ds = _load_ctx(basic_aic_path)
        info = ds.get_info()

        assert info["codebase_version"] == "v3.0"
        assert info["robot_type"] == "ur5e"
        assert info["total_episodes"] == 40
        assert info["total_tasks"] == 2
        assert info["fps"] == 20

    def test_hojun_info_fields(self, hojun_path):
        ds = _load_ctx(hojun_path)
        info = ds.get_info()

        assert info["codebase_version"] == "v3.0"
        assert info["robot_type"] == "ur5e"
        assert info["total_episodes"] == 6
        assert info["total_tasks"] == 1
        assert info["fps"] == 20


# ---------------------------------------------------------------------------
# get_episodes
# ---------------------------------------------------------------------------

class TestGetEpisodes:
    def test_basic_aic_episode_count(self, basic_aic_path):
        ds = _load_ctx(basic_aic_path)
        episodes = ds.get_episodes()
        assert len(episodes) == 40

    def test_hojun_episode_count(self, hojun_path):
        ds = _load_ctx(hojun_path)
        episodes = ds.get_episodes()
        # info.json says 9 but parquet files only contain 6 episodes
        assert len(episodes) == 6

    def test_episodes_have_required_columns(self, basic_aic_path):
        ds = _load_ctx(basic_aic_path)
        episodes = ds.get_episodes()
        ep = episodes[0]

        assert "episode_index" in ep
        assert "length" in ep
        assert "data/chunk_index" in ep
        assert "data/file_index" in ep
        assert "dataset_from_index" in ep
        assert "dataset_to_index" in ep

    def test_episodes_spread_across_multiple_files(self, basic_aic_path):
        """basic_aic has 3 episode parquet files (file-000, file-001, file-002)."""
        ds = _load_ctx(basic_aic_path)
        assert len(ds.episode_parquet_files) == 3

    def test_episode_indices_are_contiguous(self, basic_aic_path):
        ds = _load_ctx(basic_aic_path)
        episodes = ds.get_episodes()
        indices = sorted(e["episode_index"] for e in episodes)
        assert indices == list(range(40))


# ---------------------------------------------------------------------------
# get_tasks
# ---------------------------------------------------------------------------

class TestGetTasks:
    def test_basic_aic_has_two_tasks(self, basic_aic_path):
        ds = _load_ctx(basic_aic_path)
        tasks = ds.get_tasks()
        assert len(tasks) == 2

    def test_hojun_has_one_task(self, hojun_path):
        ds = _load_ctx(hojun_path)
        tasks = ds.get_tasks()
        assert len(tasks) == 1

    def test_task_structure(self, basic_aic_path):
        ds = _load_ctx(basic_aic_path)
        tasks = ds.get_tasks()
        for t in tasks:
            assert "task_index" in t
            assert "task" in t

    def test_basic_aic_task_instructions(self, basic_aic_path):
        ds = _load_ctx(basic_aic_path)
        tasks = ds.get_tasks()
        task_map = {t["task_index"]: t["task"] for t in tasks}
        assert task_map[0] == "insert sfp cable into port"
        assert task_map[1] == "default_task"


# ---------------------------------------------------------------------------
# get_features
# ---------------------------------------------------------------------------

class TestGetFeatures:
    def test_basic_aic_has_camera_features(self, basic_aic_path):
        ds = _load_ctx(basic_aic_path)
        features = ds.get_features()

        assert "observation.images.cam_center" in features
        assert "observation.images.cam_left" in features
        assert "observation.images.cam_right" in features

    def test_camera_features_are_video_dtype(self, basic_aic_path):
        ds = _load_ctx(basic_aic_path)
        features = ds.get_features()

        for cam_key in ["observation.images.cam_center", "observation.images.cam_left", "observation.images.cam_right"]:
            assert features[cam_key]["dtype"] == "video"

    def test_has_state_and_action_features(self, basic_aic_path):
        ds = _load_ctx(basic_aic_path)
        features = ds.get_features()

        assert "observation.state" in features
        assert "action" in features
        assert features["observation.state"]["dtype"] == "float32"
        assert features["action"]["shape"] == [6]


# ---------------------------------------------------------------------------
# get_episode_file_location
# ---------------------------------------------------------------------------

class TestGetEpisodeFileLocation:
    def test_returns_location_for_valid_episode(self, basic_aic_path):
        ds = _load_ctx(basic_aic_path)
        loc = ds.get_episode_file_location(0)

        assert "data_chunk_index" in loc
        assert "data_file_index" in loc
        assert "dataset_from_index" in loc
        assert "dataset_to_index" in loc
        assert "videos" in loc

    def test_location_has_video_entries_for_each_camera(self, basic_aic_path):
        ds = _load_ctx(basic_aic_path)
        loc = ds.get_episode_file_location(0)

        for cam_key in ["observation.images.cam_center", "observation.images.cam_left", "observation.images.cam_right"]:
            assert cam_key in loc["videos"], f"Missing video entry for {cam_key}"
            assert "chunk_index" in loc["videos"][cam_key]
            assert "file_index" in loc["videos"][cam_key]

    def test_raises_for_invalid_episode(self, basic_aic_path):
        ds = _load_ctx(basic_aic_path)
        with pytest.raises(KeyError):
            ds.get_episode_file_location(9999)


# ---------------------------------------------------------------------------
# episode-to-file mapping
# ---------------------------------------------------------------------------

class TestEpisodeToFileMap:
    def test_every_episode_mapped_to_file(self, basic_aic_path):
        ds = _load_ctx(basic_aic_path)
        for i in range(40):
            file_path = ds.get_file_for_episode(i)
            assert file_path is not None, f"Episode {i} not mapped to any file"
            assert file_path.exists()

    def test_unmapped_episode_returns_none(self, basic_aic_path):
        ds = _load_ctx(basic_aic_path)
        assert ds.get_file_for_episode(9999) is None
