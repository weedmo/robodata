"""Queue-driven converter entrypoint.

Bridges the parent-repo worker runtime to the converter implementation. The
real conversion lives in the `rosbag2lerobot-svt` submodule (see
`convert_task` in `rosbag2lerobot-svt/auto_converter.py`); wiring the
submodule's function in here is deferred to a separate PR so this shim can
ship without submodule surgery. Until then, `_run_conversion` raises
NotImplementedError — jobs go to `failed` rather than silently `complete`,
which keeps the API contract honest.

Tests monkeypatch `backend.converter.queue_adapter._run_conversion`.
"""
from __future__ import annotations
from typing import Any, Awaitable, Callable, Mapping

from backend.workers.runtime import (
    CancelledNormally,
    run_forever,
    tick,
)


CheckCancel = Callable[[], Awaitable[bool]]


async def _run_conversion(
    payload: Mapping[str, Any],
    *,
    check_cancel: CheckCancel | None = None,
) -> CancelledNormally | None:
    """Default impl — raises until the real converter is wired in.

    Replaced via monkeypatch in tests. Production wiring will call into the
    rosbag2lerobot-svt submodule's `convert_task` once that adapter PR lands.
    """
    raise NotImplementedError(
        "queue_adapter._run_conversion is not implemented yet — the real "
        "converter wiring lives in a follow-up PR. Tests should monkeypatch "
        "this attribute; production deployments must not reach this default."
    )


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
    await run_forever(
        worker_id="converter",
        supported_types=["convert"],
        handler=_handler,
    )
