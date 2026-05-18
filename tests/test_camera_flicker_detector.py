import numpy as np
import pytest

from backend.datasets.services.camera_flicker_detector import (
    CameraFlickerScanResult,
    detect_localized_luminance_flicker,
    is_head_or_zed_camera_key,
    mark_camera_flicker_matches_bad,
    scan_dataset_for_camera_flicker,
)


def _solid_frame(value: float, *, shape: tuple[int, int, int] = (8, 8, 3)) -> np.ndarray:
    return np.full(shape, value, dtype=np.float32)


def test_detects_localized_tile_luminance_spike():
    frames = [_solid_frame(30.0) for _ in range(6)]
    frames[3] = frames[3].copy()
    frames[3][:4, :4, :] = 220.0

    result = detect_localized_luminance_flicker(frames, tile_grid=(4, 4))

    assert result.matched is True
    assert result.spike_count >= 1
    assert result.max_tile_delta >= 100.0
    assert 3 in result.spike_frame_indices


def test_rejects_uniform_whole_frame_brightness_shift():
    frames = [
        _solid_frame(30.0),
        _solid_frame(30.0),
        _solid_frame(220.0),
        _solid_frame(220.0),
    ]

    result = detect_localized_luminance_flicker(frames, tile_grid=(4, 4))

    assert result.matched is False
    assert result.spike_count == 0


def test_stable_noisy_sequence_stays_below_threshold():
    rng = np.random.default_rng(7)
    frames = [
        _solid_frame(80.0) + rng.normal(0.0, 1.0, size=(8, 8, 3)).astype(np.float32)
        for _ in range(10)
    ]

    result = detect_localized_luminance_flicker(frames, tile_grid=(4, 4))

    assert result.matched is False
    assert result.spike_count == 0


def test_head_and_zed_camera_key_selection():
    accepted = [
        "observation.images.cam_head",
        "observation.images.cam_head_right",
        "/zed/zed_node/left/image_rect_color/compressed",
        "zed_left",
    ]
    rejected = [
        "observation.images.cam_wrist_left",
        "observation.images.cam_wrist_right",
        "overview_camera",
    ]

    assert all(is_head_or_zed_camera_key(key) for key in accepted)
    assert not any(is_head_or_zed_camera_key(key) for key in rejected)


class _FakeDatasetContext:
    def __init__(self):
        self.dataset_path = "/datasets/example"
        self._episodes = [{"episode_index": 1}, {"episode_index": 2}]
        self._features = {
            "observation.images.cam_head": {"dtype": "video"},
            "observation.images.cam_wrist_left": {"dtype": "video"},
            "observation.state": {"dtype": "float32"},
        }
        self._locations = {
            1: {
                "videos": {
                    "observation.images.cam_head": {
                        "chunk_index": 0,
                        "file_index": 3,
                        "from_timestamp": 12.0,
                        "to_timestamp": 13.0,
                    },
                    "observation.images.cam_wrist_left": {
                        "chunk_index": 0,
                        "file_index": 4,
                        "from_timestamp": 12.0,
                        "to_timestamp": 13.0,
                    },
                },
            },
            2: {
                "videos": {
                    "observation.images.cam_head": {
                        "chunk_index": 1,
                        "file_index": 2,
                        "from_timestamp": 20.0,
                        "to_timestamp": 21.0,
                    },
                },
            },
        }

    def get_dataset_path(self):
        return self.dataset_path

    def get_episodes(self):
        return self._episodes

    def get_features(self):
        return self._features

    def get_episode_file_location(self, episode_index):
        return self._locations[episode_index]


def test_dry_run_scan_uses_episode_camera_windows_and_reports_matches():
    ctx = _FakeDatasetContext()
    provider_calls = []

    def frame_provider(video_path, *, camera_key, from_timestamp, to_timestamp):
        provider_calls.append((str(video_path), camera_key, from_timestamp, to_timestamp))
        frames = [_solid_frame(30.0) for _ in range(5)]
        if from_timestamp == 12.0:
            frames[2] = frames[2].copy()
            frames[2][:4, :4, :] = 220.0
        return frames

    result = scan_dataset_for_camera_flicker(ctx, frame_provider=frame_provider)

    assert result.dry_run is True
    assert result.inspected_episode_count == 2
    assert result.matched_episode_indices == [1]
    assert result.camera_keys == ["observation.images.cam_head"]
    assert result.tile_grid == (4, 4)
    assert provider_calls == [
        (
            "/datasets/example/videos/observation.images.cam_head/chunk-000/file-003.mp4",
            "observation.images.cam_head",
            12.0,
            13.0,
        ),
        (
            "/datasets/example/videos/observation.images.cam_head/chunk-001/file-002.mp4",
            "observation.images.cam_head",
            20.0,
            21.0,
        ),
    ]


def test_dry_run_scan_preserves_decode_warning_and_continues():
    ctx = _FakeDatasetContext()

    def frame_provider(video_path, *, camera_key, from_timestamp, to_timestamp):
        if from_timestamp == 12.0:
            raise RuntimeError("decode failed")
        return [_solid_frame(30.0) for _ in range(5)]

    result = scan_dataset_for_camera_flicker(ctx, frame_provider=frame_provider)

    assert result.matched_episode_indices == []
    assert result.warnings == [
        "episode 1: cannot decode observation.images.cam_head: decode failed"
    ]


@pytest.mark.asyncio
async def test_mutation_policy_only_overwrites_allowed_grades_and_reports_skips():
    class FakeContext:
        dataset_path = "/datasets/example"

        def get_episodes(self):
            return [
                {"episode_index": 1, "grade": "normal", "tags": ["keep"]},
                {"episode_index": 2, "grade": "good", "tags": ["keep"]},
                {"episode_index": 3, "grade": "bad", "tags": ["keep"]},
                {"episode_index": 4, "grade": None, "tags": ["keep"]},
            ]

    class FakeEpisodeService:
        def __init__(self):
            self.calls = []

        async def bulk_grade(self, ctx, episode_indices, grade, reason=None):
            self.calls.append((ctx, episode_indices, grade, reason))
            return len(episode_indices)

    scan = CameraFlickerScanResult(
        dry_run=False,
        inspected_episode_count=4,
        matched_episode_indices=[1, 2, 3, 4],
        camera_keys=["observation.images.cam_head"],
        tile_grid=(4, 4),
    )
    service = FakeEpisodeService()

    result = await mark_camera_flicker_matches_bad(
        FakeContext(),
        scan,
        episode_service=service,
        allow_overwrite_grades=["normal"],
    )

    assert result.detected_episode_indices == [1, 2, 3, 4]
    assert result.eligible_episode_indices == [1]
    assert result.changed_episode_indices == [1]
    assert result.skipped_episode_indices == [2, 3, 4]
    assert result.skipped_reasons == {
        2: "grade good is not eligible",
        3: "grade bad is not eligible",
        4: "grade None is not eligible",
    }
    assert result.reason == "auto-bad: localized head/ZED camera luminance flicker"
    assert service.calls == [
        (
            service.calls[0][0],
            [1],
            "bad",
            "auto-bad: localized head/ZED camera luminance flicker",
        )
    ]
