"""Classify raw recordings and keep one input format per task."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast


RecordingFormat = Literal["fb", "mcap"]


@dataclass(frozen=True)
class SkippedRecording:
    serial: str
    reason: str
    detected_format: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "serial": self.serial,
            "reason": self.reason,
            "detected_format": self.detected_format,
        }


@dataclass(frozen=True)
class RecordingInspection:
    serial: str
    detected_format: RecordingFormat | Literal["mixed"] | None
    ready: bool
    reason: str | None = None


@dataclass(frozen=True)
class TaskFormatSelection:
    task_format: RecordingFormat | None
    recordings: tuple[str, ...]
    skipped: tuple[SkippedRecording, ...]


def normalize_requested_format(value: Any) -> Literal["auto", "fb", "mcap"]:
    """Normalize the job payload's optional ``format`` field."""
    if value is None:
        return "auto"
    if not isinstance(value, str):
        raise ValueError("convert payload format must be a string")

    normalized = value.strip().lower()
    if not normalized:
        return "auto"
    if normalized not in {"auto", "fb", "mcap"}:
        raise ValueError("convert payload format must be one of: auto, fb, mcap")
    return cast(Literal["auto", "fb", "mcap"], normalized)


def inspect_recording(recording_dir: Path, serial: str) -> RecordingInspection:
    """Inspect format markers without treating FB state MCAPs as legacy input."""
    has_metacard = (recording_dir / "metacard.json").is_file()
    has_fb = any((recording_dir / "images").glob("*/*.fb"))
    has_legacy_mcap = (recording_dir / f"{serial}_0.mcap").is_file()

    if has_fb and has_legacy_mcap:
        return RecordingInspection(
            serial=serial,
            detected_format="mixed",
            ready=False,
            reason=(
                "both images/*.fb and the legacy root MCAP are present; "
                "use one recording format"
            ),
        )

    if has_fb:
        has_state_mcap = any((recording_dir / "state").glob("*.mcap"))
        missing = []
        if not has_metacard:
            missing.append("metacard.json")
        if not has_state_mcap:
            missing.append("state/*.mcap")
        return RecordingInspection(
            serial=serial,
            detected_format="fb",
            ready=not missing,
            reason=f"incomplete FB recording; missing {', '.join(missing)}" if missing else None,
        )

    if has_legacy_mcap:
        return RecordingInspection(
            serial=serial,
            detected_format="mcap",
            ready=has_metacard,
            reason=None if has_metacard else "incomplete MCAP recording; missing metacard.json",
        )

    return RecordingInspection(
        serial=serial,
        detected_format=None,
        ready=False,
        reason="no images/*.fb or legacy root MCAP found",
    )


def select_task_recordings(
    task_dir: Path,
    serials: list[str],
    requested_format: Literal["auto", "fb", "mcap"],
) -> TaskFormatSelection:
    """Choose one task format and exclude recordings that do not match it.

    In auto mode the first recognizable, non-mixed recording determines the
    task format. A recording with FB image chunks determines ``fb`` even if its
    state upload is not complete yet; readiness is evaluated separately.
    """
    inspections = [
        inspect_recording(task_dir / serial, serial)
        for serial in sorted(serials)
    ]

    if requested_format == "auto":
        selected_format = next(
            (
                inspection.detected_format
                for inspection in inspections
                if inspection.detected_format in {"fb", "mcap"}
            ),
            None,
        )
        if selected_format is None:
            return TaskFormatSelection(
                task_format=None,
                recordings=(),
                skipped=tuple(
                    SkippedRecording(
                        inspection.serial,
                        inspection.reason or "unrecognized recording format",
                        inspection.detected_format,
                    )
                    for inspection in inspections
                ),
            )
    else:
        selected_format = requested_format

    selected: list[str] = []
    skipped: list[SkippedRecording] = []
    for inspection in inspections:
        detected = inspection.detected_format
        if detected == "mixed":
            skipped.append(SkippedRecording(
                inspection.serial,
                inspection.reason or "mixed recording formats",
                detected,
            ))
        elif detected is None:
            skipped.append(SkippedRecording(
                inspection.serial,
                inspection.reason or "unrecognized recording format",
                None,
            ))
        elif detected != selected_format:
            skipped.append(SkippedRecording(
                inspection.serial,
                (
                    f"recording format {detected} does not match task format "
                    f"{selected_format}; use one format per task"
                ),
                detected,
            ))
        elif not inspection.ready:
            skipped.append(SkippedRecording(
                inspection.serial,
                inspection.reason or "recording upload is incomplete",
                detected,
            ))
        else:
            selected.append(inspection.serial)

    return TaskFormatSelection(
        task_format=selected_format,
        recordings=tuple(selected),
        skipped=tuple(skipped),
    )
