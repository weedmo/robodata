"""Docker CLI wrapper for the bundled rosbag2lerobot-svt auto_converter."""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import re
import socket
import stat
import sys
import threading
import time
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
COMPOSE_SERVICES = ("app", "nginx", "db", "converter", "curation-worker")

# NAS paths (host-side) — same mount that Docker maps to /data
_DATA_ROOT = Path(
    os.environ.get("CONVERTER_DATA_ROOT")
    or os.environ.get("CURATION_DATASET_ROOT_BASE")
    or "/mnt/synology/data/data_div/2026_1"
)
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
_progress_refresh_task: asyncio.Task[None] | None = None
_RAW_SCAN_TTL = float(os.environ.get("CONVERTER_RAW_SCAN_TTL", "60"))
_raw_scan_cache: tuple[
    float,
    Path,
    dict[str, tuple[str, ...]],
] | None = None
_raw_scan_cache_lock = threading.Lock()

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

def is_plain_directory(path: Path) -> bool:
    """Return True only for a directory entry that is not a symlink."""
    try:
        return stat.S_ISDIR(path.stat(follow_symlinks=False).st_mode)
    except OSError:
        return False


def is_worker_plain_directory(
    relative_path: str,
    *,
    raw_base: Path | None = None,
) -> bool:
    """Validate raw-root-relative directory components without following links."""
    try:
        parts = (
            ()
            if not relative_path
            else _validate_rel_path(relative_path).parts
        )
    except ValueError:
        return False
    root = Path(raw_base) if raw_base is not None else RAW_BASE
    flags = (
        getattr(os, "O_PATH", os.O_RDONLY)
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptors: list[int] = []
    try:
        current = os.open(root, flags)
        descriptors.append(current)
        if not stat.S_ISDIR(os.fstat(current).st_mode):
            return False
        for component in parts:
            current = os.open(component, flags, dir_fd=current)
            descriptors.append(current)
            if not stat.S_ISDIR(os.fstat(current).st_mode):
                return False
        return True
    except OSError:
        return False
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _load_nas_contract():
    """Load the converter worker's fd-rooted raw-recording contract."""
    conversion_repo = CURATION_TOOLS_ROOT / "rosbag2lerobot-svt"
    if conversion_repo.is_dir() and str(conversion_repo) not in sys.path:
        sys.path.insert(0, str(conversion_repo))
    scanner_module = importlib.import_module("nas.scanner")
    access_module = importlib.import_module("nas.recording_access")
    return scanner_module.NASScanner, access_module.inspect_recording


def scan_worker_recordings(
    raw_base: Path | None = None,
    *,
    cached: bool = False,
) -> dict[str, list[str]]:
    """Return exactly the recordings accepted by the converter worker.

    ``NASScanner`` performs the same descriptor-rooted, no-follow access probe
    used immediately before conversion.  The probe may apply the worker's
    narrowly scoped owner-mode repair; keeping that behavior here ensures an
    API preflight cannot advertise a different set from the queued worker.
    """
    root = Path(raw_base) if raw_base is not None else RAW_BASE
    if not cached:
        scanner_class, _ = _load_nas_contract()
        return scanner_class(root).scan()

    global _raw_scan_cache
    now = time.monotonic()
    with _raw_scan_cache_lock:
        snapshot = _raw_scan_cache
        if (
            snapshot is not None
            and snapshot[1] == root
            and (now - snapshot[0]) < _RAW_SCAN_TTL
        ):
            return {
                cell_task: list(serials)
                for cell_task, serials in snapshot[2].items()
            }
        scanner_class, _ = _load_nas_contract()
        scanned = scanner_class(root).scan()
        _raw_scan_cache = (
            time.monotonic(),
            root,
            {
                cell_task: tuple(serials)
                for cell_task, serials in scanned.items()
            },
        )
        return scanned


def inspect_worker_recording(
    recording: str,
    *,
    raw_base: Path | None = None,
    load_metacard: bool = False,
):
    """Inspect one raw-root-relative recording through the worker contract."""
    rel = _validate_rel_path(recording)
    root = Path(raw_base) if raw_base is not None else RAW_BASE
    _, inspect_recording = _load_nas_contract()
    return inspect_recording(
        root,
        rel.parts,
        f"{rel.name}_0.mcap",
        load_metacard=load_metacard,
    )


def scan_raw_totals() -> dict[str, int]:
    """Scan NAS raw/ through the worker contract and return task totals."""
    return {
        cell_task: len(serials)
        for cell_task, serials in scan_worker_recordings(cached=True).items()
    }


def normalize_convert_target(target: str) -> str:
    """Canonicalize one API convert target before state, queue, and dedupe use."""
    normalized = target.strip().strip("/")
    rel = _validate_rel_path(normalized)
    return rel.as_posix()


def convert_target_has_recordings(target: str) -> bool:
    """Return whether a requested task/cell is worker-discoverable."""
    normalized = normalize_convert_target(target)
    recordings = scan_worker_recordings()
    if normalized in recordings:
        return True
    if "/" not in normalized:
        prefix = f"{normalized}/"
        return any(cell_task.startswith(prefix) for cell_task in recordings)
    return False


def read_state() -> dict:
    """Read convert_state.json from NAS."""
    try:
        if STATE_FILE.is_file():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def mark_failed_recordings_retryable(cell_task: str) -> int:
    """Move permanent failures for a task back into the retry queue."""
    state = read_state()
    entry = state.get(cell_task)
    if not isinstance(entry, dict):
        raise ValueError(f"no converter state found for task: {cell_task}")

    failed = entry.get("failed_serials", [])
    if not isinstance(failed, list):
        failed = list(failed)
    failed_serials = sorted({str(serial) for serial in failed if serial})
    if not failed_serials:
        return 0

    now = time.time()
    transient = entry.setdefault("transient_failed", {})
    if not isinstance(transient, dict):
        transient = {}
        entry["transient_failed"] = transient

    for serial in failed_serials:
        transient[serial] = {
            "attempt_count": 0,
            "first_failed_at": now,
            "next_retry_at": 0,
            "last_error": "manual retry requested from UI",
        }

    entry["failed_serials"] = []
    entry["last_updated"] = _dt_now()
    _write_state(state)
    _clear_progress_cache()
    return len(failed_serials)


def _dt_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _write_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def _clear_progress_cache() -> None:
    global _progress_cache
    _progress_cache = None


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


def list_tasks(cell: str) -> list[dict]:
    """List raw tasks under a RAW_BASE-relative cell for hierarchical browsing.

    Handles both 2-level (cell/task/serial) and 3-level (cell/task/subtask/serial)
    layouts. Returns ``[{task, name, count}]`` where ``task`` is the RAW_BASE-
    relative path to the directory that directly holds recordings.
    """
    canonical_cell = _validate_rel_path(cell).as_posix()
    prefix = f"{canonical_cell}/"
    return [
        {
            "task": cell_task,
            "name": cell_task.removeprefix(prefix),
            "count": len(serials),
        }
        for cell_task, serials in sorted(scan_worker_recordings().items())
        if cell_task.startswith(prefix)
    ]


def list_recordings(task: str) -> list[dict]:
    """List raw recordings (1 mcap = 1 episode) under a RAW_BASE-relative task.

    Returns ``[{serial, recording, task_name}]`` for serial dirs that hold both
    ``metacard.json`` and ``{serial}_0.mcap``, sorted by serial.
    """
    canonical_task = _validate_rel_path(task).as_posix()
    serials = scan_worker_recordings().get(canonical_task, [])
    recordings: list[dict] = []
    for serial in serials:
        recording = f"{canonical_task}/{serial}"
        metadata: object = {}
        try:
            report = inspect_worker_recording(
                recording,
                load_metacard=True,
            )
            metadata = json.loads(report.metacard_text or "{}")
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            # Membership comes from the canonical worker scan above. Metadata
            # enrichment is best-effort and must not silently change that set.
            pass
        recordings.append(
            {
                "serial": serial,
                "recording": recording,
                "task_name": str(
                    metadata.get("task_name", "")
                    if isinstance(metadata, dict)
                    else ""
                ),
            }
        )
    return recordings


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


async def _refresh_progress() -> None:
    """Refresh the filesystem-derived progress snapshot outside HTTP callers."""
    global _progress_cache

    try:
        tasks, summary = await asyncio.to_thread(build_progress)
    except (OSError, ValueError) as exc:
        logger.error("build_progress failed: %s", exc)
        cache = _progress_cache
        tasks, summary = (
            (cache[1], cache[2])
            if cache is not None
            else ([], "Progress scan failed")
        )
    _progress_cache = (time.monotonic(), tasks, summary)


async def _cached_progress() -> tuple[list[TaskProgress], str]:
    """Return progress immediately and refresh expired snapshots in background.

    NAS scans can exceed nginx's request timeout under curation video load. A
    status request therefore never owns or waits for the scan: one task
    refreshes the snapshot while callers receive stale data. During a cold
    start, callers receive an explicit loading summary until refresh completes.
    """
    global _progress_refresh_task

    cache = _progress_cache
    now = time.monotonic()
    if cache is not None and (now - cache[0]) < _PROGRESS_TTL:
        return cache[1], cache[2]

    refresh = _progress_refresh_task
    if refresh is None or refresh.done():
        _progress_refresh_task = asyncio.create_task(_refresh_progress())

    if cache is not None:
        return cache[1], cache[2]
    return [], "Progress scan in progress"


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
