"""Tests for dataset_ops_engine LeRobot-backed dataset manipulation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest
from lerobot.datasets.lerobot_dataset import LeRobotDataset


@dataclass(frozen=True)
class _FakeLeRobotDataset:
    repo_id: str
    root: Path

    def __init__(self, repo_id: str, root: Path) -> None:
        object.__setattr__(self, "repo_id", repo_id)
        object.__setattr__(self, "root", Path(root))


class _FakeLeRobotTools:
    LeRobotDataset = _FakeLeRobotDataset

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def delete_episodes(
        self,
        dataset: _FakeLeRobotDataset,
        episode_indices: list[int],
        output_dir: Path,
        repo_id: str,
    ) -> _FakeLeRobotDataset:
        self.calls.append(
            (
                "delete",
                {
                    "dataset": dataset,
                    "episode_indices": episode_indices,
                    "output_dir": output_dir,
                    "repo_id": repo_id,
                },
            )
        )
        return _FakeLeRobotDataset(repo_id, output_dir)

    def split_dataset(
        self,
        dataset: _FakeLeRobotDataset,
        splits: dict[str, list[int]],
        output_dir: Path,
    ) -> dict[str, _FakeLeRobotDataset]:
        self.calls.append(
            (
                "split",
                {
                    "dataset": dataset,
                    "splits": splits,
                    "output_dir": output_dir,
                },
            )
        )
        name = next(iter(splits))
        return {name: _FakeLeRobotDataset(f"{dataset.repo_id}_{name}", output_dir / name)}

    def merge_datasets(
        self,
        datasets: list[_FakeLeRobotDataset],
        output_repo_id: str,
        output_dir: Path,
    ) -> _FakeLeRobotDataset:
        self.calls.append(
            (
                "merge",
                {
                    "datasets": datasets,
                    "output_repo_id": output_repo_id,
                    "output_dir": output_dir,
                },
            )
        )
        return _FakeLeRobotDataset(output_repo_id, output_dir)


def _create_lerobot_dataset(root: Path, episode_indices: range | list[int]) -> Path:
    features = {
        "observation.state": {"dtype": "float32", "shape": (2,), "names": ["x", "y"]},
        "action": {"dtype": "float32", "shape": (2,), "names": ["x", "y"]},
    }
    dataset = LeRobotDataset.create(
        repo_id=f"local/{root.name}",
        root=root,
        fps=30,
        features=features,
        robot_type="test_robot",
        use_videos=False,
    )

    for episode_index in episode_indices:
        for frame_index in range(episode_index + 2):
            dataset.add_frame(
                {
                    "observation.state": np.array(
                        [episode_index, frame_index],
                        dtype=np.float32,
                    ),
                    "action": np.array([episode_index + 10, frame_index], dtype=np.float32),
                    "task": "Pick up object",
                }
            )
        dataset.save_episode()

    dataset.finalize()
    return root


@pytest.fixture()
def sample_lerobot_dataset(tmp_path: Path) -> Path:
    return _create_lerobot_dataset(tmp_path / "source_dataset", range(5))


class TestLeRobotDatasetToolsDelegation:
    def test_repo_id_for_path_sanitizes_arbitrary_local_names(self, tmp_path: Path) -> None:
        from backend.datasets.services.dataset_ops_engine import _repo_id_for_path

        assert _repo_id_for_path(tmp_path / "My Dataset #1") == "local/My-Dataset--1"

    def test_delete_delegates_to_lerobot_dataset_tools(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from backend.datasets.services import dataset_ops_engine

        tools = _FakeLeRobotTools()
        monkeypatch.setattr(dataset_ops_engine, "_load_lerobot_dataset_tools", lambda: tools)

        source = tmp_path / "source_ds"
        output = tmp_path / "deleted_ds"

        result = dataset_ops_engine.delete_episodes(source, episode_ids=[1, 3], output_dir=output)

        assert result == output
        assert tools.calls == [
            (
                "delete",
                {
                    "dataset": _FakeLeRobotDataset("local/source_ds", source),
                    "episode_indices": [1, 3],
                    "output_dir": output,
                    "repo_id": "local/deleted_ds",
                },
            )
        ]

    def test_split_delegates_to_single_lerobot_named_split(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from backend.datasets.services import dataset_ops_engine

        tools = _FakeLeRobotTools()
        monkeypatch.setattr(dataset_ops_engine, "_load_lerobot_dataset_tools", lambda: tools)

        source = tmp_path / "source_ds"
        output = tmp_path / "split_ds"

        result = dataset_ops_engine.split_dataset(source, episode_ids=[2, 4], output_dir=output)

        assert result == output
        assert tools.calls == [
            (
                "split",
                {
                    "dataset": _FakeLeRobotDataset("local/source_ds", source),
                    "splits": {"split_ds": [2, 4]},
                    "output_dir": tmp_path,
                },
            )
        ]

    def test_merge_delegates_to_lerobot_dataset_tools(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from backend.datasets.services import dataset_ops_engine

        tools = _FakeLeRobotTools()
        monkeypatch.setattr(dataset_ops_engine, "_load_lerobot_dataset_tools", lambda: tools)

        sources = [tmp_path / "source_a", tmp_path / "source_b"]
        output = tmp_path / "merged_ds"

        result = dataset_ops_engine.merge_datasets(sources, output_dir=output)

        assert result == output
        assert tools.calls == [
            (
                "merge",
                {
                    "datasets": [
                        _FakeLeRobotDataset("local/source_a", sources[0]),
                        _FakeLeRobotDataset("local/source_b", sources[1]),
                    ],
                    "output_repo_id": "local/merged_ds",
                    "output_dir": output,
                },
            )
        ]


class TestReadUtilities:
    def test_read_info_and_episodes(self, sample_lerobot_dataset: Path) -> None:
        from backend.datasets.services.dataset_ops_engine import read_episodes, read_info

        info = read_info(sample_lerobot_dataset)
        episodes = read_episodes(sample_lerobot_dataset)

        assert info["total_episodes"] == 5
        assert info["robot_type"] == "test_robot"
        assert episodes.column("episode_index").to_pylist() == [0, 1, 2, 3, 4]

    def test_read_tasks(self, sample_lerobot_dataset: Path) -> None:
        from backend.datasets.services.dataset_ops_engine import read_tasks

        tasks = read_tasks(sample_lerobot_dataset)

        assert len(tasks) == 1
        assert "task_index" in tasks.schema.names

    def test_get_camera_keys_for_no_video_dataset(self, sample_lerobot_dataset: Path) -> None:
        from backend.datasets.services.dataset_ops_engine import get_camera_keys, read_info

        assert get_camera_keys(read_info(sample_lerobot_dataset)) == []


class TestLeRobotIntegration:
    def test_split_dataset_writes_exact_output_path(
        self,
        sample_lerobot_dataset: Path,
        tmp_path: Path,
    ) -> None:
        from backend.datasets.services.dataset_ops_engine import read_episodes, read_info, split_dataset

        output = tmp_path / "split_output"

        result = split_dataset(sample_lerobot_dataset, episode_ids=[1, 3], output_dir=output)

        assert result == output
        assert read_info(output)["total_episodes"] == 2
        assert read_info(output)["total_frames"] == 8
        assert read_episodes(output).column("episode_index").to_pylist() == [0, 1]

    def test_delete_episodes_writes_reindexed_dataset(
        self,
        sample_lerobot_dataset: Path,
        tmp_path: Path,
    ) -> None:
        from backend.datasets.services.dataset_ops_engine import delete_episodes, read_episodes, read_info

        output = tmp_path / "delete_output"

        result = delete_episodes(sample_lerobot_dataset, episode_ids=[1, 3], output_dir=output)

        assert result == output
        assert read_info(output)["total_episodes"] == 3
        assert read_info(output)["total_frames"] == 12
        assert read_episodes(output).column("episode_index").to_pylist() == [0, 1, 2]

    def test_merge_datasets_writes_reindexed_dataset(
        self,
        sample_lerobot_dataset: Path,
        tmp_path: Path,
    ) -> None:
        from backend.datasets.services.dataset_ops_engine import (
            merge_datasets,
            read_episodes,
            read_info,
            split_dataset,
        )

        left = tmp_path / "left"
        right = tmp_path / "right"
        merged = tmp_path / "merged"
        split_dataset(sample_lerobot_dataset, episode_ids=[0, 1], output_dir=left)
        split_dataset(sample_lerobot_dataset, episode_ids=[2, 3], output_dir=right)

        result = merge_datasets([left, right], output_dir=merged)

        assert result == merged
        assert read_info(merged)["total_episodes"] == 4
        assert read_info(merged)["total_frames"] == 14
        assert read_episodes(merged).column("episode_index").to_pylist() == [0, 1, 2, 3]

    def test_merge_rejects_empty_source_list(self, tmp_path: Path) -> None:
        from backend.datasets.services.dataset_ops_engine import merge_datasets

        with pytest.raises(ValueError, match="No datasets to merge"):
            merge_datasets([], output_dir=tmp_path / "merged")
