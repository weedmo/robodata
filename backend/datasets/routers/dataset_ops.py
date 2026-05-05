"""FastAPI router for dataset split/merge operations."""

import logging
from datetime import datetime
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, field_validator

from backend.config import settings
from backend.datasets.services.cycle_stamp_service import describe_stamp_state
from backend.datasets.services.dataset_ops_engine import read_info
from backend.jobs import repo as jobs_repo


def _validate_path(path_str: str) -> Path:
    """Resolve a dataset path and ensure it stays under an allowed root."""
    resolved = Path(path_str).resolve()
    allowed_roots = [Path(root).resolve() for root in settings.allowed_dataset_roots]
    if not any(resolved == root or root in resolved.parents for root in allowed_roots):
        raise HTTPException(status_code=400, detail=f"Path outside allowed roots: {path_str}")
    return resolved


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


def _isoformat_or_none(value: object) -> str | None:
    """Render legacy string timestamps and DB datetimes through one UI contract."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _mapping_or_empty(value: object) -> Mapping[str, Any]:
    """Return result/summary mappings without exposing persistence-only fields."""
    return value if isinstance(value, Mapping) else {}


def _job_status_response(job: Mapping[str, Any]) -> "JobStatusResponse":
    """Flatten legacy in-memory and persistent-queue jobs into the UI façade schema."""
    result = _mapping_or_empty(job.get("result"))
    summary = job.get("summary", result.get("summary"))
    return JobStatusResponse(
        job_id=str(job.get("external_id") or job["id"]),
        operation=str(job.get("operation") or job["type"]),
        status=str(job["status"]),
        created_at=_isoformat_or_none(job["created_at"]) or "",
        completed_at=_isoformat_or_none(job.get("completed_at") or job.get("finished_at")),
        error=job.get("error"),
        result_path=job.get("result_path", result.get("result_path")),
        summary=dict(summary) if isinstance(summary, Mapping) else None,
    )


logger = logging.getLogger(__name__)


def _dedupe_path(operation: str, source: Path) -> str:
    return f"{operation}:{source}"


async def _enqueue_dataset_job(
    *,
    type_: str,
    payload: dict[str, object],
    dedupe_key: str | None,
) -> str:
    try:
        job = await jobs_repo.enqueue(type_=type_, payload=payload, dedupe_key=dedupe_key)
    except jobs_repo.DuplicateDedupe as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "duplicate_dedupe_key", "existing_job_id": exc.existing_job_id},
        ) from exc
    return str(job["external_id"])


def _job_timestamp(job: dict | object, key: str) -> str | None:
    value = job.get(key) if isinstance(job, dict) else None
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _status_response_from_job(job: dict) -> JobStatusResponse:
    result = job.get("result") or {}
    if not isinstance(result, dict):
        result = {}
    return JobStatusResponse(
        job_id=str(job["external_id"]),
        operation=str(job["type"]),
        status=str(job["status"]),
        created_at=_job_timestamp(job, "created_at") or "",
        completed_at=_job_timestamp(job, "finished_at"),
        error=job.get("error"),
        result_path=result.get("result_path"),
        summary=result.get("summary"),
    )


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
    source = _validate_path(req.source_path)
    output_dir = _validate_optional_path(req.output_dir)
    if not source.exists():
        raise HTTPException(status_code=404, detail=f"Source path not found: {req.source_path}")

    job_id = await _enqueue_dataset_job(
        type_="split",
        payload={
            "source_path": str(source),
            "episode_ids": req.episode_ids,
            "target_name": req.target_name,
            "output_dir": output_dir,
        },
        dedupe_key=f"split:{source}:{req.target_name}",
    )
    return JobResponse(job_id=job_id, operation="split", status="queued")


@router.post("/split-into", response_model=JobResponse, status_code=202)
async def split_into_dataset(req: SplitIntoRequest):
    """Sync selected good episodes to one absolute destination path."""
    source = _validate_path(req.source_path)
    destination = _validate_path(req.destination_path)
    if not source.exists():
        raise HTTPException(status_code=404, detail=f"Source path not found: {req.source_path}")
    if source == destination:
        raise HTTPException(status_code=400, detail="source and destination must differ")

    job_id = await _enqueue_dataset_job(
        type_="sync_good_episodes",
        payload={
            "source_path": str(source),
            "episode_ids": req.episode_ids,
            "destination_path": str(destination),
        },
        dedupe_key=f"sync_good_episodes:{source}:{destination}",
    )
    return JobResponse(job_id=job_id, operation="sync_good_episodes", status="queued")


@router.post("/merge", response_model=JobResponse, status_code=202)
async def merge_datasets(req: MergeRequest):
    """Merge multiple source datasets into a new derived dataset."""
    output_dir = _validate_optional_path(req.output_dir)
    source_paths: list[str] = []
    for sp in req.source_paths:
        source = _validate_path(sp)
        if not source.exists():
            raise HTTPException(status_code=404, detail=f"Source path not found: {sp}")
        source_paths.append(str(source))

    job_id = await _enqueue_dataset_job(
        type_="merge",
        payload={
            "source_paths": source_paths,
            "target_name": req.target_name,
            "output_dir": output_dir,
        },
        dedupe_key=f"merge:{','.join(source_paths)}:{req.target_name}",
    )
    return JobResponse(job_id=job_id, operation="merge", status="queued")


@router.post("/delete", response_model=JobResponse, status_code=202)
async def delete_episodes(req: DeleteRequest):
    """Delete specified episodes from a dataset, producing a new dataset."""
    source = _validate_path(req.source_path)
    output_dir = _validate_optional_path(req.output_dir)
    if not source.exists():
        raise HTTPException(status_code=404, detail=f"Source path not found: {req.source_path}")

    job_id = await _enqueue_dataset_job(
        type_="delete",
        payload={
            "source_path": str(source),
            "episode_ids": req.episode_ids,
            "output_dir": output_dir,
        },
        dedupe_key=f"delete:{source}:{','.join(map(str, req.episode_ids))}",
    )
    return JobResponse(job_id=job_id, operation="delete", status="queued")


@router.post("/stamp-cycles", response_model=JobResponse, status_code=202)
async def stamp_cycles(req: StampCyclesRequest):
    """Queue cycle stamping for a dataset under an allowed root."""
    source = _validate_path(req.source_path)
    if not source.exists():
        raise HTTPException(status_code=404, detail=f"Source path not found: {req.source_path}")

    job_id = await _enqueue_dataset_job(
        type_="stamp_cycles",
        payload={"source_path": str(source), "overwrite": req.overwrite},
        dedupe_key=_dedupe_path("stamp_cycles", source),
    )
    return JobResponse(job_id=job_id, operation="stamp_cycles", status="queued")


@router.get("/browse-dirs", response_model=BrowseDirsResponse)
async def browse_dirs(path: str | None = Query(None, description="Directory to list; defaults to dataset_root_base")):
    """List subdirectories under *path* for the destination-path picker.

    Scope: anywhere inside `allowed_dataset_roots`. When *path* is omitted
    (or equals the base), the response `parent` is null — the picker
    treats that as the top of the browsable tree.
    """
    allowed_roots = [Path(r).resolve() for r in settings.allowed_dataset_roots]
    if not allowed_roots:
        raise HTTPException(status_code=500, detail="No allowed dataset roots configured")

    base = Path(settings.dataset_root_base).resolve()
    target = Path(path).resolve() if path else base

    def _inside(candidate: Path) -> bool:
        return any(candidate == r or r in candidate.parents for r in allowed_roots)

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
    job = dataset_ops_service.get_job_status(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    return _job_status_response(job)
