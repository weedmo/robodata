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
from typing import Any, Awaitable, Callable, Mapping
from pathlib import Path

from backend.workers.runtime import (
    CancelledNormally,
    run_forever,
    tick,
)


CheckCancel = Callable[[], Awaitable[bool]]
log = logging.getLogger(__name__)
HEALTH_FILE = Path("/tmp/healthy")


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


def _pending_recordings(auto_converter: Any, cell_task: str) -> tuple[list[str], Any]:
    scanner = auto_converter.NASScanner(auto_converter.RAW_BASE)
    state = auto_converter.ConvertState(auto_converter.STATE_FILE)
    state.load()

    all_tasks = scanner.scan()
    if cell_task not in all_tasks:
        raise ValueError(f"no raw recordings found for convert task: {cell_task}")

    cell, task = cell_task.split("/", 1)
    failed = state.get_failed_serials(cell_task)
    transient = set(state.get_transient_failed(cell_task).keys())
    converted_serials = auto_converter._load_converted_serials(
        auto_converter.LEROBOT_BASE / cell / task,
    )
    pending = scanner.find_pending_recordings(
        all_tasks[cell_task],
        converted_serials,
        failed,
        transient,
    )
    retry_eligible = state.get_retry_eligible(cell_task)
    retry_set = set(retry_eligible)
    return retry_eligible + [s for s in pending if s not in retry_set], state


def _convert_payload_sync(
    auto_converter: Any,
    payload: Mapping[str, Any],
    *,
    cancel_requested: threading.Event,
) -> None:
    target_raw = payload.get("cell_task") or payload.get("cell")
    if not isinstance(target_raw, str):
        raise ValueError("convert payload requires string cell_task or cell")

    scanner = auto_converter.NASScanner(auto_converter.RAW_BASE)
    all_tasks = scanner.scan()
    target = _normalize_target(target_raw, auto_converter.RAW_BASE)
    cell_tasks = _select_cell_tasks(target, all_tasks)

    original_check_stop = auto_converter._check_stop_requested
    original_has_other_request = auto_converter._has_other_task_request
    auto_converter._check_stop_requested = lambda: cancel_requested.is_set()
    auto_converter._has_other_task_request = lambda _cell_task: False

    try:
        if hasattr(auto_converter, "_clear_stop_flag"):
            auto_converter._clear_stop_flag()
        auto_converter.shutdown_event.clear()

        for cell_task in cell_tasks:
            if cancel_requested.is_set():
                auto_converter.shutdown_event.set()
                return

            recordings, state = _pending_recordings(auto_converter, cell_task)
            if not recordings:
                log.info("No pending recordings for %s", cell_task)
                continue

            cell, task = cell_task.split("/", 1)
            mount_ok = auto_converter.convert_task(cell, task, recordings, state)
            if cancel_requested.is_set() or auto_converter.shutdown_event.is_set():
                return
            if not mount_ok:
                raise RuntimeError(f"converter reported mount/network failure for {cell_task}")
    finally:
        auto_converter._check_stop_requested = original_check_stop
        auto_converter._has_other_task_request = original_has_other_request
        auto_converter.shutdown_event.clear()


async def _run_conversion(
    payload: Mapping[str, Any],
    *,
    check_cancel: CheckCancel | None = None,
) -> CancelledNormally | None:
    auto_converter = _load_auto_converter_module()
    cancel_requested = threading.Event()

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
    return await _run_conversion(job["payload"], check_cancel=check_cancel)


async def process_one_queued(*, idle_sleep: float = 1.0) -> None:
    """Single runtime tick — used by tests and runbooks."""
    await tick(
        worker_id="converter",
        supported_types=["convert"],
        handler=_handler,
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
            supported_types=["convert"],
            handler=_handler,
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
