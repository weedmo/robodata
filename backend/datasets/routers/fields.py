"""Router for dataset field management endpoints."""

from fastapi import APIRouter, HTTPException

from backend.core.config import settings
from backend.datasets.schemas import EpisodeColumnAdd, InfoFieldUpdate
from backend.datasets.services.fields_service import (
    add_episode_column,
    delete_info_field,
    get_episode_columns,
    get_info_fields,
    update_info_field,
)
from backend.datasets.services.path_policy import (
    PathOutsideAllowedRoots,
    require_contained_in_roots,
)
from backend.datasets.services.raw_dataset_adapter import (
    RAW_READ_ONLY_ERROR,
    is_raw_task_dir,
    raw_episode_columns,
    raw_info_fields,
)

router = APIRouter(prefix="/api/datasets", tags=["fields"])


def _validate_path(dataset_path: str) -> None:
    try:
        require_contained_in_roots(dataset_path, settings.allowed_dataset_roots)
    except PathOutsideAllowedRoots as exc:
        raise HTTPException(status_code=403, detail="Access denied: path outside allowed roots") from exc


@router.get("/info-fields")
async def list_info_fields(dataset_path: str):
    """Return all fields from info.json."""
    _validate_path(dataset_path)
    if is_raw_task_dir(dataset_path):
        return raw_info_fields(dataset_path)
    try:
        return get_info_fields(dataset_path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Dataset not found")


@router.patch("/info-fields")
async def update_info(dataset_path: str, req: InfoFieldUpdate):
    """Add or update a custom field in info.json."""
    _validate_path(dataset_path)
    if is_raw_task_dir(dataset_path):
        raise HTTPException(status_code=409, detail=RAW_READ_ONLY_ERROR)
    try:
        if req.value is None:
            delete_info_field(dataset_path, req.key)
        else:
            update_info_field(dataset_path, req.key, req.value)
        return {"status": "ok"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/episode-columns")
async def list_episode_columns(dataset_path: str):
    """Return all columns from episode parquet files."""
    _validate_path(dataset_path)
    if is_raw_task_dir(dataset_path):
        return raw_episode_columns(dataset_path)
    return get_episode_columns(dataset_path)


@router.post("/episode-columns")
async def add_column(req: EpisodeColumnAdd):
    """Add a new column to all episode parquet files."""
    _validate_path(req.dataset_path)
    if is_raw_task_dir(req.dataset_path):
        raise HTTPException(status_code=409, detail=RAW_READ_ONLY_ERROR)
    try:
        add_episode_column(req.dataset_path, req.column_name, req.dtype, req.default_value)
        return {"status": "ok"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
