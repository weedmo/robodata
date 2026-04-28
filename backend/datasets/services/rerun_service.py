"""Rerun visualization service for LeRobot dataset episodes."""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

try:
    import numpy as np
    import pyarrow.parquet as pq
    import rerun as rr
    HAS_RERUN = True
except ImportError:
    HAS_RERUN = False
    rr = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]
    pq = None  # type: ignore[assignment]

from backend.datasets.services.dataset_registry import dataset_registry
from backend.core.config import settings

logger = logging.getLogger(__name__)
_RERUN_SINK_CONFIGURED = False


def _resolve_video_fps(feature_meta: dict, dataset_fps: float) -> float:
    """Return the best-known fps for a video feature."""
    for key in ("video_info", "info"):
        video_info = feature_meta.get(key)
        if isinstance(video_info, dict):
            value = video_info.get("video.fps")
            if isinstance(value, (int, float)) and value > 0:
                return float(value)
    return dataset_fps if dataset_fps > 0 else 30.0


def ensure_rerun_configured() -> None:
    """Raise a user-facing error when the Rerun SDK has no configured sink."""
    if not HAS_RERUN:
        raise RuntimeError("Rerun SDK is not available")
    if not _RERUN_SINK_CONFIGURED:
        raise RuntimeError("Rerun sink is not configured")


def ensure_rerun_ready() -> None:
    """Backward-compatible alias for the sink configuration check."""
    ensure_rerun_configured()


def _normalize_rerun_grpc_url(url: str) -> str:
    """Convert legacy rerun+grpc URLs into SDK-compatible proxy URLs."""
    if not url.startswith("rerun+grpc://"):
        return url

    parsed = urlsplit(url)
    path = parsed.path if parsed.path not in ("", "/") else "/proxy"
    return urlunsplit(("rerun+http", parsed.netloc, path, parsed.query, parsed.fragment))


def get_rerun_sink_url() -> str:
    """Return the SDK-compatible sink URL used for Rerun streaming."""
    return _normalize_rerun_grpc_url(settings.rerun_grpc_url)


def init_rerun(grpc_port: int, web_port: int) -> None:
    """Initialize the Rerun SDK and connect to the configured gRPC endpoint."""
    global _RERUN_SINK_CONFIGURED
    if not HAS_RERUN:
        raise ImportError("rerun package not installed — install with: pip install rerun-sdk")

    target_url = get_rerun_sink_url()
    _RERUN_SINK_CONFIGURED = False
    rr.init("curation_tools")
    rr.connect_grpc(target_url)
    _RERUN_SINK_CONFIGURED = True
    logger.info(
        "Rerun SDK initialized — configured sink for %s",
        target_url,
    )


def _extract_video_frames(
    video_path: Path, start_frame: int, num_frames: int
) -> list[np.ndarray]:
    """Extract ``num_frames`` RGB frames starting at ``start_frame`` from an MP4.

    Uses PyAV with a by-count decode pass (optionally anchored by a time
    seek) instead of ``cv2.CAP_PROP_POS_FRAMES``. OpenCV's frame-position
    seek is unreliable on AV1 and on stream-copied bundles — the decoder's
    internal counter can drift from the packet index at segment boundaries,
    which surfaced as later-episode content bleeding into earlier episodes
    in Rerun. By-count decode from a keyframe anchor is codec-agnostic and
    matches the upstream lerobot approach.
    """
    try:
        import av
    except ImportError:
        return []

    frames: list[np.ndarray] = []
    try:
        with av.open(str(video_path)) as container:
            stream = container.streams.video[0]
            # Prefer container fps (avg/average/base_rate) and fall back to 30.
            fps_frac = stream.average_rate or stream.base_rate or stream.guessed_rate
            fps = float(fps_frac) if fps_frac else 30.0

            # Seek to a keyframe near the target frame to avoid O(total) decode
            # on large bundled files. AV1 g=2 has keyframes every 2 frames so
            # forward decode after the seek is cheap.
            if start_frame > 0 and stream.time_base is not None:
                target_pts = int(start_frame / fps / float(stream.time_base))
                try:
                    container.seek(target_pts, stream=stream, backward=True, any_frame=False)
                except Exception:
                    container.seek(0)

            current_idx = -1
            for packet_frame in container.decode(stream):
                # Recover the decoder's actual frame index from PTS. This is
                # robust to seek landing at an earlier keyframe: we drop the
                # pre-roll frames by comparing each decoded frame's PTS to the
                # target start time.
                pts = packet_frame.pts
                if pts is None:
                    current_idx += 1
                else:
                    current_idx = int(round(float(pts * stream.time_base) * fps))
                if current_idx < start_frame:
                    continue
                if len(frames) >= num_frames:
                    break
                frames.append(packet_frame.to_ndarray(format="rgb24"))
    except Exception as exc:  # noqa: BLE001 — log + return empty on any decode fault
        logger.warning("PyAV extract failed for %s: %s", video_path, exc)
        return []

    return frames


def _log_scalar_columns(entity_prefix: str, row: dict, columns: list[str]) -> None:
    """Log numeric columns as Rerun Scalar time-series entities.

    Multi-dimensional arrays (e.g. 6-DOF state) are split into separate
    per-dimension scalar entities so each dimension gets its own timeline.
    """
    for col in columns:
        value = row.get(col)
        if value is None:
            continue
        arr = np.asarray(value, dtype=float).ravel()
        entity = f"{entity_prefix}/{col}"
        if arr.size == 1:
            rr.log(entity, rr.Scalar(float(arr[0])))
        elif arr.size > 1:
            for i, val in enumerate(arr):
                rr.log(f"{entity}/{i}", rr.Scalar(float(val)))


def _resolve_episode_rows(
    df: dict[str, list],
    from_idx: int,
    to_idx: int,
    all_columns: list[str],
) -> list[int]:
    """Return file-local row positions for an episode slice."""
    if not all_columns:
        return []

    row_count = len(df.get(all_columns[0], []))
    if row_count == 0:
        return []

    if "index" in df:
        positions = [
            position
            for position, value in enumerate(df["index"])
            if value is not None and from_idx <= int(value) < to_idx
        ]
        if positions:
            if "frame_index" in df:
                positions.sort(key=lambda pos: int(df["frame_index"][pos]))
            return positions

    start = max(0, min(from_idx, row_count))
    stop = max(start, min(to_idx, row_count))
    return list(range(start, stop))


async def visualize_episode(dataset_path: str, episode_index: int) -> None:
    """Visualize a single episode in Rerun."""
    import asyncio

    ensure_rerun_configured()
    ctx = dataset_registry.get(dataset_path)

    loc = ctx.get_episode_file_location(episode_index)
    dataset_path = Path(ctx.get_dataset_path())
    dataset_info = ctx.get_info()
    features = ctx.get_features()

    from_idx = loc["dataset_from_index"]
    to_idx = loc["dataset_to_index"]
    chunk_idx = loc["data_chunk_index"]
    file_idx = loc["data_file_index"]

    # Clear previous visualization. Must target every entity root this function
    # writes to: a mismatched path (e.g. "world") leaves leftover scalars and
    # video frames from the previous episode on the shared "frame" timeline,
    # which then bleed into this episode wherever it has fewer frames.
    for root in ("observation", "action", "camera"):
        rr.log(root, rr.Clear(recursive=True))

    # Read data parquet file
    data_path = dataset_path / f"data/chunk-{chunk_idx:03d}/file-{file_idx:03d}.parquet"
    if not data_path.exists():
        raise FileNotFoundError(f"Data parquet not found: {data_path}")

    table = await asyncio.to_thread(pq.read_table, data_path)
    df = table.to_pydict()
    all_columns = list(df.keys())

    # Classify columns by feature type
    image_columns: list[str] = []
    state_columns: list[str] = []
    action_columns: list[str] = []

    for col, feature in features.items():
        dtype = feature.get("dtype", "")
        if dtype in ("image", "video") or col.startswith("observation.image"):
            image_columns.append(col)
        elif col.startswith("observation.") and col in all_columns:
            state_columns.append(col)
        elif col.startswith("action") and col in all_columns:
            action_columns.append(col)

    # Skip remaining columns — only log explicitly classified observation/action columns
    # to avoid polluting visualizations with metadata like timestamp, index, etc.

    # Per-frame scalar logging
    row_positions = _resolve_episode_rows(df, from_idx, to_idx, all_columns)
    num_frames = len(row_positions) if row_positions else max(0, to_idx - from_idx)
    for sequence, row_position in enumerate(row_positions):
        rr.set_time("frame", sequence=sequence)
        row = {col: df[col][row_position] for col in all_columns if row_position < len(df[col])}
        _log_scalar_columns("observation", row, state_columns)
        _log_scalar_columns("action", row, action_columns)

    # Video frame logging
    video_features = {
        col: meta for col, meta in features.items()
        if meta.get("dtype") == "video"
    }

    for vkey, _meta in video_features.items():
        vid_info = loc.get("videos", {}).get(vkey, {})
        vid_chunk = vid_info.get("chunk_index", chunk_idx)
        vid_file = vid_info.get("file_index", file_idx)
        video_start_ts = float(vid_info.get("from_timestamp") or 0.0)
        video_fps = _resolve_video_fps(_meta, float(dataset_info.get("fps") or 0))
        start_frame = max(0, int(round(video_start_ts * video_fps)))

        video_path = dataset_path / f"videos/{vkey}/chunk-{vid_chunk:03d}/file-{vid_file:03d}.mp4"
        if not video_path.exists():
            logger.warning("Video not found, skipping %s: %s", vkey, video_path)
            continue

        try:
            frames = _extract_video_frames(video_path, start_frame, num_frames)
        except Exception as exc:
            logger.warning("Video extraction failed for %s: %s", vkey, exc)
            continue

        if not frames:
            continue

        entity = f"camera/{vkey.replace('.', '/')}"
        for i, frame_rgb in enumerate(frames):
            rr.set_time("frame", sequence= i)
            rr.log(entity, rr.Image(frame_rgb))

    logger.info("Visualized episode %d (%d frames)", episode_index, num_frames)
