"""Tests for pure gripper cycle-boundary detection and dataset stamping."""

import asyncio
import builtins
import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from backend.datasets.services.cycle_stamp_service import (
    LEFT_GRIPPER_IDX,
    RIGHT_GRIPPER_IDX,
    describe_stamp_state,
    detect_cycle_ends,
    stamp_dataset_cycles,
)


def make_states(left_values, right_values):
    """Build a state array with only the gripper columns populated."""
    assert len(left_values) == len(right_values)
    states = np.zeros((len(left_values), 16), dtype=np.float32)
    states[:, LEFT_GRIPPER_IDX] = left_values
    states[:, RIGHT_GRIPPER_IDX] = right_values
    return states


def _write_fake_dataset(
    root: Path,
    episodes: list[np.ndarray],
    *,
    episode_frame_indices: list[list[int]] | None = None,
) -> Path:
    """Create a minimal LeRobot-style dataset split across two parquet files."""
    meta_dir = root / "meta"
    data_dir = root / "data" / "chunk-000"
    meta_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)

    total_frames = sum(len(episode) for episode in episodes)
    info = {
        "codebase_version": "v3.0",
        "robot_type": "test_robot",
        "total_episodes": len(episodes),
        "total_frames": total_frames,
        "chunks_size": 1000,
        "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
        "features": {
            "observation.state": {"dtype": "float32", "shape": [16], "names": None},
            "episode_index": {"dtype": "int64", "shape": [1], "names": None},
            "frame_index": {"dtype": "int64", "shape": [1], "names": None},
        },
    }
    (meta_dir / "info.json").write_text(json.dumps(info, indent=2))

    if episode_frame_indices is None:
        episode_frame_indices = [list(range(len(episode))) for episode in episodes]

    rows: list[np.ndarray] = []
    episode_indices: list[int] = []
    frame_indices: list[int] = []
    for episode_index, (episode, logical_indices) in enumerate(zip(episodes, episode_frame_indices, strict=True)):
        assert len(episode) == len(logical_indices)
        for row, frame_index in zip(episode, logical_indices, strict=True):
            rows.append(np.asarray(row, dtype=np.float32))
            episode_indices.append(episode_index)
            frame_indices.append(frame_index)

    split_at = min(len(episodes[0]) + 1, total_frames - 1)
    file_slices = [
        (0, split_at),
        (split_at, total_frames),
    ]

    for file_index, (start, stop) in enumerate(file_slices):
        frame_rows = rows[start:stop]
        flat_values = np.concatenate(frame_rows).astype(np.float32, copy=False)
        table = pa.table(
            {
                "observation.state": pa.FixedSizeListArray.from_arrays(
                    pa.array(flat_values.tolist(), type=pa.float32()),
                    16,
                ),
                "episode_index": pa.array(episode_indices[start:stop], type=pa.int64()),
                "frame_index": pa.array(frame_indices[start:stop], type=pa.int64()),
            }
        )
        pq.write_table(table, data_dir / f"file-{file_index:03d}.parquet")

    return root


def _add_stamp_column(parquet_path: Path, column_name: str, values: list[bool]) -> None:
    """Inject a single stamp column into one parquet file for repair-path tests."""
    table = pq.read_table(parquet_path)
    col_idx = table.schema.get_field_index(column_name)
    if col_idx >= 0:
        table = table.remove_column(col_idx)
    table = table.append_column(column_name, pa.array(values, type=pa.bool_()))
    pq.write_table(table, parquet_path)


class TestDetectCycleEnds:
    """Verify cycle-end detection across the two gripper traces."""

    def test_locks_expected_gripper_indices(self):
        assert LEFT_GRIPPER_IDX == 7
        assert RIGHT_GRIPPER_IDX == 15

    def test_detects_two_cycles_from_left_gripper_only(self):
        states = make_states(
            [0.9, 0.4, 0.3, 0.4, 0.81, 0.2, 0.1, 0.3, 0.81],
            [0.9] * 9,
        )

        assert detect_cycle_ends(states) == [4, 8]

    def test_detects_cycles_when_trace_starts_closed(self):
        states = make_states(
            [0.4, 0.3, 0.81, 0.2, 0.3, 0.81],
            [0.9] * 6,
        )

        assert detect_cycle_ends(states) == [2, 5]

    def test_merges_cycle_ends_from_both_arms(self):
        states = make_states(
            [0.9, 0.4, 0.81, 0.9, 0.9],
            [0.9, 0.9, 0.9, 0.2, 0.81],
        )

        assert detect_cycle_ends(states) == [2, 4]

    def test_deduplicates_reopenings_on_same_frame(self):
        states = make_states(
            [0.9, 0.2, 0.81, 0.9],
            [0.9, 0.3, 0.81, 0.9],
        )

        assert detect_cycle_ends(states) == [2]

    def test_returns_empty_when_grippers_are_always_open(self):
        states = make_states([0.9, 0.95, 0.85], [0.82, 0.9, 0.99])

        assert detect_cycle_ends(states) == []

    def test_returns_empty_when_grippers_are_always_closed(self):
        states = make_states([0.2, 0.3, 0.4], [0.1, 0.2, 0.49])

        assert detect_cycle_ends(states) == []

    def test_ignores_borderline_hysteresis_values(self):
        states = make_states(
            [0.5, 0.79, 0.8, 0.6, 0.5],
            [0.8, 0.79, 0.5, 0.6, 0.8],
        )

        assert detect_cycle_ends(states) == []


class TestStampDatasetCycles:
    def test_adds_is_terminal_and_is_last(self, tmp_path: Path):
        dataset_root = _write_fake_dataset(
            tmp_path / "dataset",
            [
                make_states([0.9, 0.4, 0.3, 0.4, 0.79, 0.81], [0.9] * 6),
                make_states([0.9, 0.9, 0.9, 0.9], [0.9] * 4),
            ],
        )

        result = stamp_dataset_cycles(dataset_root, overwrite=False)

        assert result["episodes_processed"] == 2
        assert result["is_terminal_count"] == 1
        assert result["is_last_count"] == 2

        tables = [
            pq.read_table(dataset_root / "data" / "chunk-000" / "file-000.parquet"),
            pq.read_table(dataset_root / "data" / "chunk-000" / "file-001.parquet"),
        ]
        for table in tables:
            assert "is_terminal" in table.schema.names
            assert "is_last" in table.schema.names

        combined = pa.concat_tables(tables)
        is_terminal = combined.column("is_terminal").to_pylist()
        is_last = combined.column("is_last").to_pylist()

        assert sum(is_terminal) == 1
        assert sum(is_last) == 2
        assert [idx for idx, value in enumerate(is_last) if value] == [5, 9]

    def test_refuses_to_restamp_without_overwrite(self, tmp_path: Path):
        dataset_root = _write_fake_dataset(
            tmp_path / "dataset",
            [
                make_states([0.9, 0.4, 0.3, 0.4, 0.79, 0.81], [0.9] * 6),
                make_states([0.9, 0.9, 0.9, 0.9], [0.9] * 4),
            ],
        )

        stamp_dataset_cycles(dataset_root, overwrite=False)

        with pytest.raises(ValueError, match="already_stamped"):
            stamp_dataset_cycles(dataset_root, overwrite=False)

    def test_refuses_partial_prior_stamp_without_overwrite(self, tmp_path: Path):
        dataset_root = _write_fake_dataset(
            tmp_path / "dataset",
            [
                make_states([0.9, 0.4, 0.3, 0.4, 0.79, 0.81], [0.9] * 6),
                make_states([0.9, 0.9, 0.9, 0.9], [0.9] * 4),
            ],
        )
        first_file = dataset_root / "data" / "chunk-000" / "file-000.parquet"
        _add_stamp_column(first_file, "is_last", [False] * 7)

        with pytest.raises(ValueError, match="already_stamped"):
            stamp_dataset_cycles(dataset_root, overwrite=False)

    def test_overwrite_replaces_existing_columns(self, tmp_path: Path):
        dataset_root = _write_fake_dataset(
            tmp_path / "dataset",
            [
                make_states([0.9, 0.4, 0.3, 0.4, 0.79, 0.81], [0.9] * 6),
                make_states([0.9, 0.9, 0.9, 0.9], [0.9] * 4),
            ],
        )

        stamp_dataset_cycles(dataset_root, overwrite=False)

        for parquet_path in sorted((dataset_root / "data" / "chunk-000").glob("file-*.parquet")):
            table = pq.read_table(parquet_path)
            for name in ("is_terminal", "is_last"):
                col_idx = table.schema.get_field_index(name)
                table = table.remove_column(col_idx)
            table = table.append_column("is_terminal", pa.array([True] * len(table), type=pa.bool_()))
            table = table.append_column("is_last", pa.array([True] * len(table), type=pa.bool_()))
            pq.write_table(table, parquet_path)

        result = stamp_dataset_cycles(dataset_root, overwrite=True)

        assert result["episodes_processed"] == 2
        assert result["is_terminal_count"] == 1
        assert result["is_last_count"] == 2

        combined = pa.concat_tables(
            [
                pq.read_table(path)
                for path in sorted((dataset_root / "data" / "chunk-000").glob("file-*.parquet"))
            ]
        )
        assert sum(combined.column("is_terminal").to_pylist()) == 1
        assert sum(combined.column("is_last").to_pylist()) == 2

    def test_overwrite_repairs_partial_prior_stamp_and_keeps_files_readable(self, tmp_path: Path):
        dataset_root = _write_fake_dataset(
            tmp_path / "dataset",
            [
                make_states([0.9, 0.4, 0.3, 0.4, 0.79, 0.81], [0.9] * 6),
                make_states([0.9, 0.9, 0.9, 0.9], [0.9] * 4),
            ],
        )
        first_file = dataset_root / "data" / "chunk-000" / "file-000.parquet"
        _add_stamp_column(first_file, "is_last", [True] * 7)

        result = stamp_dataset_cycles(dataset_root, overwrite=True)

        assert result["episodes_processed"] == 2
        assert result["is_terminal_count"] == 1
        assert result["is_last_count"] == 2

        tables = [
            pq.read_table(path)
            for path in sorted((dataset_root / "data" / "chunk-000").glob("file-*.parquet"))
        ]
        combined = pa.concat_tables(tables)
        assert all("is_terminal" in table.schema.names for table in tables)
        assert all("is_last" in table.schema.names for table in tables)
        assert sum(combined.column("is_terminal").to_pylist()) == 1
        assert sum(combined.column("is_last").to_pylist()) == 2
        assert [idx for idx, value in enumerate(combined.column("is_last").to_pylist()) if value] == [5, 9]

    def test_describe_stamp_state(self, tmp_path: Path):
        dataset_root = _write_fake_dataset(
            tmp_path / "dataset",
            [
                make_states([0.9, 0.4, 0.3, 0.4, 0.79, 0.81], [0.9] * 6),
                make_states([0.9, 0.9, 0.9, 0.9], [0.9] * 4),
            ],
        )

        before = describe_stamp_state(dataset_root)
        assert before == {"stamped": False, "is_terminal_count_sample": 0}

        stamp_dataset_cycles(dataset_root, overwrite=False)

        after = describe_stamp_state(dataset_root)
        assert after["stamped"] is True
        assert after["is_terminal_count_sample"] == 1

    def test_marks_cycle_end_when_split_episode_reopens_in_second_file(self, tmp_path: Path):
        dataset_root = _write_fake_dataset(
            tmp_path / "dataset",
            [
                make_states([0.9, 0.9, 0.9, 0.9], [0.9] * 4),
                make_states([0.9, 0.4, 0.3, 0.81, 0.9], [0.9] * 5),
            ],
        )

        result = stamp_dataset_cycles(dataset_root, overwrite=False)

        assert result["episodes_processed"] == 2
        assert result["is_terminal_count"] == 1
        second_file = pq.read_table(dataset_root / "data" / "chunk-000" / "file-001.parquet")
        second_file_terminal = second_file.column("is_terminal").to_pylist()
        second_file_last = second_file.column("is_last").to_pylist()
        second_file_episode_index = second_file.column("episode_index").to_pylist()

        assert second_file_episode_index == [1, 1, 1, 1]
        assert second_file_terminal == [False, False, True, False]
        assert second_file_last == [False, False, False, True]

    def test_uses_logical_frame_index_order_when_rows_are_shuffled(self, tmp_path: Path):
        dataset_root = _write_fake_dataset(
            tmp_path / "dataset",
            [
                make_states([0.3, 0.9, 0.81, 0.4], [0.9] * 4),
            ],
            episode_frame_indices=[[2, 0, 3, 1]],
        )

        result = stamp_dataset_cycles(dataset_root, overwrite=False)

        assert result["episodes_processed"] == 1
        assert result["is_terminal_count"] == 1
        assert result["is_last_count"] == 1

        combined = pa.concat_tables(
            [
                pq.read_table(path)
                for path in sorted((dataset_root / "data" / "chunk-000").glob("file-*.parquet"))
            ]
        )
        assert combined.column("frame_index").to_pylist() == [2, 0, 3, 1]
        assert combined.column("is_terminal").to_pylist() == [False, False, True, False]
        assert combined.column("is_last").to_pylist() == [False, False, True, False]


class TestCycleStampHandler:
    @pytest.mark.asyncio
    async def test_stamp_cycles_handler_completes(self, tmp_path: Path):
        from backend.datasets.services.cycle_stamp_handler import handle_stamp_cycles

        ds = _write_fake_dataset(
            tmp_path / "dataset",
            [
                make_states([0.9, 0.4, 0.3, 0.4, 0.79, 0.81], [0.9] * 6),
            ],
        )

        result = await handle_stamp_cycles({"payload": {"source_path": str(ds), "overwrite": False}})

        assert result == {"result_path": str(ds)}
        combined = pa.concat_tables(
            [
                pq.read_table(path)
                for path in sorted((ds / "data" / "chunk-000").glob("file-*.parquet"))
            ]
        )
        assert combined.column("is_terminal").to_pylist() == [False, False, False, False, False, True]
        assert combined.column("is_last").to_pylist() == [False, False, False, False, False, True]

    @pytest.mark.asyncio
    async def test_stamp_cycles_handler_failure_restores_original_dataset(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from backend.datasets.services import cycle_stamp_service
        from backend.datasets.services.cycle_stamp_handler import handle_stamp_cycles

        ds = _write_fake_dataset(
            tmp_path / "dataset",
            [
                make_states([0.9, 0.4, 0.3, 0.4, 0.79, 0.81], [0.9] * 6),
                make_states([0.9, 0.9, 0.9, 0.9], [0.9] * 4),
            ],
        )
        parquet_paths = sorted((ds / "data" / "chunk-000").glob("file-*.parquet"))

        def partially_stamp_then_fail(dataset_path: Path, *, overwrite: bool) -> None:
            assert dataset_path == ds
            assert overwrite is False
            _add_stamp_column(parquet_paths[0], "is_last", [True] * 7)
            raise RuntimeError("stamp write boom")

        monkeypatch.setattr(
            cycle_stamp_service,
            "stamp_dataset_cycles",
            partially_stamp_then_fail,
        )

        with pytest.raises(RuntimeError, match="stamp write boom"):
            await handle_stamp_cycles({"payload": {"source_path": str(ds), "overwrite": False}})

        restored_tables = [pq.read_table(path) for path in parquet_paths]
        assert all("is_terminal" not in table.schema.names for table in restored_tables)
        assert all("is_last" not in table.schema.names for table in restored_tables)
