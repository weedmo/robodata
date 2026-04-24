"""Docker CLI wrapper for the bundled rosbag2lerobot-svt auto_converter."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
HOST_REQUEST_FILE = LEROBOT_BASE / "convert_requests.json"
HOST_RUNTIME_FILE = LEROBOT_BASE / "convert_runtime.json"
HOST_EVENTS_FILE = LEROBOT_BASE / "convert_events.jsonl"
HOST_STOP_FLAG = LEROBOT_BASE / "convert_stop.flag"

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
    last_updated: str | None = None


@dataclass
class ContainerStateInfo:
    status: str
    exit_code: int | None = None
    oom_killed: bool = False
    finished_at: str | None = None


@dataclass(frozen=True)
class HostControlInfo:
    active: bool
    updated_at: str | None = None
    active_cell_task: str | None = None


@dataclass
class ConverterStatus:
    container_state: str
    docker_available: bool
    task_start_available: bool = False
    tasks: list[TaskProgress] = field(default_factory=list)
    summary: str = ""
    exit_code: int | None = None
    oom_killed: bool = False
    finished_at: str | None = None
    active_cell_task: str | None = None

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

        last_updated_raw = entry.get("last_updated")
        last_updated = last_updated_raw if isinstance(last_updated_raw, str) else None
        tasks.append(TaskProgress(
            cell_task=key,
            total=total,
            done=done,
            pending=pending,
            failed=failed,
            retry=retry,
            last_updated=last_updated,
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


def _parse_container_state(stdout: str) -> ContainerStateInfo:
    """Parse ``docker inspect --format '{{json .State}}'`` output."""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return ContainerStateInfo(status="stopped")

    if not isinstance(payload, dict):
        return ContainerStateInfo(status="stopped")

    status = payload.get("Status") or "stopped"
    terminal_state = status in {"exited", "dead"}

    exit_code = payload.get("ExitCode") if terminal_state else None
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        exit_code = None

    finished_at = payload.get("FinishedAt") if terminal_state else None
    if not isinstance(finished_at, str) or not finished_at.strip() or finished_at == "0001-01-01T00:00:00Z":
        finished_at = None

    return ContainerStateInfo(
        status=status,
        exit_code=exit_code,
        oom_killed=bool(payload.get("OOMKilled", False)),
        finished_at=finished_at,
    )


def _normalize_exposed_container_state(state: str) -> str:
    """Map Docker terminal states onto the API's exposed container contract."""
    if state in {"exited", "dead"}:
        return "stopped"
    return state


def _read_json_dict(path: Path) -> dict[str, object]:
    """Read a JSON object from *path*; malformed/missing files return an empty dict."""
    try:
        if not path.is_file():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json_dict_atomic(path: Path, payload: dict[str, object]) -> None:
    """Atomically write a JSON object without creating an unexpected fallback path."""
    if not path.parent.is_dir():
        raise FileNotFoundError(f"Parent directory does not exist: {path.parent}")

    tmp_file = Path(f"{path}.tmp")
    tmp_file.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp_file.replace(path)


def _parse_host_runtime_timestamp(raw: object) -> datetime | None:
    """Parse host runtime timestamp values into a timezone-aware UTC datetime."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_host_control_info() -> HostControlInfo:
    """Return whether the host-managed converter heartbeat is fresh enough to trust."""
    payload = _read_json_dict(HOST_RUNTIME_FILE)
    updated_at = payload.get("updated_at")
    active_cell_task_raw = payload.get("active_cell_task")
    active_cell_task = active_cell_task_raw if isinstance(active_cell_task_raw, str) else None
    updated_str = updated_at if isinstance(updated_at, str) else None

    if payload.get("state") != "running":
        return HostControlInfo(active=False, updated_at=updated_str)

    heartbeat_at = _parse_host_runtime_timestamp(updated_at)
    if heartbeat_at is None:
        return HostControlInfo(active=False, updated_at=updated_str)

    scan_interval = payload.get("scan_interval_seconds")
    if not isinstance(scan_interval, int) or scan_interval <= 0:
        scan_interval = 60

    stale_after_seconds = max(600, scan_interval * 6 + 15)
    age_seconds = (datetime.now(timezone.utc) - heartbeat_at).total_seconds()
    if age_seconds > stale_after_seconds:
        return HostControlInfo(active=False, updated_at=updated_str, active_cell_task=active_cell_task)

    # Only trust active_cell_task when the heartbeat is very fresh — otherwise
    # it could belong to a task that finished minutes ago but wasn't cleared.
    if age_seconds > 30:
        active_cell_task = None

    return HostControlInfo(
        active=True,
        updated_at=updated_str,
        active_cell_task=active_cell_task,
    )


def request_host_stop() -> tuple[bool, str]:
    """Drop a stop flag for the host-managed converter to pick up.

    Used when Docker isn't reachable from the UI container: the running
    ``auto_converter`` polls this flag at task/recording boundaries and
    shuts down gracefully. Safe to call repeatedly; the flag is one-shot
    and the converter clears it on exit.
    """
    if not LEROBOT_BASE.is_dir():
        return False, f"LeRobot root is not available: {LEROBOT_BASE}"
    try:
        HOST_STOP_FLAG.parent.mkdir(parents=True, exist_ok=True)
        HOST_STOP_FLAG.write_text(
            datetime.now(timezone.utc).isoformat(),
            encoding="utf-8",
        )
    except OSError as exc:
        return False, f"failed to write stop flag: {exc}"
    return True, "stop_requested"


def enqueue_task_start_request(cell_task: str) -> tuple[bool, str]:
    """Queue a single task for the host-managed converter to pick up on its next cycle."""
    normalized = cell_task.strip()
    if not normalized:
        return False, "cell_task is required"
    if not LEROBOT_BASE.is_dir():
        return False, f"LeRobot root is not available: {LEROBOT_BASE}"

    pending = _read_json_dict(HOST_REQUEST_FILE)
    if normalized in pending:
        return True, "already queued"

    pending[normalized] = {"requested_at": datetime.now(timezone.utc).isoformat()}
    _write_json_dict_atomic(HOST_REQUEST_FILE, pending)
    return True, "queued"


def _can_restart_container(state: str) -> bool:
    """Return True when the current container state should permit a restart."""
    return state == "stopped"


def _is_benign_missing_container_error(output: str) -> bool:
    """Return True when Docker only reports a missing container plus warnings."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return False

    saw_missing_container = False
    for line in lines:
        if re.fullmatch(
            rf"(?:error(?: response from daemon)?:\s*)?no such container(?:[:\s]+[\"'`]?{re.escape(CONTAINER_NAME)}[\"'`]?)?",
            line,
            flags=re.IGNORECASE,
        ):
            saw_missing_container = True
            continue
        if re.match(r'^(?:warning:|time="[^"]+"\s+level=warning\b)', line, flags=re.IGNORECASE):
            continue
        return False

    return saw_missing_container


async def get_container_state_info() -> ContainerStateInfo:
    """Return structured container state information from Docker inspect."""
    rc, stdout, _ = await _run(
        ["docker", "inspect", CONTAINER_NAME, "--format", "{{json .State}}"],
        timeout=5.0,
    )
    if rc != 0:
        return ContainerStateInfo(status="stopped")
    return _parse_container_state(stdout.strip())


async def get_container_state() -> str:
    """Return the container state string (e.g. ``running``, ``exited``)."""
    return (await get_container_state_info()).status


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
        host_control = read_host_control_info()
        return ConverterStatus(
            container_state="running" if host_control.active else "stopped",
            docker_available=False,
            task_start_available=host_control.active,
            tasks=tasks,
            summary=progress_summary or "Host-controlled (Docker not reachable from UI)",
            active_cell_task=host_control.active_cell_task if host_control.active else None,
        )

    if _build_lock.locked():
        return ConverterStatus(
            container_state="building",
            docker_available=True,
            task_start_available=False,
            tasks=tasks,
            summary="Image build in progress",
        )

    state_info = await get_container_state_info()
    state = _normalize_exposed_container_state(state_info.status)

    return ConverterStatus(
        container_state=state,
        docker_available=True,
        task_start_available=state == "stopped",
        tasks=tasks,
        summary=progress_summary,
        exit_code=state_info.exit_code,
        oom_killed=state_info.oom_killed,
        finished_at=state_info.finished_at,
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
    state = _normalize_exposed_container_state(await get_container_state())
    if state == "running":
        return False, "Container already running"
    if not _can_restart_container(state):
        return False, f"Container in unexpected state: {state}"

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
    errors: list[str] = []

    rm_rc, rm_stdout, rm_stderr = await _run(
        ["docker", "rm", "-f", CONTAINER_NAME],
        timeout=15.0,
    )
    rm_output = "\n".join(
        part.strip() for part in (rm_stdout, rm_stderr) if part.strip()
    )
    if rm_rc != 0 and not _is_benign_missing_container_error(rm_output):
        errors.append(rm_output or "failed to remove one-off container")

    down_rc, down_stdout, down_stderr = await _run(_compose_cmd("down"), timeout=30.0)
    down_output = down_stderr.strip() or down_stdout.strip()
    if down_rc != 0:
        errors.append(down_output or "failed to stop")

    if errors:
        return False, " | ".join(errors)

    return True, down_stdout.strip() or "stopped"


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


async def stream_events_from_file(
    tail: int = 200,
    poll_interval_s: float = 1.0,
) -> AsyncGenerator[dict, None]:
    """Tail ``convert_events.jsonl`` on NAS for structured activity events.

    Used when the UI container has no Docker access and therefore cannot run
    ``docker logs``. The converter writes each event (converted /
    recording_start / finalizing / etc.) to this file so the UI's Activity
    panel can still populate.
    """
    def _read_tail_lines(limit: int) -> tuple[list[str], int]:
        try:
            if not HOST_EVENTS_FILE.is_file():
                return [], 0
            with open(HOST_EVENTS_FILE, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
            with open(HOST_EVENTS_FILE, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
            return lines[-limit:], size
        except OSError:
            return [], 0

    initial_lines, last_size = await asyncio.to_thread(_read_tail_lines, tail)
    for line in initial_lines:
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue

    while True:
        await asyncio.sleep(poll_interval_s)

        def _read_new(offset: int) -> tuple[list[str], int]:
            try:
                if not HOST_EVENTS_FILE.is_file():
                    return [], offset
                current_size = HOST_EVENTS_FILE.stat().st_size
                if current_size == offset:
                    return [], offset
                if current_size < offset:
                    # File rotated/truncated: reread the tail.
                    with open(HOST_EVENTS_FILE, "r", encoding="utf-8", errors="replace") as fh:
                        lines = fh.readlines()
                    return lines[-tail:], current_size
                with open(HOST_EVENTS_FILE, "r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(offset)
                    new = fh.read()
                return new.splitlines(), current_size
            except OSError:
                return [], offset

        new_lines, last_size = await asyncio.to_thread(_read_new, last_size)
        for line in new_lines:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
