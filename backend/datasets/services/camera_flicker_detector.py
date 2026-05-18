"""Localized camera flicker detection primitives.

This module is intentionally pure: it accepts decoded frames and returns a
structured verdict. Dataset I/O, PyAV decoding, and annotation mutation live in
later integration layers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

DEFAULT_CAMERA_FLICKER_BAD_REASON = "auto-bad: localized head/ZED camera luminance flicker"


@dataclass(frozen=True)
class FlickerDetectionResult:
    matched: bool
    spike_count: int
    max_tile_delta: float
    spike_frame_indices: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class CameraFlickerScanResult:
    dry_run: bool
    inspected_episode_count: int
    matched_episode_indices: list[int]
    camera_keys: list[str]
    tile_grid: tuple[int, int]
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CameraFlickerMutationResult:
    detected_episode_indices: list[int]
    eligible_episode_indices: list[int]
    changed_episode_indices: list[int]
    skipped_episode_indices: list[int]
    skipped_reasons: dict[int, str]
    reason: str
    allow_overwrite_grades: list[str]


def is_head_or_zed_camera_key(camera_key: str) -> bool:
    """Return true for camera keys that identify head/ZED streams."""
    normalized = camera_key.lower()
    return (
        "cam_head" in normalized
        or "head" in normalized
        or "zed" in normalized
    )


def detect_localized_luminance_flicker(
    frames: Sequence[np.ndarray] | Iterable[np.ndarray],
    *,
    tile_grid: tuple[int, int] = (4, 4),
    min_tile_delta: float = 12.0,
    robust_mad_multiplier: float = 8.0,
    max_spiking_tile_fraction: float = 0.5,
) -> FlickerDetectionResult:
    """Detect localized luminance spikes across decoded RGB/gray frames.

    A transition is considered localized flicker when at least one tile jumps
    above the robust threshold, but fewer than a configured fraction of all
    tiles jump together. That rejects uniform exposure changes while preserving
    the head/ZED artifact pattern observed as spatially localized flashes.
    """
    tile_rows, tile_cols = tile_grid
    if tile_rows <= 0 or tile_cols <= 0:
        raise ValueError("tile_grid dimensions must be positive")

    tile_means = [_frame_tile_luminance(frame, tile_rows, tile_cols) for frame in frames]
    if len(tile_means) < 2:
        return FlickerDetectionResult(False, 0, 0.0, [])

    deltas = np.abs(np.diff(np.asarray(tile_means, dtype=np.float32), axis=0))
    max_tile_delta = float(np.max(deltas)) if deltas.size else 0.0
    if not deltas.size:
        return FlickerDetectionResult(False, 0, 0.0, [])

    flattened = deltas.reshape(-1)
    median = float(np.median(flattened))
    mad = float(np.median(np.abs(flattened - median))) or 1e-6
    threshold = max(min_tile_delta, median + robust_mad_multiplier * mad)

    tile_count = tile_rows * tile_cols
    spike_frame_indices: list[int] = []
    for transition_idx, transition_deltas in enumerate(deltas):
        spiking_tiles = int(np.count_nonzero(transition_deltas > threshold))
        if spiking_tiles == 0:
            continue
        if spiking_tiles / tile_count > max_spiking_tile_fraction:
            continue
        spike_frame_indices.append(transition_idx + 1)

    return FlickerDetectionResult(
        matched=bool(spike_frame_indices),
        spike_count=len(spike_frame_indices),
        max_tile_delta=max_tile_delta,
        spike_frame_indices=spike_frame_indices,
    )


def scan_dataset_for_camera_flicker(
    dataset_context,
    *,
    frame_provider,
    tile_grid: tuple[int, int] = (4, 4),
    dry_run: bool = True,
) -> CameraFlickerScanResult:
    """Scan head/ZED video windows and report matching episodes without writes."""
    features = dataset_context.get_features()
    camera_keys = [
        key
        for key, feature in features.items()
        if feature.get("dtype") == "video" and is_head_or_zed_camera_key(key)
    ]

    dataset_path = Path(dataset_context.get_dataset_path())
    matched_episode_indices: list[int] = []
    warnings: list[str] = []
    inspected_episode_count = 0

    for episode in dataset_context.get_episodes():
        episode_index = int(episode["episode_index"])
        inspected_episode_count += 1
        location = dataset_context.get_episode_file_location(episode_index)
        videos = location.get("videos", {})

        for camera_key in camera_keys:
            video_location = videos.get(camera_key)
            if not video_location:
                warnings.append(f"episode {episode_index}: missing video location for {camera_key}")
                continue

            video_path = _episode_video_path(dataset_path, camera_key, video_location)
            try:
                frames = frame_provider(
                    video_path,
                    camera_key=camera_key,
                    from_timestamp=video_location.get("from_timestamp"),
                    to_timestamp=video_location.get("to_timestamp"),
                )
            except Exception as exc:  # noqa: BLE001 - keep scanning while preserving decode evidence
                warnings.append(f"episode {episode_index}: cannot decode {camera_key}: {exc}")
                continue
            verdict = detect_localized_luminance_flicker(frames, tile_grid=tile_grid)
            if verdict.matched:
                matched_episode_indices.append(episode_index)
                break

    return CameraFlickerScanResult(
        dry_run=dry_run,
        inspected_episode_count=inspected_episode_count,
        matched_episode_indices=matched_episode_indices,
        camera_keys=camera_keys,
        tile_grid=tile_grid,
        warnings=warnings,
    )


async def mark_camera_flicker_matches_bad(
    dataset_context,
    scan_result: CameraFlickerScanResult,
    *,
    episode_service,
    allow_overwrite_grades: Sequence[str] = ("normal",),
    reason: str = DEFAULT_CAMERA_FLICKER_BAD_REASON,
) -> CameraFlickerMutationResult:
    """Mark eligible detected flicker episodes bad through EpisodeService."""
    allowed = {grade.lower() for grade in allow_overwrite_grades}
    detected = list(scan_result.matched_episode_indices)
    episodes_by_index = {
        int(episode["episode_index"]): episode
        for episode in dataset_context.get_episodes()
    }

    eligible: list[int] = []
    skipped_reasons: dict[int, str] = {}
    for episode_index in detected:
        grade = episodes_by_index.get(episode_index, {}).get("grade")
        normalized_grade = grade.lower() if isinstance(grade, str) else grade
        if normalized_grade in allowed:
            eligible.append(episode_index)
            continue
        skipped_reasons[episode_index] = f"grade {grade} is not eligible"

    changed_count = 0
    if eligible:
        changed_count = await episode_service.bulk_grade(
            dataset_context,
            eligible,
            "bad",
            reason=reason,
        )

    changed = eligible[:changed_count]
    return CameraFlickerMutationResult(
        detected_episode_indices=detected,
        eligible_episode_indices=eligible,
        changed_episode_indices=changed,
        skipped_episode_indices=[idx for idx in detected if idx not in eligible],
        skipped_reasons=skipped_reasons,
        reason=reason,
        allow_overwrite_grades=list(allow_overwrite_grades),
    )


def _episode_video_path(dataset_path: Path, camera_key: str, video_location: dict) -> Path:
    chunk_index = int(video_location["chunk_index"])
    file_index = int(video_location["file_index"])
    return (
        dataset_path
        / "videos"
        / camera_key
        / f"chunk-{chunk_index:03d}"
        / f"file-{file_index:03d}.mp4"
    )


def _frame_tile_luminance(frame: np.ndarray, tile_rows: int, tile_cols: int) -> list[float]:
    arr = np.asarray(frame, dtype=np.float32)
    if arr.ndim == 3:
        if arr.shape[2] < 3:
            raise ValueError("RGB frames must have at least three channels")
        luminance = (
            0.299 * arr[:, :, 0]
            + 0.587 * arr[:, :, 1]
            + 0.114 * arr[:, :, 2]
        )
    elif arr.ndim == 2:
        luminance = arr
    else:
        raise ValueError("frames must be 2-D grayscale or 3-D RGB arrays")

    height, width = luminance.shape
    if height < tile_rows or width < tile_cols:
        raise ValueError("frame is smaller than tile_grid")

    means: list[float] = []
    for row in range(tile_rows):
        y0 = row * height // tile_rows
        y1 = (row + 1) * height // tile_rows
        for col in range(tile_cols):
            x0 = col * width // tile_cols
            x1 = (col + 1) * width // tile_cols
            means.append(float(np.mean(luminance[y0:y1, x0:x1])))
    return means
