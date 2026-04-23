"""Docker CLI wrapper for the bundled rosbag2lerobot-svt auto_converter."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncGenerator, Callable

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CURATION_TOOLS_ROOT = Path(__file__).resolve().parent.parent.parent
COMPOSE_FILE = CURATION_TOOLS_ROOT / "docker" / "converter" / "docker-compose.yml"
PROJECT_NAME = "convert-server"
CONTAINER_NAME = "convert-server"

# NAS paths (host-side) — same mount that Docker maps to /data
_DATA_ROOT = Path(os.environ.get(
    "CONVERTER_DATA_ROOT",
    "/mnt/synology/data/data_div/2026_1",
))
RAW_BASE = _DATA_ROOT / "raw"
LEROBOT_BASE = _DATA_ROOT / "lerobot"
STATE_FILE = LEROBOT_BASE / "convert_state.json"

SERIAL_RE = re.compile(r"^\d{8}_\d{6}(_\d+)?$")

logger = logging.getLogger(__name__)

# Module-level lock to guard concurrent build requests
_build_lock = asyncio.Lock()

# Progress scan is filesystem-heavy (NAS walk + parquet metadata reads),
# but /api/converter/status is polled every 5s from every open tab. Memoize
# the result for a short window so poll cadence is decoupled from scan
# cadence. Override via CONVERTER_PROGRESS_TTL (seconds, float) for tests.
_PROGRESS_TTL = float(os.environ.get("CONVERTER_PROGRESS_TTL", "5"))
_progress_cache: tuple[float, list["TaskProgress"], str] | None = None
_progress_cache_lock = asyncio.Lock()

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class TaskProgress:
    cell_task: str
    total: int
    done: int
    pending: int
    failed: int
    retry: int


@dataclass
class ConverterStatus:
    container_state: str
    docker_available: bool
    tasks: list[TaskProgress] = field(default_factory=list)
    summary: str = ""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compose_cmd(*args: str) -> list[str]:
    """Build a ``docker compose`` command list."""
    return [
        "docker", "compose",
        "-p", PROJECT_NAME,
        "-f", str(COMPOSE_FILE),
        *args,
    ]


async def _run(cmd: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
    """Run *cmd* asynchronously and return (returncode, stdout, stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return -1, "", "command not found"
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", "timeout"
    return proc.returncode or 0, stdout.decode(), stderr.decode()


# ---------------------------------------------------------------------------
# NAS scanner (host-side, lightweight)
# ---------------------------------------------------------------------------

def _count_recordings(task_dir: Path) -> int:
    """Count valid recordings (serial pattern + metacard.json) in a task dir."""
    count = 0
    try:
        for entry in task_dir.iterdir():
            if entry.is_dir() and SERIAL_RE.match(entry.name):
                if (entry / "metacard.json").is_file():
                    count += 1
    except OSError:
        pass
    return count


def scan_raw_totals() -> dict[str, int]:
    """Scan NAS raw/ and return {cell_task: total_recordings}.

    Supports both 2-level (cell/task/serial) and 3-level (cell/task/subtask/serial).
    """
    totals: dict[str, int] = {}
    if not RAW_BASE.is_dir():
        return totals

    for cell_dir in sorted(RAW_BASE.iterdir()):
        if not cell_dir.is_dir() or cell_dir.name.startswith("."):
            continue
        for task_dir in sorted(cell_dir.iterdir()):
            if not task_dir.is_dir() or task_dir.name.startswith("."):
                continue

            cell_task = f"{cell_dir.name}/{task_dir.name}"

            # Check for direct serial dirs
            serials = 0
            subtask_dirs = []
            try:
                for entry in task_dir.iterdir():
                    if not entry.is_dir():
                        continue
                    if SERIAL_RE.match(entry.name):
                        if (entry / "metacard.json").is_file():
                            serials += 1
                    else:
                        subtask_dirs.append(entry)
            except OSError:
                continue

            if serials > 0:
                totals[cell_task] = serials
            elif subtask_dirs:
                # 3-level: cell/task/subtask/serial
                for sub_dir in sorted(subtask_dirs):
                    if sub_dir.name.startswith("."):
                        continue
                    sub_count = _count_recordings(sub_dir)
                    if sub_count > 0:
                        sub_key = f"{cell_dir.name}/{task_dir.name}/{sub_dir.name}"
                        totals[sub_key] = sub_count

    return totals


def read_state() -> dict:
    """Read convert_state.json from NAS."""
    try:
        if STATE_FILE.is_file():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return {}


# ---------------------------------------------------------------------------
# Progress (state file based)
# ---------------------------------------------------------------------------


def _count_output_episodes(cell_task: str) -> int | None:
    """Return the number of converted episodes written for *cell_task*."""
    dataset_dir = LEROBOT_BASE / cell_task
    episodes_dir = dataset_dir / "meta" / "episodes"
    if not episodes_dir.is_dir():
        return None

    parquet_files = sorted(episodes_dir.glob("chunk-*/file-*.parquet"))
    if parquet_files:
        try:
            import pyarrow.parquet as pq

            # read_metadata closes its own file handle; ParquetFile would leak
            # file descriptors under the /api/converter/status poll cadence.
            return sum(pq.read_metadata(path).num_rows for path in parquet_files)
        except Exception as exc:
            logger.warning("Failed to count episode parquet rows for %s: %s", cell_task, exc)

    info_path = dataset_dir / "meta" / "info.json"
    if not info_path.is_file():
        return None

    try:
        info = json.loads(info_path.read_text(encoding="utf-8").rstrip("\x00"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read %s: %s", info_path, exc)
        return None
    return int(info.get("total_episodes", 0))


def build_progress() -> tuple[list[TaskProgress], str]:
    """Build progress from state file + NAS scan. No log parsing needed."""
    state = read_state()
    totals = scan_raw_totals()

    all_keys = sorted(set(state.keys()) | set(totals.keys()))
    tasks: list[TaskProgress] = []
    sum_total = sum_done = sum_pending = sum_failed = 0

    for key in all_keys:
        total = totals.get(key, 0)
        entry = state.get(key, {})
        state_done = int(entry.get("converted_count", 0))
        actual_done = _count_output_episodes(key)
        if actual_done is not None and actual_done != state_done:
            logger.warning(
                "Progress mismatch for %s: state converted_count=%s, output episodes=%s",
                key,
                state_done,
                actual_done,
            )
        done = actual_done if actual_done is not None else state_done
        done = max(0, min(total, done))
        failed = len(entry.get("failed_serials", []))
        retry = len(entry.get("transient_failed", {}))
        pending = max(0, total - done - failed)

        if total == 0 and done == 0:
            continue

        tasks.append(TaskProgress(
            cell_task=key,
            total=total,
            done=done,
            pending=pending,
            failed=failed,
            retry=retry,
        ))
        sum_total += total
        sum_done += done
        sum_pending += pending
        sum_failed += failed

    summary = (
        f"{len(tasks)} tasks | {sum_total} recordings | "
        f"{sum_done} done | {sum_pending} pending | {sum_failed} failed"
    )
    return tasks, summary


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


async def check_docker() -> bool:
    """Return ``True`` if the Docker daemon is reachable."""
    rc, _, _ = await _run(["docker", "info"], timeout=5.0)
    return rc == 0


async def get_container_state() -> str:
    """Return the container state string (e.g. ``running``, ``exited``)."""
    rc, stdout, _ = await _run(
        ["docker", "inspect", CONTAINER_NAME, "--format", "{{.State.Status}}"],
        timeout=5.0,
    )
    if rc == 0:
        return stdout.strip()
    return "stopped"


async def _cached_progress() -> tuple[list[TaskProgress], str]:
    """Return a cached ``build_progress()`` snapshot, refreshing only on expiry.

    Without this cache every open tab's 5s status poll would trigger a fresh
    NAS walk + parquet metadata read. The TTL collapses concurrent polls
    into a single scan. Scan exceptions are logged and turned into an empty
    snapshot so callers always get a well-formed tuple.
    """
    import time

    global _progress_cache
    now = time.monotonic()
    cache = _progress_cache
    if cache is not None and (now - cache[0]) < _PROGRESS_TTL:
        return cache[1], cache[2]

    async with _progress_cache_lock:
        cache = _progress_cache
        if cache is not None and (now - cache[0]) < _PROGRESS_TTL:
            return cache[1], cache[2]
        try:
            tasks, summary = await asyncio.to_thread(build_progress)
        except (OSError, ValueError) as exc:
            logger.error("build_progress failed: %s", exc)
            tasks, summary = [], "Progress scan failed"
        _progress_cache = (time.monotonic(), tasks, summary)
        return tasks, summary


async def get_status() -> ConverterStatus:
    """Combine docker check, container state, and progress into a status.

    Progress is filesystem-derived (``convert_state.json`` on NAS + raw scan),
    so it is reported regardless of Docker availability. When Docker is
    unreachable — expected in the deployment mode scoped in the
    ui-service-docker-ops spec — the UI falls back to read-only progress
    monitoring while control actions are disabled on the client.
    """
    tasks, progress_summary = await _cached_progress()

    docker_ok = await check_docker()
    if not docker_ok:
        return ConverterStatus(
            container_state="unknown",
            docker_available=False,
            tasks=tasks,
            summary=progress_summary or "Host-controlled (Docker not reachable from UI)",
        )

    if _build_lock.locked():
        return ConverterStatus(
            container_state="building",
            docker_available=True,
            tasks=tasks,
            summary="Image build in progress",
        )

    state = await get_container_state()
    if state == "exited":
        state = "stopped"

    return ConverterStatus(
        container_state=state,
        docker_available=True,
        tasks=tasks,
        summary=progress_summary,
    )


async def build_image(on_line: Callable[[str], None] | None = None) -> int:
    """Run ``docker compose build --no-cache``, streaming output via *on_line*.

    Returns the process exit code.
    """
    if _build_lock.locked():
        raise RuntimeError("Build already in progress")
    async with _build_lock:
        proc = await asyncio.create_subprocess_exec(
            *_compose_cmd("build", "--no-cache"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        if proc.stdout is None:
            raise RuntimeError("Failed to capture build output")
        async for raw_line in proc.stdout:
            line = raw_line.decode(errors="replace").rstrip("\n")
            if on_line:
                on_line(line)
        await proc.wait()
        return proc.returncode or 0


async def start_converter(cell_task: str | None = None) -> tuple[bool, str]:
    """Check state and start container atomically. Returns (ok, message).

    When *cell_task* is provided, the container runs in single-shot mode:
    only that task is converted and the container exits on completion.
    """
    state = await get_container_state()
    if state == "running":
        return False, "Container already running"
    if state not in ("stopped", "exited"):
        return False, f"Container in unexpected state: {state}"

    # Remove old container if it exists (safe — not running)
    await _run(["docker", "rm", "-f", CONTAINER_NAME], timeout=10.0)

    env_args: list[str] = []
    if cell_task:
        env_args = [
            "-e", f"ONLY_CELL_TASK={cell_task}",
            "-e", "SINGLE_SHOT=1",
        ]

    cmd = _compose_cmd(
        "run", "-d", "--build",
        *env_args,
        "--name", CONTAINER_NAME,
        "convert-server",
        "python3", "/app/auto_converter.py",
    )
    rc, stdout, stderr = await _run(cmd, timeout=30.0)
    if rc == 0:
        return True, stdout.strip() or "started"
    return False, stderr.strip() or "failed to start"


async def stop_converter() -> tuple[bool, str]:
    """Stop and remove the converter stack. Returns (ok, message)."""
    rc, stdout, stderr = await _run(_compose_cmd("down"), timeout=30.0)
    if rc == 0:
        return True, stdout.strip() or "stopped"
    return False, stderr.strip() or "failed to stop"


async def stream_logs(tail: int = 200) -> AsyncGenerator[str, None]:
    """Async generator that streams container logs, yielding lines."""
    proc = await asyncio.create_subprocess_exec(
        "docker", "logs", "-f", "--tail", str(tail), CONTAINER_NAME,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    if proc.stdout is None:
        raise RuntimeError("Failed to capture log output")
    try:
        async for raw_line in proc.stdout:
            yield raw_line.decode(errors="replace").rstrip("\n")
    finally:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
