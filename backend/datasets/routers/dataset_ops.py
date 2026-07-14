"""FastAPI router for dataset split/merge operations."""

from pathlib import Path
from typing import Awaitable, Callable

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, field_validator

from backend.config import settings
from backend.datasets.services.cycle_stamp_service import describe_stamp_state
from backend.datasets.services.dataset_ops_engine import read_info
from backend.datasets.services.operation_intake import (
    DatasetJobNotFound,
    DuplicateDatasetOperation,
    EnqueuedDatasetOperation,
    OperationIntakeError,
    ProjectedJobStatus,
    SameSourceAndDestination,
    SourcePathNotFound,
    fetch_projected_job_status,
    intake_delete,
    intake_merge,
    intake_split,
    intake_split_into,
    intake_stamp_cycles,
)
from backend.datasets.services.path_policy import (
    PathOutsideAllowedRoots,
    is_contained_in,
    normalize_roots,
    require_contained_in_roots,
)


def _validate_path(path_str: str) -> Path:
    """Resolve a dataset path and ensure it stays under an allowed root."""
    try:
        return require_contained_in_roots(path_str, settings.allowed_dataset_roots).path
    except PathOutsideAllowedRoots:
        raise HTTPException(status_code=400, detail=f"Path outside allowed roots: {path_str}")


def _validate_optional_path(path_str: str | None) -> str | None:
    """Resolve an optional dataset path when present."""
    if path_str is None:
        return None
    return str(_validate_path(path_str))


def _coerce_summary_int(field_name: str, value: object) -> int:
    """Convert summary numeric metadata into ints with a controlled HTTP error."""
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid {field_name} in info.json") from exc


def _operation_response(operation: EnqueuedDatasetOperation) -> "JobResponse":
    return JobResponse(job_id=operation.job_id, operation=operation.operation, status=operation.status)


def _status_response(status: ProjectedJobStatus) -> "JobStatusResponse":
    return JobStatusResponse(
        job_id=status.job_id,
        operation=status.operation,
        status=status.status,
        created_at=status.created_at,
        completed_at=status.completed_at,
        error=status.error,
        result_path=status.result_path,
        summary=status.summary,
    )


def _translate_intake_error(exc: OperationIntakeError) -> HTTPException:
    if isinstance(exc, DuplicateDatasetOperation):
        return HTTPException(
            status_code=409,
            detail={"error": "duplicate_dedupe_key", "existing_job_id": exc.existing_job_id},
        )
    if isinstance(exc, SourcePathNotFound):
        return HTTPException(status_code=404, detail=f"Source path not found: {exc.requested_path}")
    if isinstance(exc, SameSourceAndDestination):
        return HTTPException(status_code=400, detail="source and destination must differ")
    if isinstance(exc, DatasetJobNotFound):
        return HTTPException(status_code=404, detail=f"Job not found: {exc.job_id}")
    return HTTPException(status_code=500, detail=str(exc))


async def _run_intake(operation_factory: Callable[[], Awaitable[EnqueuedDatasetOperation]]) -> "JobResponse":
    try:
        operation = await operation_factory()
    except OperationIntakeError as exc:
        raise _translate_intake_error(exc) from exc
    return _operation_response(operation)


router = APIRouter(prefix="/api/datasets", tags=["dataset-ops"])


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class SplitRequest(BaseModel):
    source_path: str
    episode_ids: list[int]
    target_name: str
    output_dir: str | None = None  # If omitted, sibling of source_path

    @field_validator("episode_ids")
    @classmethod
    def episode_ids_nonempty(cls, v: list[int]) -> list[int]:
        if not v:
            raise ValueError("episode_ids must not be empty")
        return v


class SplitIntoRequest(BaseModel):
    source_path: str
    episode_ids: list[int]
    destination_path: str

    @field_validator("episode_ids")
    @classmethod
    def episode_ids_nonempty(cls, v: list[int]) -> list[int]:
        if not v:
            raise ValueError("episode_ids must not be empty")
        return v

    @field_validator("destination_path")
    @classmethod
    def destination_path_must_be_absolute(cls, v: str) -> str:
        if not Path(v).is_absolute():
            raise ValueError("destination_path must be absolute")
        return v


class DeleteRequest(BaseModel):
    source_path: str
    episode_ids: list[int]
    output_dir: str | None = None  # If omitted, overwrites source in-place

    @field_validator("episode_ids")
    @classmethod
    def episode_ids_nonempty(cls, v: list[int]) -> list[int]:
        if not v:
            raise ValueError("episode_ids must not be empty")
        return v


class MergeRequest(BaseModel):
    source_paths: list[str]
    target_name: str
    output_dir: str | None = None  # If omitted, sibling of first source_path


class StampCyclesRequest(BaseModel):
    source_path: str
    overwrite: bool = False


class JobResponse(BaseModel):
    job_id: str
    operation: str
    status: str


class JobStatusResponse(BaseModel):
    job_id: str
    operation: str
    status: str
    created_at: str
    completed_at: str | None = None
    error: str | None = None
    result_path: str | None = None
    summary: dict[str, str | int] | None = None


class StampCyclesStatusResponse(BaseModel):
    stamped: bool
    is_terminal_count_sample: int


class BrowseDirEntry(BaseModel):
    name: str
    path: str
    is_lerobot_dataset: bool


class BrowseDirsResponse(BaseModel):
    path: str
    parent: str | None
    roots: list[str]
    entries: list[BrowseDirEntry]


class SummaryResponse(BaseModel):
    path: str
    total_episodes: int
    robot_type: str | None
    fps: int
    features_count: int


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/split", response_model=JobResponse, status_code=202)
async def split_dataset(req: SplitRequest):
    """Split episodes from a source dataset into a new derived dataset."""
    return await _run_intake(
        lambda: intake_split(
            req,
            validate_path=_validate_path,
            validate_optional_path=_validate_optional_path,
        )
    )


@router.post("/split-into", response_model=JobResponse, status_code=202)
async def split_into_dataset(req: SplitIntoRequest):
    """Sync selected good episodes to one absolute destination path."""
    return await _run_intake(lambda: intake_split_into(req, validate_path=_validate_path))


@router.post("/merge", response_model=JobResponse, status_code=202)
async def merge_datasets(req: MergeRequest):
    """Merge multiple source datasets into a new derived dataset."""
    return await _run_intake(
        lambda: intake_merge(
            req,
            validate_path=_validate_path,
            validate_optional_path=_validate_optional_path,
        )
    )


@router.post("/delete", response_model=JobResponse, status_code=202)
async def delete_episodes(req: DeleteRequest):
    """Delete specified episodes from a dataset, producing a new dataset."""
    return await _run_intake(
        lambda: intake_delete(
            req,
            validate_path=_validate_path,
            validate_optional_path=_validate_optional_path,
        )
    )


@router.post("/stamp-cycles", response_model=JobResponse, status_code=202)
async def stamp_cycles(req: StampCyclesRequest):
    """Queue cycle stamping for a dataset under an allowed root."""
    return await _run_intake(lambda: intake_stamp_cycles(req, validate_path=_validate_path))


@router.get("/browse-dirs", response_model=BrowseDirsResponse)
async def browse_dirs(path: str | None = Query(None, description="Directory to list; defaults to dataset_root_base")):
    """List subdirectories under *path* for the destination-path picker.

    Scope: anywhere inside `allowed_dataset_roots`. When *path* is omitted
    (or equals the base), the response `parent` is null — the picker
    treats that as the top of the browsable tree.
    """
    allowed_roots = normalize_roots(settings.allowed_dataset_roots)
    if not allowed_roots:
        raise HTTPException(status_code=500, detail="No allowed dataset roots configured")

    base = Path(settings.dataset_root_base).resolve()
    target = Path(path).resolve() if path else base

    def _inside(candidate: Path) -> bool:
        return any(is_contained_in(candidate, root) for root in allowed_roots)

    if not _inside(target):
        raise HTTPException(status_code=400, detail=f"Path outside allowed roots: {path}")
    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=404, detail=f"Directory not found: {target}")

    entries: list[BrowseDirEntry] = []
    try:
        for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            is_lerobot = (child / "meta" / "info.json").exists()
            entries.append(
                BrowseDirEntry(
                    name=child.name,
                    path=str(child.resolve()),
                    is_lerobot_dataset=is_lerobot,
                )
            )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=f"Permission denied: {target}") from exc

    parent: str | None = None
    if _inside(target.parent) and target != target.parent:
        # Don't let the user navigate above every allowed root.
        if any(target == r for r in allowed_roots):
            parent = None
        else:
            parent = str(target.parent)

    return BrowseDirsResponse(
        path=str(target),
        parent=parent,
        roots=[str(r) for r in allowed_roots],
        entries=entries,
    )


@router.get("/summary", response_model=SummaryResponse)
async def dataset_summary(path: str = Query(..., description="Absolute dataset path to summarize")):
    """Return a small metadata summary used by the Out tab's TargetSummary."""
    resolved = _validate_path(path)

    info_path = resolved / "meta" / "info.json"
    if not info_path.exists():
        raise HTTPException(status_code=404, detail="Not a LeRobot dataset")

    try:
        info = read_info(resolved)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read info.json: {exc}") from exc

    features = info.get("features") or {}
    return SummaryResponse(
        path=str(resolved),
        total_episodes=_coerce_summary_int("total_episodes", info.get("total_episodes")),
        robot_type=info.get("robot_type"),
        fps=_coerce_summary_int("fps", info.get("fps")),
        features_count=len(features) if isinstance(features, dict) else 0,
    )


@router.get("/stamp-cycles/status", response_model=StampCyclesStatusResponse)
async def get_stamp_cycles_status(path: str = Query(..., description="Dataset path to inspect")):
    """Describe whether cycle stamps exist for a dataset."""
    source = _validate_path(path)
    if not source.exists():
        raise HTTPException(status_code=404, detail=f"Source path not found: {path}")
    return describe_stamp_state(source)


@router.get("/ops/status/{job_id}", response_model=JobStatusResponse)
async def get_job_status(job_id: str):
    """Return status of a dataset operation job."""
    try:
        status = await fetch_projected_job_status(job_id)
    except OperationIntakeError as exc:
        raise _translate_intake_error(exc) from exc
    return _status_response(status)
