from fastapi import APIRouter, HTTPException, Query

from backend.converter import service as converter_service
from backend.datasets.services import rerun_service

router = APIRouter(prefix="/api/rerun", tags=["rerun"])


@router.post("/visualize/{episode_index}")
async def visualize_episode(episode_index: int, dataset_path: str = Query(...)):
    try:
        await rerun_service.visualize_episode(dataset_path, episode_index)
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=404, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"status": "ok", "episode_index": episode_index}


@router.post("/visualize-raw")
async def visualize_raw(recording: str = Query(...)):
    """Stream a raw rosbag recording into the shared Rerun viewer.

    ``recording`` is a path relative to the raw NAS root whose last component is
    the serial (1 mcap = 1 episode). The extraction runs inside the converter
    container and streams to the same sink the lerobot viewer uses.
    """
    try:
        ok, message = await converter_service.visualize_raw_recording(recording)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=502, detail=message)
    return {"status": "ok", "recording": recording, "detail": message}
