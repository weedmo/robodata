from fastapi import APIRouter, HTTPException, Query

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
