"""Queue handler for non-destructive legacy MCAP to FlatBuffer conversion."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable, Mapping

from backend.jobs import lifecycle
from backend.workers.runtime import CancelledNormally
from backend.converter.mcap_to_fb_converter import (
    ConversionCancelled,
    convert_episode,
)


CheckCancel = Callable[[], Awaitable[bool]]
ProgressCallback = Callable[[Mapping[str, Any]], None]
SERIAL_RE = re.compile(r"^\d{8}_\d{6}(_\d+)?$")
log = logging.getLogger(__name__)


def _cell_task(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"mcap_to_fb_convert payload requires a non-empty {field}")
    raw = value.strip()
    candidate = PurePosixPath(raw.strip("/"))
    if raw.startswith("/") or len(candidate.parts) != 2 or ".." in candidate.parts:
        raise ValueError(f"{field} must have the form <cell>/<task>")
    return candidate.as_posix()


def _parse_payload(payload: Mapping[str, Any]) -> tuple[str, str, int, bool]:
    source = _cell_task(payload.get("cell_task"), field="cell_task")
    cell, task = source.split("/", 1)
    output_raw = payload.get("output_cell_task")
    output = (
        f"{cell}/{task}_fb"
        if output_raw is None
        else _cell_task(output_raw, field="output_cell_task")
    )
    if output == source:
        raise ValueError("output_cell_task must differ from the source cell_task")

    messages_per_chunk = payload.get("messages_per_chunk", 300)
    if (
        isinstance(messages_per_chunk, bool)
        or not isinstance(messages_per_chunk, int)
        or messages_per_chunk < 1
    ):
        raise ValueError("messages_per_chunk must be an integer greater than zero")
    force = payload.get("force", False)
    if not isinstance(force, bool):
        raise ValueError("force must be a boolean")
    return source, output, messages_per_chunk, force


def _source_recordings(source_task_dir: Path) -> tuple[list[Path], list[dict[str, str]]]:
    if not source_task_dir.is_dir():
        raise ValueError(f"source cell_task directory does not exist: {source_task_dir}")

    recordings: list[Path] = []
    skipped: list[dict[str, str]] = []
    for recording_dir in sorted(source_task_dir.iterdir()):
        if not recording_dir.is_dir() or not SERIAL_RE.fullmatch(recording_dir.name):
            continue
        serial = recording_dir.name
        missing: list[str] = []
        if not (recording_dir / f"{serial}_0.mcap").is_file():
            missing.append(f"{serial}_0.mcap")
        if not (recording_dir / "metacard.json").is_file():
            missing.append("metacard.json")
        if missing:
            skipped.append({
                "serial": serial,
                "reason": f"missing {', '.join(missing)}",
            })
            continue
        recordings.append(recording_dir)

    if not recordings:
        raise ValueError(f"no complete legacy MCAP recordings found in {source_task_dir}")
    return recordings, skipped


def _convert_task_sync(
    payload: Mapping[str, Any],
    *,
    raw_base: Path,
    cancel_requested: threading.Event,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    source_cell_task, output_cell_task, messages_per_chunk, force = _parse_payload(payload)
    source_dir = raw_base / source_cell_task
    output_dir = raw_base / output_cell_task
    recordings, skipped = _source_recordings(source_dir)

    progress: dict[str, Any] = {
        "phase": "scanning",
        "source_cell_task": source_cell_task,
        "output_cell_task": output_cell_task,
        "recording_total": len(recordings),
        "skipped_recordings": skipped,
    }

    def publish(update: Mapping[str, Any]) -> None:
        progress.update(update)
        if progress_callback is not None:
            progress_callback(dict(progress))

    publish({"phase": "converting"})
    converted: list[dict[str, Any]] = []
    for index, recording_dir in enumerate(recordings, start=1):
        if cancel_requested.is_set():
            raise ConversionCancelled("MCAP-to-FB task cancelled")
        serial = recording_dir.name
        publish({
            "phase": "converting",
            "recording": serial,
            "recording_index": index,
        })
        result = convert_episode(
            recording_dir,
            output_dir,
            serial=serial,
            messages_per_chunk=messages_per_chunk,
            force=force,
            cancel_requested=cancel_requested.is_set,
        )
        converted.append({
            "serial": serial,
            "episode_dir": str(result.episode_dir),
            "camera_message_counts": result.camera_message_counts,
            "state_message_count": result.state_message_count,
            "chunk_count": result.chunk_count,
        })
        publish({
            "converted_recordings": len(converted),
            "last_converted_recording": serial,
        })

    publish({"phase": "complete", "recording": None})
    return {
        "source_cell_task": source_cell_task,
        "output_cell_task": output_cell_task,
        "output_path": str(output_dir),
        "converted_recordings": converted,
        "skipped_recordings": skipped,
    }


async def handle_mcap_to_fb_convert(
    job: Mapping[str, Any], *, check_cancel: CheckCancel,
) -> CancelledNormally | Mapping[str, Any]:
    """Convert all complete legacy recordings in one cell/task into FB episodes."""
    cancel_requested = threading.Event()
    loop = asyncio.get_running_loop()
    job_id = int(job["id"])

    def update_progress(progress: Mapping[str, Any]) -> None:
        future = asyncio.run_coroutine_threadsafe(
            lifecycle.update_progress(job_id, progress),
            loop,
        )
        try:
            future.result(timeout=2.0)
        except Exception:
            log.exception("failed to update MCAP-to-FB job progress for job %s", job_id)

    async def watch_cancel() -> None:
        while not cancel_requested.is_set():
            if await check_cancel():
                cancel_requested.set()
                return
            await asyncio.sleep(1.0)

    watcher = asyncio.create_task(watch_cancel())
    try:
        try:
            result = await asyncio.to_thread(
                _convert_task_sync,
                job["payload"],
                raw_base=Path(os.environ.get("RAW_BASE", "/data/raw")),
                cancel_requested=cancel_requested,
                progress_callback=update_progress,
            )
        except ConversionCancelled:
            return CancelledNormally(cleanup="MCAP-to-FB conversion cancelled")
    finally:
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass

    return result
