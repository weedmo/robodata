"""Queue-driven converter entrypoint.

The UI enqueues convert jobs into Postgres. This module is the long-running
worker process that claims those jobs and calls the bundled
``rosbag2lerobot-svt`` converter for the requested task.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
import threading
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from backend.converter.recovery_service import RecoveryError, recovery_blockers
from backend.datasets.services.bronze_silver_pipeline import (
    handle_bronze_silver_batch,
)
from backend.jobs import lifecycle
from backend.workers.runtime import (
    CancelledNormally,
    run_forever,
    tick,
)


CheckCancel = Callable[[], Awaitable[bool]]
ProgressCallback = Callable[[Mapping[str, Any]], None]
log = logging.getLogger(__name__)
HEALTH_FILE = Path("/tmp/healthy")


class PendingConversionRecoveryError(RuntimeError):
    """The target has an interrupted conversion transaction to recover first."""


def _ensure_conversion_repo_on_path() -> None:
    repo_path = Path(__file__).resolve().parents[2] / "rosbag2lerobot-svt"
    if repo_path.is_dir() and str(repo_path) not in sys.path:
        sys.path.insert(0, str(repo_path))


def _load_auto_converter_module():
    _ensure_conversion_repo_on_path()
    return importlib.import_module("auto_converter")


def _normalize_target(raw: str, raw_base: Path) -> str:
    target = raw.strip()
    if not target:
        raise ValueError("convert payload requires a non-empty cell_task or cell")

    path = Path(target)
    if path.is_absolute():
        try:
            target = path.resolve().relative_to(raw_base.resolve()).as_posix()
        except ValueError:
            target = path.name
    return target.strip("/")


def _select_cell_tasks(target: str, all_tasks: Mapping[str, list[str]]) -> list[str]:
    if target in all_tasks:
        return [target]
    if "/" not in target:
        prefix = f"{target}/"
        matches = [cell_task for cell_task in sorted(all_tasks) if cell_task.startswith(prefix)]
        if matches:
            return matches
    raise ValueError(f"no raw recordings found for convert target: {target}")


def _target_fps_from_payload(payload: Mapping[str, Any]) -> int | None:
    target_fps = payload.get("target_fps")
    if target_fps is None:
        return None
    if (
        isinstance(target_fps, bool)
        or not isinstance(target_fps, int)
        or target_fps <= 0
    ):
        raise ValueError("convert payload target_fps must be a positive integer")
    return target_fps


def _require_recovered_output(auto_converter: Any, cell_task: str) -> None:
    """Fail closed while any canonical recovery artifact remains active."""
    try:
        blockers = recovery_blockers(
            cell_task,
            lerobot_root=Path(auto_converter.LEROBOT_BASE),
        )
    except (OSError, RecoveryError) as exc:
        raise PendingConversionRecoveryError(
            f"conversion recovery state cannot be inspected safely for {cell_task}"
        ) from exc
    if blockers:
        raise PendingConversionRecoveryError(
            "conversion blocked for "
            f"{cell_task}: active recovery artifacts {', '.join(blockers)}"
        )


def _pending_recordings(auto_converter: Any, cell_task: str) -> tuple[list[str], Any]:
    _require_recovered_output(auto_converter, cell_task)
    scanner = auto_converter.NASScanner(auto_converter.RAW_BASE)
    state = auto_converter.ConvertState(auto_converter.STATE_FILE)
    state.load()

    all_tasks = scanner.scan()
    if cell_task not in all_tasks:
        raise ValueError(f"no raw recordings found for convert task: {cell_task}")

    cell, task = cell_task.split("/", 1)
    converted_serials = auto_converter._load_converted_serials(
        auto_converter.LEROBOT_BASE / cell / task,
    )
    state.reconcile_persisted_serials(cell_task, converted_serials)
    state.flush()
    failed = state.get_failed_serials(cell_task)
    transient = set(state.get_transient_failed(cell_task).keys())
    pending = scanner.find_pending_recordings(
        all_tasks[cell_task],
        converted_serials,
        failed,
        transient,
    )
    task_serials = set(all_tasks[cell_task])
    retry_eligible = [
        serial
        for serial in state.get_retry_eligible(cell_task)
        if serial in task_serials
    ]
    retry_set = set(retry_eligible)
    return retry_eligible + [s for s in pending if s not in retry_set], state


def _convert_payload_sync(
    auto_converter: Any,
    payload: Mapping[str, Any],
    *,
    cancel_requested: threading.Event,
    progress_callback: ProgressCallback | None = None,
) -> None:
    target_raw = payload.get("cell_task") or payload.get("cell")
    target_fps = _target_fps_from_payload(payload)
    if target_fps is not None and not (
        isinstance(target_raw, str) and target_raw.strip()
    ):
        raise ValueError(
            "convert payload target_fps requires a non-empty cell_task or cell"
        )

    scanner = auto_converter.NASScanner(auto_converter.RAW_BASE)
    all_tasks = scanner.scan()
    if isinstance(target_raw, str):
        target = _normalize_target(target_raw, auto_converter.RAW_BASE)
        cell_tasks = _select_cell_tasks(target, all_tasks)
    elif target_raw is None:
        cell_tasks = sorted(all_tasks)
    else:
        raise ValueError("convert payload cell_task or cell must be a string")

    original_check_stop = auto_converter._check_stop_requested
    original_has_other_request = auto_converter._has_other_task_request
    original_emit_event = getattr(auto_converter, "_emit_event", None)
    auto_converter._check_stop_requested = lambda: cancel_requested.is_set()
    auto_converter._has_other_task_request = lambda _cell_task: False

    progress: dict[str, Any] = {
        "phase": "scanning",
        "target": target_raw,
        "task_total": len(cell_tasks),
    }

    def publish_progress(update: Mapping[str, Any]) -> None:
        if progress_callback is None:
            return
        progress.update(update)
        progress_callback(dict(progress))

    def wrapped_emit_event(event: Mapping[str, Any]) -> None:
        if callable(original_emit_event):
            original_emit_event(event)

        event_type = event.get("type")
        if event_type == "converting":
            publish_progress({
                "phase": "converting",
                "cell_task": event.get("task"),
                "pending_recordings": event.get("count"),
            })
        elif event_type == "recording_start":
            recording = event.get("recording")
            publish_progress({
                "phase": "converting",
                "recording": recording,
                "recording_index": event.get("index"),
                "recording_total": event.get("total"),
            })
        elif event_type == "converted":
            publish_progress({
                "phase": "converting",
                "last_converted_recording": event.get("recording"),
                "last_frames": event.get("frames"),
                "last_duration": event.get("duration"),
            })
        elif event_type == "failed":
            publish_progress({
                "phase": "converting",
                "last_failed_recording": event.get("recording"),
                "last_error_code": event.get("error_code"),
                "last_error": event.get("reason"),
            })
        elif event_type == "finalizing":
            publish_progress({
                "phase": "finalizing",
                "cell_task": event.get("task"),
            })
        elif event_type == "finalized":
            publish_progress({
                "phase": "finalized",
                "cell_task": event.get("task"),
            })

    try:
        if hasattr(auto_converter, "_clear_stop_flag"):
            auto_converter._clear_stop_flag()
        auto_converter.shutdown_event.clear()
        if callable(original_emit_event):
            auto_converter._emit_event = wrapped_emit_event
        publish_progress({"phase": "scanning"})

        for task_index, cell_task in enumerate(cell_tasks, start=1):
            if cancel_requested.is_set():
                auto_converter.shutdown_event.set()
                return

            recordings, state = _pending_recordings(auto_converter, cell_task)
            if not recordings:
                log.info("No pending recordings for %s", cell_task)
                continue

            cell, task = cell_task.split("/", 1)
            publish_progress({
                "phase": "converting",
                "cell_task": cell_task,
                "task_index": task_index,
                "task_total": len(cell_tasks),
                "pending_recordings": len(recordings),
                "recording": None,
                "recording_index": None,
                "recording_total": len(recordings),
            })
            before_count = state.get_converted_count(cell_task)
            output_root = auto_converter.LEROBOT_BASE / cell / task
            before_output_serials = auto_converter._load_converted_serials(output_root)
            if target_fps is None:
                result = auto_converter.convert_task(
                    cell,
                    task,
                    recordings,
                    state,
                )
            else:
                result = auto_converter.convert_task(
                    cell,
                    task,
                    recordings,
                    state,
                    target_fps=target_fps,
                )
            mount_ok = getattr(result, "mount_ok", bool(result))
            after_count = state.get_converted_count(cell_task)
            after_output_serials = auto_converter._load_converted_serials(output_root)
            after_failed_serials = state.get_failed_serials(cell_task)
            if cancel_requested.is_set() or auto_converter.shutdown_event.is_set():
                return
            if not mount_ok:
                raise RuntimeError(f"converter reported mount/network failure for {cell_task}")
            if after_count < before_count:
                raise RuntimeError(
                    "converter state regressed for "
                    f"{cell_task}: converted_count {before_count} -> {after_count}. "
                    "The output dataset was likely repaired after corruption; "
                    "backup or remove that lerobot task output before retrying."
                )
            if (
                after_count == before_count
                and len(after_output_serials) <= len(before_output_serials)
            ):
                unresolved = (
                    set(recordings)
                    - after_output_serials
                    - after_failed_serials
                )
                if unresolved:
                    raise RuntimeError(
                        "converter made no durable or terminal progress for "
                        f"{cell_task}; {len(unresolved)} of {len(recordings)} "
                        "recordings remain unresolved. The output dataset may "
                        "be corrupted or transient retries may still be pending; "
                        "inspect converter logs before retrying."
                    )
                log.info(
                    "Resolved %d recording(s) for %s as terminal data errors",
                    len(recordings),
                    cell_task,
                )
        publish_progress({"phase": "complete"})
    finally:
        auto_converter._check_stop_requested = original_check_stop
        auto_converter._has_other_task_request = original_has_other_request
        if callable(original_emit_event):
            auto_converter._emit_event = original_emit_event
        auto_converter.shutdown_event.clear()


async def _run_conversion(
    payload: Mapping[str, Any],
    *,
    job_id: int | None = None,
    check_cancel: CheckCancel | None = None,
) -> CancelledNormally | None:
    auto_converter = _load_auto_converter_module()
    cancel_requested = threading.Event()
    loop = asyncio.get_running_loop()

    def update_progress(progress: Mapping[str, Any]) -> None:
        if job_id is None:
            return
        future = asyncio.run_coroutine_threadsafe(
            lifecycle.update_progress(job_id, progress),
            loop,
        )
        try:
            future.result(timeout=2.0)
        except Exception:
            log.exception("failed to update convert job progress for job %s", job_id)

    async def _watch_cancel() -> None:
        if check_cancel is None:
            return
        while not cancel_requested.is_set():
            if await check_cancel():
                cancel_requested.set()
                auto_converter.shutdown_event.set()
                return
            await asyncio.sleep(1.0)

    watcher = asyncio.create_task(_watch_cancel())
    try:
        await asyncio.to_thread(
            _convert_payload_sync,
            auto_converter,
            payload,
            cancel_requested=cancel_requested,
            progress_callback=update_progress,
        )
    finally:
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass

    if cancel_requested.is_set():
        return CancelledNormally(cleanup="conversion cancelled")
    return None


async def _handler(
    job: Mapping[str, Any], *, check_cancel: CheckCancel,
) -> CancelledNormally | None:
    return await _run_conversion(job["payload"], job_id=job["id"], check_cancel=check_cancel)


HANDLERS = {"convert": _handler, "bronze_silver_batch": handle_bronze_silver_batch}


async def process_one_queued(*, idle_sleep: float = 1.0) -> None:
    """Single runtime tick — used by tests and runbooks."""
    await tick(
        worker_id="converter",
        handlers=HANDLERS,
        idle_sleep=idle_sleep,
    )


async def run_converter_forever() -> None:  # pragma: no cover — ops entry point
    async def _touch_health() -> None:
        while True:
            try:
                HEALTH_FILE.touch()
            except OSError:
                log.exception("failed to update converter health file")
            await asyncio.sleep(30.0)

    health_task = asyncio.create_task(_touch_health())
    try:
        await run_forever(
            worker_id="converter",
            handlers=HANDLERS,
        )
    finally:
        health_task.cancel()
        try:
            await health_task
        except asyncio.CancelledError:
            pass


def main() -> None:  # pragma: no cover — ops entry point
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(run_converter_forever())


if __name__ == "__main__":  # pragma: no cover — ops entry point
    main()
