from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from backend.datasets.routers import scalars as scalars_router


def _write_split_style_data_file(root: Path) -> None:
    data_path = root / "data" / "chunk-000" / "file-001.parquet"
    data_path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "index": pa.array([120036, 120037, 120038], type=pa.int64()),
            "frame_index": pa.array([0, 1, 2], type=pa.int64()),
            "episode_index": pa.array([511, 511, 511], type=pa.int64()),
            "timestamp": pa.array([0.1, 0.2, 0.3], type=pa.float64()),
            "is_terminal": pa.array([False, False, True], type=pa.bool_()),
            "observation.state": pa.array(
                [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]],
                type=pa.list_(pa.float32()),
            ),
            "action": pa.array(
                [[10.0, 20.0], [30.0, 40.0], [50.0, 60.0]],
                type=pa.list_(pa.float32()),
            ),
        }
    )
    pq.write_table(table, data_path)


def _scalar_features() -> dict[str, dict[str, str]]:
    return {
        "observation.state": {"dtype": "float32"},
        "action": {"dtype": "float32"},
        "is_terminal": {"dtype": "bool"},
        "timestamp": {"dtype": "float64"},
    }


@pytest.mark.asyncio
async def test_scalars_router_uses_global_index_column_for_split_dataset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset_path = tmp_path / "dataset"
    _write_split_style_data_file(dataset_path)

    monkeypatch.setattr(
        scalars_router.dataset_service,
        "get_episode_file_location",
        lambda _episode_index: {
            "dataset_from_index": 120036,
            "dataset_to_index": 120039,
            "data_chunk_index": 0,
            "data_file_index": 1,
        },
    )
    monkeypatch.setattr(
        scalars_router.dataset_service,
        "get_dataset_path",
        lambda: str(dataset_path),
    )
    monkeypatch.setattr(
        scalars_router.dataset_service,
        "get_features",
        _scalar_features,
    )

    result = await scalars_router.get_scalars(episode_index=511)

    assert result["num_frames"] == 3
    assert result["observations"] == {
        "observation.state[0]": [1.0, 3.0, 5.0],
        "observation.state[1]": [2.0, 4.0, 6.0],
    }
    assert result["actions"] == {
        "action[0]": [10.0, 30.0, 50.0],
        "action[1]": [20.0, 40.0, 60.0],
    }
    assert result["terminal_frames"] == [2]
    assert result["terminal_timestamps"] == [0.3]
