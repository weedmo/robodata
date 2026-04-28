from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from backend.datasets.services.dataset_registry import DatasetContext, dataset_registry

router = APIRouter(tags=["videos"])


def _ctx_for_key(dataset_key: str) -> DatasetContext:
    try:
        return dataset_registry.get_by_key(dataset_key)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


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
        video_path = dataset_path / f"videos/{vkey}/chunk-{chunk_idx:03d}/file-{file_idx:03d}.mp4"
        if video_path.exists():
            cameras.append({
                "key": vkey,
                "label": vkey.replace("observation.images.", "").replace("observation.image.", ""),
                "url": f"/api/datasets/{dataset_key}/videos/{episode_index}/stream/{vkey}",
                "from_timestamp": vid_info.get("from_timestamp", 0.0),
                "to_timestamp": vid_info.get("to_timestamp"),
            })
    return cameras


def _stream_response(ctx, episode_index: int, camera_key: str):
    """Stream MP4 video file for an episode camera. Supports range requests via FileResponse."""
    try:
        loc = ctx.get_episode_file_location(episode_index)
    except (KeyError, RuntimeError) as e:
        raise HTTPException(status_code=404, detail=str(e))

    # C1: Validate camera_key against dataset features to prevent path traversal
    features = ctx.get_features()
    video_keys = [
        key for key, meta in features.items()
        if meta.get("dtype") == "video"
    ]
    if camera_key not in video_keys:
        raise HTTPException(status_code=404, detail=f"Unknown camera: {camera_key}")

    dataset_path = Path(ctx.get_dataset_path())
    vid_info = loc.get("videos", {}).get(camera_key, {})
    chunk_idx = vid_info.get("chunk_index", loc["data_chunk_index"])
    file_idx = vid_info.get("file_index", loc["data_file_index"])

    video_path = dataset_path / f"videos/{camera_key}/chunk-{chunk_idx:03d}/file-{file_idx:03d}.mp4"

    # C1: Verify resolved path stays under dataset directory
    if not video_path.resolve().is_relative_to(dataset_path.resolve()):
        raise HTTPException(status_code=400, detail="Invalid camera path")

    if not video_path.exists():
        raise HTTPException(status_code=404, detail=f"Video not found: {camera_key}")

    return FileResponse(
        path=str(video_path),
        media_type="video/mp4",
        filename=f"episode_{episode_index}_{camera_key.replace('/', '_')}.mp4",
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/api/datasets/{dataset_key}/videos/{episode_index}/cameras")
async def list_cameras_by_dataset(dataset_key: str, episode_index: int):
    return _camera_response(_ctx_for_key(dataset_key), episode_index, dataset_key=dataset_key)


@router.get("/api/datasets/{dataset_key}/videos/{episode_index}/stream/{camera_key:path}")
async def stream_video_by_dataset(dataset_key: str, episode_index: int, camera_key: str):
    return _stream_response(_ctx_for_key(dataset_key), episode_index, camera_key)
