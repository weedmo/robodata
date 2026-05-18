"""Job handler for auto-bad marking localized head/ZED camera flicker."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import numpy as np

from backend.datasets.services.camera_flicker_detector import (
    mark_camera_flicker_matches_bad,
    scan_dataset_for_camera_flicker,
)
from backend.datasets.services.camera_flicker_payload import AutoBadCameraFlickerPayload
from backend.datasets.services.dataset_registry import dataset_registry
from backend.datasets.services.episode_service import episode_service
from backend.workers.runtime import CancelledNormally


async def handle_auto_bad_camera_flicker(
    job: Mapping[str, Any],
    *,
    check_cancel=None,
) -> dict[str, object] | CancelledNormally:
    payload = AutoBadCameraFlickerPayload.model_validate(job.get("payload") or {})
    if check_cancel is not None and await check_cancel():
        return CancelledNormally("auto_bad_camera_flicker cancelled before dataset scan")

    ctx = dataset_registry.get(payload.dataset_path)

    scan_result = scan_dataset_for_camera_flicker(
        ctx,
        frame_provider=_decode_video_window_frames,
        tile_grid=payload.tile_grid,
        dry_run=payload.dry_run,
    )
    if check_cancel is not None and await check_cancel():
        return CancelledNormally("auto_bad_camera_flicker cancelled after dataset scan")

    eligible_episode_indices: list[int] = []
    changed_episode_indices: list[int] = []
    skipped_episode_indices: list[int] = []
    if not payload.dry_run:
        mutation = await mark_camera_flicker_matches_bad(
            ctx,
            scan_result,
            episode_service=episode_service,
            allow_overwrite_grades=payload.allow_overwrite_grades,
            reason=payload.reason,
        )
        eligible_episode_indices = mutation.eligible_episode_indices
        changed_episode_indices = mutation.changed_episode_indices
        skipped_episode_indices = mutation.skipped_episode_indices

    return {
        "summary": {
            "dry_run": payload.dry_run,
            "detected": len(scan_result.matched_episode_indices),
            "eligible": len(eligible_episode_indices),
            "changed": len(changed_episode_indices),
            "skipped": len(skipped_episode_indices),
        },
        "detected_episode_indices": scan_result.matched_episode_indices,
        "eligible_episode_indices": eligible_episode_indices,
        "changed_episode_indices": changed_episode_indices,
        "skipped_episode_indices": skipped_episode_indices,
        "warnings": scan_result.warnings,
    }


def _decode_video_window_frames(
    video_path: Path,
    *,
    camera_key: str,
    from_timestamp: float | None,
    to_timestamp: float | None,
) -> list[np.ndarray]:
    try:
        import av
    except ImportError as exc:
        raise RuntimeError("PyAV is required to decode camera flicker videos") from exc

    start_time = float(from_timestamp or 0.0)
    end_time = float(to_timestamp) if to_timestamp is not None else None
    frames: list[np.ndarray] = []
    try:
        with av.open(str(video_path)) as container:
            stream = container.streams.video[0]
            if stream.time_base is not None and start_time > 0:
                target_pts = int(start_time / float(stream.time_base))
                try:
                    container.seek(target_pts, stream=stream, backward=True, any_frame=False)
                except Exception:
                    container.seek(0)
            for frame in container.decode(stream):
                timestamp = frame.time
                if timestamp is not None and timestamp < start_time:
                    continue
                if end_time is not None and timestamp is not None and timestamp > end_time:
                    break
                frames.append(frame.to_ndarray(format="rgb24"))
    except Exception as exc:
        raise RuntimeError(f"PyAV decode failed for {video_path}: {exc}") from exc
    return frames
