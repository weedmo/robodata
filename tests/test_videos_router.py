from pathlib import Path

import pytest

from backend.datasets.routers import videos


class FakeDatasetContext:
    def __init__(self, root: Path):
        self.root = root

    def get_episode_file_location(self, episode_index: int):
        assert episode_index == 3
        return {
            "data_chunk_index": 0,
            "data_file_index": 0,
            "videos": {
                "observation.images.cam": {
                    "chunk_index": 0,
                    "file_index": 0,
                    "from_timestamp": 2.3,
                    "to_timestamp": 10.2,
                },
            },
        }

    def get_features(self):
        return {"observation.images.cam": {"dtype": "video"}}

    def get_dataset_path(self):
        return str(self.root)


def _make_context(tmp_path: Path) -> FakeDatasetContext:
    video_dir = tmp_path / "videos" / "observation.images.cam" / "chunk-000"
    video_dir.mkdir(parents=True)
    (video_dir / "file-000.mp4").write_bytes(b"fake mp4")
    return FakeDatasetContext(tmp_path)


def test_camera_response_exposes_stable_file_url_and_source_timebase(tmp_path):
    ctx = _make_context(tmp_path)
    video_path = tmp_path / "videos" / "observation.images.cam" / "chunk-000" / "file-000.mp4"

    cameras = videos._camera_response(ctx, 3, dataset_key="abc")

    assert len(cameras) == 1
    assert cameras[0]["key"] == "observation.images.cam"
    assert cameras[0]["label"] == "cam"
    assert cameras[0]["url"] == videos._video_file_url(
        "abc",
        "observation.images.cam",
        0,
        0,
        video_path,
    )
    assert cameras[0]["from_timestamp"] == pytest.approx(2.3)
    assert cameras[0]["to_timestamp"] == pytest.approx(10.2)


def test_stream_response_serves_source_video_file(tmp_path):
    ctx = _make_context(tmp_path)

    response = videos._stream_response(ctx, 3, "observation.images.cam")

    assert response.path == str(
        tmp_path / "videos" / "observation.images.cam" / "chunk-000" / "file-000.mp4",
    )
    assert response.headers["cache-control"] == videos.VIDEO_CACHE_CONTROL


def test_file_response_serves_stable_chunk_file_route(tmp_path):
    ctx = _make_context(tmp_path)

    response = videos._stream_file_response(ctx, 0, 0, "observation.images.cam")

    assert response.path == str(
        tmp_path / "videos" / "observation.images.cam" / "chunk-000" / "file-000.mp4",
    )
    assert response.headers["cache-control"] == videos.VIDEO_CACHE_CONTROL
