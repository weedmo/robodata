from fastapi import APIRouter, HTTPException, Query

from backend.datasets.schemas import BulkGradeRequest, Episode, EpisodeUpdate
from backend.datasets.services.dataset_registry import dataset_registry
from backend.datasets.services.episode_service import episode_service, EpisodeNotFoundError
from backend.datasets.services.raw_dataset_adapter import is_raw_task_dir, load_raw_context

router = APIRouter(prefix="/api/episodes", tags=["episodes"])


def _ctx_for(dataset_path: str):
    if is_raw_task_dir(dataset_path):
        return load_raw_context(dataset_path)
    return dataset_registry.get(dataset_path)


@router.get("", response_model=list[Episode])
async def list_episodes(dataset_path: str = Query(...)):
    try:
        return await episode_service.get_episodes(_ctx_for(dataset_path))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{episode_index}", response_model=Episode)
async def get_episode(episode_index: int, dataset_path: str = Query(...)):
    try:
        return await episode_service.get_episode(_ctx_for(dataset_path), episode_index=episode_index)
    except EpisodeNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/{episode_index}", response_model=Episode)
async def update_episode(
    episode_index: int,
    update: EpisodeUpdate,
):
    try:
        ctx = _ctx_for(update.dataset_path)
        # C3: When tags not provided, preserve existing tags instead of erasing
        if update.tags is not None:
            tags = update.tags
        else:
            current = await episode_service.get_episode(ctx, episode_index=episode_index)
            tags = current.get("tags", [])
        return await episode_service.update_episode(
            ctx,
            episode_index=episode_index,
            grade=update.grade,
            tags=tags,
            reason=update.reason,
        )
    except EpisodeNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/bulk-grade")
async def bulk_grade_episodes(req: BulkGradeRequest):
    try:
        ctx = _ctx_for(req.dataset_path)
        count = await episode_service.bulk_grade(
            ctx,
            req.episode_indices, req.grade, reason=req.reason,
        )
        return {"updated": count}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
