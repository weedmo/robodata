from fastapi import APIRouter, HTTPException, Query

from backend.datasets.schemas import BulkGradeRequest, Episode, EpisodeUpdate, EpisodeInstructionRequest
from backend.datasets.services.dataset_registry import dataset_registry
from backend.datasets.services.episode_service import episode_service, EpisodeNotFoundError
from backend.datasets.services.raw_dataset_adapter import is_raw_task_dir, load_raw_context
from backend.datasets.services import episode_instruction_service

router = APIRouter(prefix="/api/episodes", tags=["episodes"])


def _instruction_error(error: Exception) -> HTTPException:
    if isinstance(error, KeyError):
        return HTTPException(404, detail={"code": "episode_not_found", "message": str(error)})
    if isinstance(error, episode_instruction_service.InstructionConflictError):
        return HTTPException(409, detail={"code": error.code, "message": str(error)})
    return HTTPException(400, detail=str(error))


def _ctx_for(dataset_path: str):
    if is_raw_task_dir(dataset_path):
        return load_raw_context(dataset_path)
    return dataset_registry.get(dataset_path)


def _require_lerobot_v3(ctx) -> None:
    root = ctx.dataset_path
    version = str(ctx.info.get("codebase_version", ""))
    if not version.startswith("v3") or not (root / "meta/tasks.parquet").is_file() or not any((root / "meta/episodes").glob("chunk-*/file-*.parquet")) or not any((root / "data").glob("chunk-*/file-*.parquet")):
        raise HTTPException(409, detail={"code": "unsupported_dataset", "message": "Instruction editing requires a LeRobot v3 dataset"})


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


@router.post("/{episode_index}/instruction-preview")
async def preview_instruction(episode_index: int, request: EpisodeInstructionRequest):
    if is_raw_task_dir(request.dataset_path):
        raise HTTPException(409, detail={"code": "raw_read_only", "message": "Raw datasets are read-only"})
    try:
        ctx = _ctx_for(request.dataset_path)
        _require_lerobot_v3(ctx)
        root = ctx.dataset_path
        async with ctx.get_file_lock("__episode_instruction_mutation__"):
            with episode_instruction_service.dataset_lock(root):
                episode_instruction_service.recover(root)
                return episode_instruction_service.preview(root, episode_index, request.instruction, request.mode)
    except Exception as error:
        raise _instruction_error(error)


@router.post("/{episode_index}/instruction")
async def commit_instruction(episode_index: int, request: EpisodeInstructionRequest):
    if is_raw_task_dir(request.dataset_path):
        raise HTTPException(409, detail={"code": "raw_read_only", "message": "Raw datasets are read-only"})
    if request.fingerprint is None:
        raise HTTPException(409, detail={"code": "instruction_preview_stale", "message": "Preview before saving"})
    try:
        ctx = _ctx_for(request.dataset_path)
        _require_lerobot_v3(ctx)
        root = ctx.dataset_path
        async with ctx.get_file_lock("__episode_instruction_mutation__"):
            with episode_instruction_service.dataset_lock(root):
                episode_instruction_service.recover(root)
                result = episode_instruction_service.commit(root, episode_index, request.instruction, request.mode, request.fingerprint, request.confirm_shared)
            dataset_registry.invalidate(request.dataset_path)
            fresh = _ctx_for(request.dataset_path)
            episode = await episode_service.get_episode(fresh, episode_index=episode_index)
        task_index = int(episode["task_index"])
        task = next((item for item in fresh.get_tasks() if int(item["task_index"]) == int(task_index)), None)
        return {"episode": episode, "task": task, "action": result["action"], "affected_episode_count": result["affected_episode_count"]}
    except Exception as error:
        raise _instruction_error(error)


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
