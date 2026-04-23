from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pyarrow as pa
import pytest

from backend.datasets.services import rerun_service


class _FakeRR:
    def __init__(self) -> None:
        self.logged: list[tuple[str, object]] = []
        self.times: list[tuple[str, int]] = []

    def log(self, entity: str, value: object) -> None:
        self.logged.append((entity, value))

    def set_time(self, timeline: str, *, sequence: int) -> None:
        self.times.append((timeline, sequence))

    class Clear:
        def __init__(self, recursive: bool) -> None:
            self.recursive = recursive

    class Scalar:
        def __init__(self, value: float) -> None:
            self.value = value

    class Image:
        def __init__(self, value: object) -> None:
            self.value = value


def _install_fake_rerun(monkeypatch: pytest.MonkeyPatch, table: pa.Table, fake_rr: _FakeRR) -> None:
    monkeypatch.setattr(rerun_service, "HAS_RERUN", True)
    monkeypatch.setattr(rerun_service, "_RERUN_READY", True)
    monkeypatch.setattr(rerun_service, "rr", fake_rr)
    monkeypatch.setattr(rerun_service, "np", np)
    monkeypatch.setattr(
        rerun_service,
        "pq",
        SimpleNamespace(read_table=lambda _path: table),
    )


@pytest.mark.asyncio
async def test_visualize_episode_starts_shared_video_at_episode_offset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dataset_path = tmp_path / "dataset"
    data_path = dataset_path / "data" / "chunk-000" / "file-000.parquet"
    video_path = dataset_path / "videos" / "observation.images.cam_top" / "chunk-000" / "file-000.mp4"
    data_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"parquet")
    video_path.write_bytes(b"mp4")

    table = pa.table({"timestamp": [0.0, 1 / 30, 2 / 30]})
    fake_rr = _FakeRR()
    extract_calls: list[tuple[Path, int, int]] = []

    _install_fake_rerun(monkeypatch, table, fake_rr)
    monkeypatch.setattr(
        rerun_service.dataset_service,
        "get_episode_file_location",
        lambda _episode_index: {
            "dataset_from_index": 10,
            "dataset_to_index": 13,
            "data_chunk_index": 0,
            "data_file_index": 0,
            "videos": {
                "observation.images.cam_top": {
                    "chunk_index": 0,
                    "file_index": 0,
                    "from_timestamp": 1.5,
                    "to_timestamp": 1.6,
                }
            },
        },
    )
    monkeypatch.setattr(rerun_service.dataset_service, "get_dataset_path", lambda: str(dataset_path))
    monkeypatch.setattr(rerun_service.dataset_service, "get_info", lambda: {"fps": 30})
    monkeypatch.setattr(
        rerun_service.dataset_service,
        "get_features",
        lambda: {
            "observation.images.cam_top": {
                "dtype": "video",
                "video_info": {"video.fps": 30},
            }
        },
    )
    monkeypatch.setattr(
        rerun_service,
        "_extract_video_frames",
        lambda path, start_frame, num_frames: extract_calls.append((path, start_frame, num_frames)) or [],
    )

    await rerun_service.visualize_episode(12)

    assert extract_calls == [(video_path, 45, 3)]


@pytest.mark.asyncio
async def test_visualize_episode_uses_file_local_rows_for_global_dataset_indices(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dataset_path = tmp_path / "dataset"
    data_path = dataset_path / "data" / "chunk-000" / "file-001.parquet"
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"parquet")

    table = pa.table(
        {
            "index": [10, 11, 12],
            "frame_index": [0, 1, 2],
            "timestamp": [10 / 30, 11 / 30, 12 / 30],
            "observation.state": [[1.0], [2.0], [3.0]],
            "action": [[11.0], [12.0], [13.0]],
        }
    )
    fake_rr = _FakeRR()

    _install_fake_rerun(monkeypatch, table, fake_rr)
    monkeypatch.setattr(
        rerun_service.dataset_service,
        "get_episode_file_location",
        lambda _episode_index: {
            "dataset_from_index": 10,
            "dataset_to_index": 13,
            "data_chunk_index": 0,
            "data_file_index": 1,
            "videos": {},
        },
    )
    monkeypatch.setattr(rerun_service.dataset_service, "get_dataset_path", lambda: str(dataset_path))
    monkeypatch.setattr(rerun_service.dataset_service, "get_info", lambda: {"fps": 30})
    monkeypatch.setattr(
        rerun_service.dataset_service,
        "get_features",
        lambda: {
            "observation.state": {"dtype": "float32"},
            "action": {"dtype": "float32"},
        },
    )
    monkeypatch.setattr(rerun_service, "_extract_video_frames", lambda *_args, **_kwargs: [])

    await rerun_service.visualize_episode(1)

    frame_times = [sequence for timeline, sequence in fake_rr.times if timeline == "frame"]
    observation_scalars = [
        value.value for entity, value in fake_rr.logged
        if entity == "observation/observation.state"
    ]
    action_scalars = [
        value.value for entity, value in fake_rr.logged
        if entity == "action/action"
    ]

    assert frame_times == [0, 1, 2]
    assert observation_scalars == [1.0, 2.0, 3.0]
    assert action_scalars == [11.0, 12.0, 13.0]


@pytest.mark.asyncio
async def test_visualize_episode_clears_previously_logged_entity_roots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression: Clear must target the entity roots that actually receive
    data (observation/, action/, camera/). Clearing an unrelated 'world' path
    leaves a previous episode's scalars and video frames on the shared
    'frame' timeline, so a short episode following a longer one inherits the
    tail of the previous visualization.
    """
    dataset_path = tmp_path / "dataset"
    data_path = dataset_path / "data" / "chunk-000" / "file-000.parquet"
    video_path = dataset_path / "videos" / "observation.images.cam_top" / "chunk-000" / "file-000.mp4"
    data_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"parquet")
    video_path.write_bytes(b"mp4")

    table = pa.table(
        {
            "index": [0, 1, 2],
            "frame_index": [0, 1, 2],
            "observation.state": [[1.0], [2.0], [3.0]],
            "action": [[11.0], [12.0], [13.0]],
        }
    )
    fake_rr = _FakeRR()

    _install_fake_rerun(monkeypatch, table, fake_rr)
    monkeypatch.setattr(
        rerun_service.dataset_service,
        "get_episode_file_location",
        lambda _episode_index: {
            "dataset_from_index": 0,
            "dataset_to_index": 3,
            "data_chunk_index": 0,
            "data_file_index": 0,
            "videos": {
                "observation.images.cam_top": {
                    "chunk_index": 0,
                    "file_index": 0,
                    "from_timestamp": 0.0,
                    "to_timestamp": 0.1,
                }
            },
        },
    )
    monkeypatch.setattr(rerun_service.dataset_service, "get_dataset_path", lambda: str(dataset_path))
    monkeypatch.setattr(rerun_service.dataset_service, "get_info", lambda: {"fps": 30})
    monkeypatch.setattr(
        rerun_service.dataset_service,
        "get_features",
        lambda: {
            "observation.state": {"dtype": "float32"},
            "action": {"dtype": "float32"},
            "observation.images.cam_top": {
                "dtype": "video",
                "video_info": {"video.fps": 30},
            },
        },
    )
    monkeypatch.setattr(
        rerun_service,
        "_extract_video_frames",
        lambda *_args, **_kwargs: [np.zeros((1, 1, 3), dtype=np.uint8)],
    )

    await rerun_service.visualize_episode(0)

    clear_log_indices: dict[str, int] = {}
    for idx, (entity, value) in enumerate(fake_rr.logged):
        if isinstance(value, _FakeRR.Clear) and value.recursive:
            clear_log_indices.setdefault(entity, idx)

    assert {"observation", "action", "camera"}.issubset(clear_log_indices.keys()), (
        "expected Clear(recursive=True) on the three entity roots actually "
        f"written to (observation, action, camera); got clears on: "
        f"{sorted(clear_log_indices)}"
    )

    for root in ("observation", "action", "camera"):
        first_child_log = next(
            (
                idx
                for idx, (entity, value) in enumerate(fake_rr.logged)
                if entity.startswith(f"{root}/") and not isinstance(value, _FakeRR.Clear)
            ),
            None,
        )
        assert first_child_log is not None, f"no child log found under {root!r}"
        assert clear_log_indices[root] < first_child_log, (
            f"Clear for {root!r} (idx {clear_log_indices[root]}) must precede "
            f"its first data log (idx {first_child_log})"
        )
