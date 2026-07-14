"""Dataset operation intake: payload construction, enqueue, and status projection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from backend.datasets.services.delete_payload import DeletePayload
from backend.datasets.services.merge_payload import MergePayload
from backend.datasets.services.split_payload import SplitPayload
from backend.datasets.services.stamp_cycles_payload import StampCyclesPayload
from backend.datasets.services.sync_good_episodes_payload import SyncGoodEpisodesPayload
from backend.jobs import repo as jobs_repo

PathValidator = Callable[[str], Path]
OptionalPathValidator = Callable[[str | None], str | None]


class OperationIntakeError(Exception):
    """Base error for operation intake failures."""


class DuplicateDatasetOperation(OperationIntakeError):
    def __init__(self, existing_job_id: int) -> None:
        self.existing_job_id = existing_job_id
        super().__init__(f"Duplicate dataset operation: {existing_job_id}")


class SourcePathNotFound(OperationIntakeError):
    def __init__(self, requested_path: str) -> None:
        self.requested_path = requested_path
        super().__init__(f"Source path not found: {requested_path}")


class SameSourceAndDestination(OperationIntakeError):
    """Raised when a sync-good-episodes operation targets its source path."""


class DatasetJobNotFound(OperationIntakeError):
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        super().__init__(f"Job not found: {job_id}")


@dataclass(frozen=True)
class EnqueuedDatasetOperation:
    job_id: str
    operation: str
    status: str = "queued"


@dataclass(frozen=True)
class ProjectedJobStatus:
    job_id: str
    operation: str
    status: str
    created_at: str
    completed_at: str | None = None
    error: str | None = None
    result_path: str | None = None
    summary: dict[str, Any] | None = None


def _timestamp(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _result_mapping(job: Mapping[str, Any]) -> Mapping[str, Any]:
    result = job.get("result") or {}
    return result if isinstance(result, Mapping) else {}


async def _enqueue(type_: str, payload: dict[str, object], dedupe_key: str | None) -> EnqueuedDatasetOperation:
    try:
        job = await jobs_repo.enqueue(type_=type_, payload=payload, dedupe_key=dedupe_key)
    except jobs_repo.DuplicateDedupe as exc:
        raise DuplicateDatasetOperation(exc.existing_job_id) from exc
    return EnqueuedDatasetOperation(job_id=str(job["external_id"]), operation=type_)


def _require_source_exists(source: Path, requested_path: str) -> None:
    if not source.exists():
        raise SourcePathNotFound(requested_path)


async def intake_split(
    req: Any,
    *,
    validate_path: PathValidator,
    validate_optional_path: OptionalPathValidator,
) -> EnqueuedDatasetOperation:
    source = validate_path(req.source_path)
    output_dir = validate_optional_path(req.output_dir)
    _require_source_exists(source, req.source_path)
    payload = SplitPayload(
        source_path=str(source),
        episode_ids=req.episode_ids,
        target_name=req.target_name,
        output_dir=output_dir,
    )
    return await _enqueue("split", payload.model_dump(), payload.dedupe_key())


async def intake_split_into(
    req: Any,
    *,
    validate_path: PathValidator,
) -> EnqueuedDatasetOperation:
    source = validate_path(req.source_path)
    destination = validate_path(req.destination_path)
    _require_source_exists(source, req.source_path)
    if source == destination:
        raise SameSourceAndDestination("source and destination must differ")
    payload = SyncGoodEpisodesPayload(
        source_path=str(source),
        episode_ids=req.episode_ids,
        destination_path=str(destination),
    )
    return await _enqueue("sync_good_episodes", payload.model_dump(), payload.dedupe_key())


async def intake_merge(
    req: Any,
    *,
    validate_path: PathValidator,
    validate_optional_path: OptionalPathValidator,
) -> EnqueuedDatasetOperation:
    output_dir = validate_optional_path(req.output_dir)
    source_paths: list[str] = []
    for requested_path in req.source_paths:
        source = validate_path(requested_path)
        _require_source_exists(source, requested_path)
        source_paths.append(str(source))
    payload = MergePayload(
        source_paths=source_paths,
        target_name=req.target_name,
        output_dir=output_dir,
    )
    return await _enqueue("merge", payload.model_dump(), payload.dedupe_key())


async def intake_delete(
    req: Any,
    *,
    validate_path: PathValidator,
    validate_optional_path: OptionalPathValidator,
) -> EnqueuedDatasetOperation:
    source = validate_path(req.source_path)
    output_dir = validate_optional_path(req.output_dir)
    _require_source_exists(source, req.source_path)
    payload = DeletePayload(
        source_path=str(source),
        episode_ids=req.episode_ids,
        output_dir=output_dir,
    )
    return await _enqueue("delete", payload.model_dump(), payload.dedupe_key())


async def intake_stamp_cycles(
    req: Any,
    *,
    validate_path: PathValidator,
) -> EnqueuedDatasetOperation:
    source = validate_path(req.source_path)
    _require_source_exists(source, req.source_path)
    payload = StampCyclesPayload(source_path=str(source), overwrite=req.overwrite)
    return await _enqueue("stamp_cycles", payload.model_dump(), payload.dedupe_key())


def project_job_status(job: Mapping[str, Any]) -> ProjectedJobStatus:
    """Flatten persistent queue rows into the dataset operation status schema."""
    result = _result_mapping(job)
    summary = result.get("summary")
    return ProjectedJobStatus(
        job_id=str(job["external_id"]),
        operation=str(job["type"]),
        status=str(job["status"]),
        created_at=_timestamp(job.get("created_at")) or "",
        completed_at=_timestamp(job.get("finished_at")),
        error=job.get("error"),
        result_path=result.get("result_path"),
        summary=dict(summary) if isinstance(summary, Mapping) else None,
    )


async def fetch_projected_job_status(job_id: str) -> ProjectedJobStatus:
    job = await jobs_repo.fetch_by_external_id(job_id)
    if job is None and job_id.isdigit():
        job = await jobs_repo.fetch(int(job_id))
    if job is None:
        raise DatasetJobNotFound(job_id)
    return project_job_status(dict(job))

