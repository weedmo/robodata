from fastapi import APIRouter, HTTPException

from backend.datasets.schemas import BulkGradeRequest, Episode, EpisodeUpdate
from backend.datasets.services.episode_service import episode_service, EpisodeNotFoundError

router = APIRouter(prefix="/api/episodes", tags=["episodes"])


@router.get("", response_model=list[Episode])
async def list_episodes():
    try:
        return await episode_service.get_episodes()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{episode_index}", response_model=Episode)
async def get_episode(episode_index: int):
    try:
        return await episode_service.get_episode(episode_index)
    except EpisodeNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/{episode_index}", response_model=Episode)
async def update_episode(episode_index: int, update: EpisodeUpdate):
    try:
        # C3: When tags not provided, preserve existing tags instead of erasing
        if update.tags is not None:
            tags = update.tags
        else:
            current = await episode_service.get_episode(episode_index)
            tags = current.get("tags", [])
        return await episode_service.update_episode(
            episode_index=episode_index,
            grade=update.grade,
            tags=tags,
            reason=update.reason,
        )
    except EpisodeNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/bulk-grade")
async def bulk_grade_episodes(req: BulkGradeRequest):
    try:
        count = await episode_service.bulk_grade(
            req.episode_indices, req.grade, reason=req.reason,
        )
        return {"updated": count}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
