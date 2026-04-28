from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from backend.datasets.services.dataset_registry import DatasetContext, dataset_registry

router = APIRouter(tags=["videos"])
VIDEO_CACHE_CONTROL = "private, max-age=604800, immutable"


def _ctx_for_key(dataset_key: str) -> DatasetContext:
    try:
        return dataset_registry.get_by_key(dataset_key)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


def _safe_float(value) -> float | None:
    try:
        if value is None:
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _episode_window(vid_info: dict) -> tuple[float, float | None]:
    start = _safe_float(vid_info.get("from_timestamp")) or 0.0
    end = _safe_float(vid_info.get("to_timestamp"))
    if end is None or end <= start:
        return start, None
    return start, end


def _validate_camera_key(ctx, camera_key: str) -> None:
    features = ctx.get_features()
    video_keys = [
        key for key, meta in features.items()
        if meta.get("dtype") == "video"
    ]
    if camera_key not in video_keys:
        raise HTTPException(status_code=404, detail=f"Unknown camera: {camera_key}")


def _video_file_path(ctx, camera_key: str, chunk_idx: int, file_idx: int) -> Path:
    _validate_camera_key(ctx, camera_key)
    dataset_path = Path(ctx.get_dataset_path())
    video_path = dataset_path / f"videos/{camera_key}/chunk-{chunk_idx:03d}/file-{file_idx:03d}.mp4"

    if not video_path.resolve().is_relative_to(dataset_path.resolve()):
        raise HTTPException(status_code=400, detail="Invalid camera path")

    if not video_path.exists():
        raise HTTPException(status_code=404, detail=f"Video not found: {camera_key}")
    return video_path


def _video_cache_token(video_path: Path) -> str:
    stat = video_path.stat()
    return f"{stat.st_mtime_ns:x}-{stat.st_size:x}"


def _video_file_url(
    dataset_key: str | None,
    camera_key: str,
    chunk_idx: int,
    file_idx: int,
    video_path: Path,
) -> str:
    token = _video_cache_token(video_path)
    return (
        f"/api/datasets/{dataset_key}/videos/file/{chunk_idx}/{file_idx}/"
        f"{camera_key}?v={token}"
    )


def _file_response(
    video_path: Path,
    *,
    filename: str,
    cache_control: str = VIDEO_CACHE_CONTROL,
) -> FileResponse:
    return FileResponse(
        path=str(video_path),
        media_type="video/mp4",
        filename=filename,
        headers={"Cache-Control": cache_control},
    )


def _camera_response(ctx, episode_index: int, *, dataset_key: str | None = None):
    """Return available camera keys for an episode."""
    try:
        loc = ctx.get_episode_file_location(episode_index)
    except (KeyError, RuntimeError) as e:
        raise HTTPException(status_code=404, detail=str(e))

    features = ctx.get_features()
    video_keys = [
        key for key, meta in features.items()
        if meta.get("dtype") == "video"
    ]

    dataset_path = Path(ctx.get_dataset_path())
    cameras = []
    for vkey in video_keys:
        vid_info = loc.get("videos", {}).get(vkey, {})
        chunk_idx = vid_info.get("chunk_index", loc["data_chunk_index"])
        file_idx = vid_info.get("file_index", loc["data_file_index"])
        start, end = _episode_window(vid_info)
        video_path = dataset_path / f"videos/{vkey}/chunk-{int(chunk_idx):03d}/file-{int(file_idx):03d}.mp4"
        if not video_path.resolve().is_relative_to(dataset_path.resolve()):
            continue
        if not video_path.exists():
            continue
        cameras.append({
            "key": vkey,
            "label": vkey.replace("observation.images.", "").replace("observation.image.", ""),
            "url": _video_file_url(dataset_key, vkey, int(chunk_idx), int(file_idx), video_path),
            "from_timestamp": start,
            "to_timestamp": end,
        })
    return cameras


def _stream_response(ctx, episode_index: int, camera_key: str):
    """Stream the source MP4 for an episode camera. Supports range requests via FileResponse."""
    try:
        loc = ctx.get_episode_file_location(episode_index)
    except (KeyError, RuntimeError) as e:
        raise HTTPException(status_code=404, detail=str(e))

    vid_info = loc.get("videos", {}).get(camera_key, {})
    chunk_idx = vid_info.get("chunk_index", loc["data_chunk_index"])
    file_idx = vid_info.get("file_index", loc["data_file_index"])
    video_path = _video_file_path(ctx, camera_key, int(chunk_idx), int(file_idx))

    return _file_response(
        video_path,
        filename=f"episode_{episode_index}_{camera_key.replace('/', '_')}.mp4",
    )


def _stream_file_response(ctx, chunk_index: int, file_index: int, camera_key: str):
    video_path = _video_file_path(ctx, camera_key, chunk_index, file_index)
    return _file_response(
        video_path,
        filename=f"{camera_key.replace('/', '_')}_chunk-{chunk_index:03d}_file-{file_index:03d}.mp4",
    )


@router.get("/api/datasets/{dataset_key}/videos/{episode_index}/cameras")
def list_cameras_by_dataset(dataset_key: str, episode_index: int):
    return _camera_response(_ctx_for_key(dataset_key), episode_index, dataset_key=dataset_key)


@router.get("/api/datasets/{dataset_key}/videos/{episode_index}/stream/{camera_key:path}")
def stream_video_by_dataset(dataset_key: str, episode_index: int, camera_key: str):
    return _stream_response(_ctx_for_key(dataset_key), episode_index, camera_key)


@router.get("/api/datasets/{dataset_key}/videos/file/{chunk_index}/{file_index}/{camera_key:path}")
def stream_video_file_by_dataset(dataset_key: str, chunk_index: int, file_index: int, camera_key: str):
    return _stream_file_response(_ctx_for_key(dataset_key), chunk_index, file_index, camera_key)
