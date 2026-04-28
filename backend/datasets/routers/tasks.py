from fastapi import APIRouter, HTTPException, Query

from backend.datasets.schemas import Task, TaskUpdate
from backend.datasets.services.dataset_registry import dataset_registry
from backend.datasets.services import task_service

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _ctx_for(dataset_path: str | None):
    return dataset_registry.get(dataset_path) if dataset_path else None


@router.get("", response_model=list[Task])
async def list_tasks(dataset_path: str | None = Query(None)):
    try:
        items = task_service.get_tasks(_ctx_for(dataset_path))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return [Task(task_index=t["task_index"], task_instruction=t["task_instruction"]) for t in items]


@router.patch("/{task_index}", response_model=Task)
async def update_task(
    task_index: int,
    update: TaskUpdate,
    dataset_path: str | None = Query(None),
):
    try:
        result = await task_service.update_task(
            task_index,
            update.task_instruction,
            _ctx_for(update.dataset_path or dataset_path),
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return Task(task_index=result["task_index"], task_instruction=result["task_instruction"])
