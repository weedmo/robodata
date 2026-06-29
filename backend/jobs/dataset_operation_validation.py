"""Validation adapter for Dataset Operations enqueued through /api/jobs."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from backend.core import config
from backend.datasets.services.delete_payload import DeletePayload
from backend.datasets.services.merge_payload import MergePayload
from backend.datasets.services.split_payload import SplitPayload
from backend.datasets.services.stamp_cycles_payload import StampCyclesPayload
from backend.datasets.services.sync_good_episodes_payload import SyncGoodEpisodesPayload


_DATASET_OPERATION_PAYLOADS: dict[str, type[BaseModel]] = {
    "split": SplitPayload,
    "merge": MergePayload,
    "delete": DeletePayload,
    "sync_good_episodes": SyncGoodEpisodesPayload,
    "stamp_cycles": StampCyclesPayload,
}


class DatasetOperationValidationError(Exception):
    """HTTP-shaped validation failure for a Dataset Operation enqueue."""

    def __init__(self, *, status_code: int, detail: dict[str, Any]) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


class ValidatedDatasetOperation:
    """Canonical Operation Payload plus its canonical active-dedupe key."""

    def __init__(self, *, payload: dict[str, Any], dedupe_key: str) -> None:
        self.payload = payload
        self.dedupe_key = dedupe_key


def is_dataset_operation(type_: str) -> bool:
    return type_ in _DATASET_OPERATION_PAYLOADS


def validate_dataset_operation(type_: str, payload: Mapping[str, Any]) -> ValidatedDatasetOperation:
    """Validate and canonicalize a Dataset Operation before Job persistence."""
    model_type = _DATASET_OPERATION_PAYLOADS[type_]
    try:
        parsed = model_type.model_validate(dict(payload))
    except ValidationError as exc:
        raise DatasetOperationValidationError(
            status_code=422,
            detail={
                "error": "invalid_dataset_operation_payload",
                "operation": type_,
                "issues": _validation_issues(exc),
            },
        ) from exc

    canonical = parsed.model_copy(update=_canonical_path_updates(type_, parsed))
    return ValidatedDatasetOperation(
        payload=canonical.model_dump(),
        dedupe_key=getattr(canonical, "dedupe_key")(),
    )


def _validation_issues(exc: ValidationError) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for error in exc.errors(include_url=False):
        loc = error.get("loc") or ()
        field = ".".join(str(part) for part in loc) if loc else "payload"
        issues.append({"field": field, "message": str(error.get("msg", "invalid value"))})
    return issues or [{"field": "payload", "message": "invalid Dataset Operation payload"}]


def _canonical_path_updates(type_: str, payload: BaseModel) -> dict[str, Any]:
    if type_ == "split":
        source = _canonical_existing_source(type_, getattr(payload, "source_path"))
        return {
            "source_path": str(source),
            "output_dir": _canonical_optional_path(type_, getattr(payload, "output_dir")),
        }
    if type_ == "delete":
        source = _canonical_existing_source(type_, getattr(payload, "source_path"))
        return {
            "source_path": str(source),
            "output_dir": _canonical_optional_path(type_, getattr(payload, "output_dir")),
        }
    if type_ == "stamp_cycles":
        return {"source_path": str(_canonical_existing_source(type_, getattr(payload, "source_path")))}
    if type_ == "merge":
        return {
            "source_paths": [
                str(_canonical_existing_source(type_, source_path))
                for source_path in getattr(payload, "source_paths")
            ],
            "output_dir": _canonical_optional_path(type_, getattr(payload, "output_dir")),
        }
    if type_ == "sync_good_episodes":
        source = _canonical_existing_source(type_, getattr(payload, "source_path"))
        destination = _canonical_path(type_, getattr(payload, "destination_path"))
        if source == destination:
            raise DatasetOperationValidationError(
                status_code=400,
                detail={
                    "error": "invalid_dataset_operation_payload",
                    "operation": type_,
                    "issues": [{"field": "destination_path", "message": "source and destination must differ"}],
                },
            )
        return {"source_path": str(source), "destination_path": str(destination)}
    return {}


def _canonical_existing_source(operation: str, path_str: str) -> Path:
    path = _canonical_path(operation, path_str)
    if not path.exists():
        raise DatasetOperationValidationError(
            status_code=404,
            detail={
                "error": "dataset_operation_source_missing",
                "operation": operation,
                "path": str(path),
            },
        )
    return path


def _canonical_optional_path(operation: str, path_str: str | None) -> str | None:
    if path_str is None:
        return None
    return str(_canonical_path(operation, path_str))


def _canonical_path(operation: str, path_str: str) -> Path:
    path = Path(path_str).resolve()
    allowed_roots = [Path(root).resolve() for root in config.settings.allowed_dataset_roots]
    if not any(path == root or root in path.parents for root in allowed_roots):
        raise DatasetOperationValidationError(
            status_code=400,
            detail={
                "error": "dataset_operation_path_policy",
                "operation": operation,
                "path": path_str,
            },
        )
    return path
