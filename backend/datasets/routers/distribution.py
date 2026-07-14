"""Router for dataset distribution analysis endpoints."""

from fastapi import APIRouter, HTTPException

from backend.core.config import settings
from backend.datasets.schemas import (
    DistributionRequest,
    DistributionResponse,
    FieldInfo,
)
from backend.datasets.services.distribution_service import (
    compute_distribution,
    get_available_fields,
)
from backend.datasets.services.path_policy import (
    PathOutsideAllowedRoots,
    require_contained_in_roots,
)

router = APIRouter(prefix="/api/datasets", tags=["distribution"])


def _validate_dataset_path(dataset_path: str) -> None:
    try:
        require_contained_in_roots(dataset_path, settings.allowed_dataset_roots)
    except PathOutsideAllowedRoots as exc:
        raise HTTPException(status_code=403, detail="Access denied: path outside allowed roots") from exc


@router.get("/fields", response_model=list[FieldInfo])
async def list_fields(dataset_path: str):
    """Return all available fields in episode parquet files."""
    _validate_dataset_path(dataset_path)

    try:
        return get_available_fields(dataset_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/distribution", response_model=DistributionResponse)
async def get_distribution(req: DistributionRequest):
    """Compute value distribution for a selected field."""
    _validate_dataset_path(req.dataset_path)

    try:
        return compute_distribution(req.dataset_path, req.field, req.chart_type)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
