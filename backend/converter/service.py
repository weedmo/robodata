"""Docker CLI wrapper for the bundled rosbag2lerobot-svt auto_converter."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import socket
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import AsyncGenerator, Callable

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CURATION_TOOLS_ROOT = Path(__file__).resolve().parent.parent.parent
COMPOSE_FILE = CURATION_TOOLS_ROOT / "docker" / "compose.yml"
COMPOSE_ENV_FILE = (
    CURATION_TOOLS_ROOT / "docker" / ".env"
    if (CURATION_TOOLS_ROOT / "docker" / ".env").exists()
    else CURATION_TOOLS_ROOT / "docker" / ".env.example"
)
COMPOSE_PROFILE = "convert"
PROJECT_NAME = "curation-tools"
CONVERTER_SERVICE = "converter"
CONTAINER_NAME = "convert-server"
DOCKER_SOCKET = Path(os.environ.get("CURATION_DOCKER_SOCKET", "/var/run/docker.sock"))
DOCKER_PROJECT_NAME = os.environ.get("CURATION_DOCKER_PROJECT_NAME", "curation-tools")
COMPOSE_SERVICES = ("app", "nginx", "db", "rerun", "converter", "curation-worker")

# NAS paths (host-side) — same mount that Docker maps to /data
_DATA_ROOT = Path(os.environ.get(
    "CONVERTER_DATA_ROOT",
    "/mnt/synology/data/data_div/2026_1",
))
RAW_BASE = _DATA_ROOT / "raw"
LEROBOT_BASE = _DATA_ROOT / "lerobot"
STATE_FILE = LEROBOT_BASE / "convert_state.json"

SERIAL_RE = re.compile(r"^\d{8}_\d{6}(_\d+)?$")

# Shared Rerun gRPC sink the converter streams raw episodes into, so they show
# up in the same :18080/rerun/ viewer the app uses. Same value as the app's
# CURATION_RERUN_GRPC_URL; the converter reaches it over the compose network.
RERUN_GRPC_URL = os.environ.get("CURATION_RERUN_GRPC_URL", "rerun+grpc://rerun:9876")

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
    docker_services: list["DockerServiceStatus"] = field(default_factory=list)


@dataclass
class DockerServiceStatus:
    name: str
    state: str
    healthy: bool
    status: str | None = None

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compose_cmd(*args: str) -> list[str]:
    """Build a ``docker compose`` command list."""
    return [
        "docker", "compose",
        "--env-file", str(COMPOSE_ENV_FILE),
        "-p", PROJECT_NAME,
        "-f", str(COMPOSE_FILE),
        "--profile", COMPOSE_PROFILE,
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


def _decode_chunked(body: bytes) -> bytes:
    decoded = bytearray()
    idx = 0
    while idx < len(body):
        line_end = body.find(b"\r\n", idx)
        if line_end == -1:
            break
        size_line = body[idx:line_end].split(b";", 1)[0]
        try:
            size = int(size_line, 16)
        except ValueError:
            return body
        idx = line_end + 2
        if size == 0:
            break
        decoded.extend(body[idx:idx + size])
        idx += size + 2
    return bytes(decoded)


def _docker_socket_request(path: str, *, timeout: float = 1.0) -> tuple[int, bytes] | None:
    if not DOCKER_SOCKET.exists():
        return None

    request = (
        f"GET {path} HTTP/1.1\r\n"
        "Host: docker\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(DOCKER_SOCKET))
            client.sendall(request)
            chunks: list[bytes] = []
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
    except OSError:
        return None

    raw = b"".join(chunks)
    header, sep, body = raw.partition(b"\r\n\r\n")
    if not sep:
        return None
    status_line = header.split(b"\r\n", 1)[0]
    try:
        status_code = int(status_line.split()[1])
    except (IndexError, ValueError):
        return None
    if b"transfer-encoding: chunked" in header.lower():
        body = _decode_chunked(body)
    return status_code, body


def _docker_socket_post(
    path: str, body_obj: object, *, timeout: float = 240.0
) -> tuple[int, bytes] | None:
    """POST a JSON body to the docker socket and return ``(status, body)``."""
    if not DOCKER_SOCKET.exists():
        return None

    payload = json.dumps(body_obj).encode("utf-8")
    request = (
        f"POST {path} HTTP/1.1\r\n"
        "Host: docker\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii") + payload
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(DOCKER_SOCKET))
            client.sendall(request)
            chunks: list[bytes] = []
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
    except OSError:
        return None

    raw = b"".join(chunks)
    header, sep, resp_body = raw.partition(b"\r\n\r\n")
    if not sep:
        return None
    status_line = header.split(b"\r\n", 1)[0]
    try:
        status_code = int(status_line.split()[1])
    except (IndexError, ValueError):
        return None
    if b"transfer-encoding: chunked" in header.lower():
        resp_body = _decode_chunked(resp_body)
    return status_code, resp_body


def _docker_socket_json(path: str) -> object | None:
    response = _docker_socket_request(path)
    if response is None:
        return None
    status_code, body = response
    if status_code < 200 or status_code >= 300:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        return None


def _docker_socket_available() -> bool:
    response = _docker_socket_request("/_ping")
    return bool(response and response[0] == 200 and response[1].strip() == b"OK")


def _compose_containers() -> list[dict]:
    filters = urllib.parse.quote(json.dumps({
        "label": [f"com.docker.compose.project={DOCKER_PROJECT_NAME}"],
    }))
    payload = _docker_socket_json(f"/containers/json?all=1&filters={filters}")
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _container_for_service(service: str) -> dict | None:
    for container in _compose_containers():
        labels = container.get("Labels")
        if isinstance(labels, dict) and labels.get("com.docker.compose.service") == service:
            return container
    return None


def _inspect_container(container_id: str) -> dict | None:
    payload = _docker_socket_json(f"/containers/{urllib.parse.quote(container_id)}/json")
    return payload if isinstance(payload, dict) else None


def _service_status_from_container(service: str, container: dict | None) -> DockerServiceStatus:
    if container is None:
        return DockerServiceStatus(name=service, state="missing", healthy=False)

    state = str(container.get("State") or "unknown")
    status = container.get("Status")
    status_text = status if isinstance(status, str) else None
    status_lower = status_text.lower() if status_text else ""
    healthy = state == "running" and "unhealthy" not in status_lower
    return DockerServiceStatus(
        name=service,
        state=state,
        healthy=healthy,
        status=status_text,
    )


def list_docker_services() -> list[DockerServiceStatus]:
    if not _docker_socket_available():
        return [DockerServiceStatus(name="app", state="running", healthy=True)]

    return [
        _service_status_from_container(service, _container_for_service(service))
        for service in COMPOSE_SERVICES
    ]


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


def _validate_rel_path(rel_path: str) -> PurePosixPath:
    """Validate a RAW_BASE-relative path against traversal/absolute escapes."""
    if not rel_path or not rel_path.strip():
        raise ValueError("empty path")
    rel = PurePosixPath(rel_path)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"invalid path: {rel_path!r}")
    return rel


def list_recordings(task: str) -> list[dict]:
    """List raw recordings (1 mcap = 1 episode) under a RAW_BASE-relative task.

    Returns ``[{serial, recording, task_name}]`` for serial dirs that hold both
    ``metacard.json`` and ``{serial}_0.mcap``, sorted by serial.
    """
    rel = _validate_rel_path(task)
    task_dir = RAW_BASE / rel
    recordings: list[dict] = []
    if not task_dir.is_dir():
        return recordings

    for entry in sorted(task_dir.iterdir()):
        if not entry.is_dir() or not SERIAL_RE.match(entry.name):
            continue
        metacard = entry / "metacard.json"
        mcap = entry / f"{entry.name}_0.mcap"
        if not (metacard.is_file() and mcap.is_file()):
            continue
        task_name = ""
        try:
            task_name = json.loads(metacard.read_text(encoding="utf-8")).get(
                "task_name", ""
            )
        except (OSError, json.JSONDecodeError):
            task_name = ""
        recordings.append(
            {
                "serial": entry.name,
                "recording": f"{task}/{entry.name}",
                "task_name": task_name,
            }
        )
    return recordings


def _build_raw_viz_command(recording: str, sink_url: str) -> list[str]:
    """Build the exec ``Cmd`` that streams a raw episode to Rerun.

    The app container has no docker CLI (only the docker socket), so this is the
    ``Cmd`` array for a docker-socket exec in the converter container, not a
    ``docker exec`` shell invocation. ``recording`` is a path relative to
    ``RAW_BASE`` whose last component is the serial (1 mcap = 1 episode); the
    data mount maps the same path host- and container-side. Validates against
    traversal and a malformed serial.
    """
    rel = _validate_rel_path(recording)
    serial = rel.name
    if not SERIAL_RE.match(serial):
        raise ValueError(f"invalid serial: {serial!r}")

    container_dir = str(RAW_BASE / rel)
    # Source ROS first, then prepend /app to PYTHONPATH (do NOT overwrite it, or
    # the ROS python paths providing rclpy/rosbag2_py are lost).
    inner = (
        "source /opt/ros/jazzy/setup.bash && PYTHONPATH=/app:$PYTHONPATH "
        "python3 -m conversion.rerun_viz "
        f"--recording-dir {shlex.quote(container_dir)} "
        f"--connect {shlex.quote(sink_url)}"
    )
    return ["bash", "-lc", inner]


def _docker_socket_exec(
    container: str, cmd: list[str], *, timeout: float = 240.0
) -> tuple[int, str] | None:
    """Run *cmd* inside *container* via the docker socket exec API.

    Returns ``(exit_code, output)``, or ``None`` if the socket is unavailable.
    Uses a TTY so stdout/stderr arrive as a single un-multiplexed stream.
    """
    create = _docker_socket_post(
        f"/containers/{urllib.parse.quote(container)}/exec",
        {"AttachStdout": True, "AttachStderr": True, "Tty": True, "Cmd": cmd},
        timeout=timeout,
    )
    if create is None:
        return None
    status, body = create
    if status < 200 or status >= 300:
        return None
    try:
        exec_id = json.loads(body.decode("utf-8"))["Id"]
    except (ValueError, KeyError):
        return None

    start = _docker_socket_post(
        f"/exec/{urllib.parse.quote(exec_id)}/start",
        {"Detach": False, "Tty": True},
        timeout=timeout,
    )
    if start is None:
        return None
    output = start[1].decode("utf-8", errors="replace")

    inspect = _docker_socket_json(f"/exec/{urllib.parse.quote(exec_id)}/json")
    if not isinstance(inspect, dict):
        return None
    exit_code = inspect.get("ExitCode")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        return None
    return exit_code, output


async def visualize_raw_recording(
    recording: str, sink_url: str | None = None
) -> tuple[bool, str]:
    """Stream a raw rosbag recording into the shared Rerun viewer.

    Runs the extraction + logging inside the converter container (the only place
    with the ROS stack) via the docker socket exec API. Returns ``(ok, message)``.
    Raises ``ValueError`` for an invalid recording path so the router can answer 400.
    """
    cmd = _build_raw_viz_command(recording, sink_url or RERUN_GRPC_URL)

    rec_dir = RAW_BASE / PurePosixPath(recording)
    if not rec_dir.is_dir():
        return False, f"recording not found: {recording}"

    result = await asyncio.to_thread(_docker_socket_exec, CONTAINER_NAME, cmd, timeout=300.0)
    if result is None:
        return False, "converter container not reachable via docker socket"
    exit_code, output = result
    tail = output.strip().splitlines()[-1] if output.strip() else ""
    if exit_code == 0:
        return True, tail or "ok"
    return False, tail or f"raw visualization failed (exit {exit_code})"


async def check_docker() -> bool:
    """Return ``True`` if the Docker daemon is reachable."""
    rc, _, _ = await _run(["docker", "info"], timeout=5.0)
    return rc == 0 or await asyncio.to_thread(_docker_socket_available)


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
    rc, stdout, stderr = await _run(
        ["docker", "inspect", CONTAINER_NAME, "--format", "{{json .State}}"],
        timeout=5.0,
    )
    if rc != 0:
        if "No such container" in stderr:
            return ContainerStateInfo(status="stopped")
        container = await asyncio.to_thread(_container_for_service, CONVERTER_SERVICE)
        if container is None:
            return ContainerStateInfo(status="stopped")
        container_id = str(container.get("Id") or "")
        detail = await asyncio.to_thread(_inspect_container, container_id) if container_id else None
        state_payload = detail.get("State") if detail else None
        if isinstance(state_payload, dict):
            return _parse_container_state(json.dumps(state_payload))
        return ContainerStateInfo(status=str(container.get("State") or "stopped"))
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

    Progress is filesystem-derived (``convert_state.json`` on NAS + raw scan)
    and reported regardless of Docker availability. The worker queue (managed
    via ``backend.workers``/``backend.jobs``) is the canonical lifecycle: a
    Convert click enqueues work to ``/api/jobs`` and the worker picks it up
    on its own schedule. ``task_start_available`` therefore stays True
    independent of container_state — the queue accepts work at any time.
    The UI surfaces a separate ``WorkerControlPill`` to expose worker
    desired_state vs actual_state when operators need that signal.
    """
    tasks, progress_summary = await _cached_progress()
    docker_services = await asyncio.to_thread(list_docker_services)

    docker_ok = await check_docker()
    if not docker_ok:
        return ConverterStatus(
            container_state="stopped",
            docker_available=False,
            task_start_available=True,
            tasks=tasks,
            summary=progress_summary or "Docker not reachable from UI; queue still accepts work",
            docker_services=docker_services,
        )

    if _build_lock.locked():
        return ConverterStatus(
            container_state="building",
            docker_available=True,
            task_start_available=True,
            tasks=tasks,
            summary="Image build in progress",
            docker_services=docker_services,
        )

    state_info = await get_container_state_info()
    state = _normalize_exposed_container_state(state_info.status)

    return ConverterStatus(
        container_state=state,
        docker_available=True,
        task_start_available=True,
        tasks=tasks,
        summary=progress_summary,
        exit_code=state_info.exit_code,
        oom_killed=state_info.oom_killed,
        finished_at=state_info.finished_at,
        docker_services=docker_services,
    )


async def build_image(on_line: Callable[[str], None] | None = None) -> int:
    """Run ``docker compose build --no-cache``, streaming output via *on_line*.

    Returns the process exit code.
    """
    if _build_lock.locked():
        raise RuntimeError("Build already in progress")
    async with _build_lock:
        proc = await asyncio.create_subprocess_exec(
            *_compose_cmd("build", "--no-cache", CONVERTER_SERVICE),
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
    """Check state and start the queue worker container atomically."""
    del cell_task
    state = _normalize_exposed_container_state(await get_container_state())
    if state == "running":
        return False, "Container already running"
    if not _can_restart_container(state):
        return False, f"Container in unexpected state: {state}"

    await _run(["docker", "rm", "-f", CONTAINER_NAME], timeout=10.0)

    cmd = _compose_cmd(
        "up", "-d", "--build",
        CONVERTER_SERVICE,
    )
    rc, stdout, stderr = await _run(cmd, timeout=30.0)
    if rc == 0:
        return True, stdout.strip() or "started"
    return False, stderr.strip() or "failed to start"


async def stop_converter() -> tuple[bool, str]:
    """Stop converter containers without tearing down the full stack."""
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

    stop_rc, stop_stdout, stop_stderr = await _run(
        _compose_cmd("stop", CONVERTER_SERVICE),
        timeout=30.0,
    )
    stop_output = stop_stderr.strip() or stop_stdout.strip()
    if stop_rc != 0:
        errors.append(stop_output or "failed to stop")

    if errors:
        return False, " | ".join(errors)

    return True, stop_stdout.strip() or "stopped"


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
