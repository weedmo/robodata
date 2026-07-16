"""Run the Data Foundry FlatBuffer converter in its GPU worker image."""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import tempfile
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


log = logging.getLogger(__name__)
ProgressCallback = Callable[[Mapping[str, Any]], None]
DEFAULT_DOCKER_SOCKET = "/var/run/docker.sock"
DEFAULT_IMAGE = "data_foundry-conversion-worker:latest"
DEFAULT_GPU_INFORMATION_ROOT = Path("/proc/driver/nvidia/gpus")


@dataclass(frozen=True)
class FBConverterGpuProfile:
    parallel: int
    nvenc_parallel: int


FB_CONVERTER_GPU_PROFILES: Mapping[str, FBConverterGpuProfile] = {
    "RTX 5090": FBConverterGpuProfile(parallel=3, nvenc_parallel=3),
    "RTX 4090": FBConverterGpuProfile(parallel=3, nvenc_parallel=2),
}
DEFAULT_GPU_PROFILE = FBConverterGpuProfile(parallel=1, nvenc_parallel=1)


class FBConversionCancelled(RuntimeError):
    """Raised after the GPU child container is stopped for cancellation."""


def _decode_chunked(body: bytes) -> bytes:
    decoded = bytearray()
    offset = 0
    while offset < len(body):
        line_end = body.find(b"\r\n", offset)
        if line_end < 0:
            return body
        try:
            size = int(body[offset:line_end].split(b";", 1)[0], 16)
        except ValueError:
            return body
        offset = line_end + 2
        if size == 0:
            return bytes(decoded)
        decoded.extend(body[offset:offset + size])
        offset += size + 2
    return bytes(decoded)


def _docker_request(
    method: str,
    path: str,
    *,
    body: object | None = None,
    timeout: float = 30.0,
) -> tuple[int, bytes]:
    socket_path = Path(os.environ.get("CURATION_DOCKER_SOCKET", DEFAULT_DOCKER_SOCKET))
    if not socket_path.exists():
        raise RuntimeError(
            f"FB conversion requires the Docker socket at {socket_path}"
        )

    payload = b"" if body is None else json.dumps(body).encode("utf-8")
    request = (
        f"{method} {path} HTTP/1.1\r\n"
        "Host: docker\r\n"
        "Connection: close\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n\r\n"
    ).encode("ascii") + payload

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(socket_path))
            client.sendall(request)
            chunks: list[bytes] = []
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
    except OSError as exc:
        raise RuntimeError(f"Docker API request failed: {exc}") from exc

    raw = b"".join(chunks)
    header, separator, response_body = raw.partition(b"\r\n\r\n")
    if not separator:
        raise RuntimeError("Docker API returned an invalid HTTP response")
    try:
        status = int(header.split(b"\r\n", 1)[0].split()[1])
    except (IndexError, ValueError) as exc:
        raise RuntimeError("Docker API returned an invalid status line") from exc
    if b"transfer-encoding: chunked" in header.lower():
        response_body = _decode_chunked(response_body)
    return status, response_body


def _docker_json(method: str, path: str, *, body: object | None = None) -> object:
    status, response_body = _docker_request(method, path, body=body)
    if not 200 <= status < 300:
        detail = response_body.decode("utf-8", errors="replace")
        raise RuntimeError(f"Docker API {method} {path} failed ({status}): {detail}")
    if not response_body:
        return {}
    try:
        return json.loads(response_body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("Docker API returned invalid JSON") from exc


def _decode_container_logs(raw: bytes) -> str:
    """Decode Docker's multiplexed stdout/stderr stream."""
    chunks: list[bytes] = []
    offset = 0
    while offset + 8 <= len(raw):
        size = int.from_bytes(raw[offset + 4:offset + 8], "big")
        end = offset + 8 + size
        if end > len(raw):
            return raw.decode("utf-8", errors="replace")
        chunks.append(raw[offset + 8:end])
        offset = end
    if offset != len(raw):
        return raw.decode("utf-8", errors="replace")
    return b"".join(chunks).decode("utf-8", errors="replace")


def _detect_gpu_model(
    information_root: Path = DEFAULT_GPU_INFORMATION_ROOT,
) -> str | None:
    override = os.environ.get("FB_CONVERTER_GPU_MODEL", "").strip()
    if override:
        return override

    for information_file in sorted(information_root.glob("*/information")):
        try:
            lines = information_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            key, separator, value = line.partition(":")
            if separator and key.strip().casefold() == "model":
                model = value.strip()
                if model:
                    return model
    return None


def _gpu_profile_for(model: str | None) -> FBConverterGpuProfile:
    normalized_model = (model or "").casefold()
    for model_fragment, profile in FB_CONVERTER_GPU_PROFILES.items():
        if model_fragment.casefold() in normalized_model:
            return profile
    return DEFAULT_GPU_PROFILE


def _parallel_setting(name: str, automatic_value: int) -> int:
    raw_value = os.environ.get(name, "auto").strip()
    if not raw_value or raw_value.casefold() == "auto":
        return automatic_value
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be 'auto' or a positive integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be 'auto' or a positive integer")
    return value


def _resolve_parallelism() -> tuple[str | None, int, int]:
    gpu_model = _detect_gpu_model()
    profile = _gpu_profile_for(gpu_model)
    parallel = _parallel_setting("FB_CONVERTER_PARALLEL", profile.parallel)
    nvenc_parallel = _parallel_setting(
        "FB_CONVERTER_NVENC_PARALLEL",
        profile.nvenc_parallel,
    )
    return gpu_model, parallel, nvenc_parallel


def _container_command(input_root: Path, output_root: Path) -> list[str]:
    gpu_model, parallel, nvenc_parallel = _resolve_parallelism()
    log.info(
        "FB converter GPU profile: model=%s parallel=%d nvenc_parallel=%d",
        gpu_model or "unknown",
        parallel,
        nvenc_parallel,
    )
    return [
        os.environ.get("FB_CONVERTER_BIN", "/usr/local/bin/flatbuffer_convert"),
        "--input", str(input_root),
        "--output", str(output_root),
        "--preset", "dataset",
        "--codec", "av1_nvenc",
        "--parallel", str(parallel),
        "--nvenc-parallel", str(nvenc_parallel),
    ]


def _run_gpu_container(
    *,
    command: list[str],
    data_root: Path,
    cancel_requested: Any,
    name_hint: str,
) -> str:
    image = os.environ.get("FB_CONVERTER_IMAGE", DEFAULT_IMAGE)
    safe_hint = "".join(
        ch if ch.isalnum() else "-" for ch in name_hint.lower()
    ).strip("-")
    container_name = f"curation-fb-{safe_hint[:32]}-{uuid.uuid4().hex[:8]}"
    create_path = f"/containers/create?name={urllib.parse.quote(container_name)}"
    create_payload = {
        "Image": image,
        "Cmd": command,
        "HostConfig": {
            "Binds": [f"{data_root}:{data_root}:rw"],
            "DeviceRequests": [{
                "Driver": "nvidia",
                "Count": -1,
                "Capabilities": [["gpu"]],
            }],
        },
    }
    created = _docker_json("POST", create_path, body=create_payload)
    if not isinstance(created, dict) or not isinstance(created.get("Id"), str):
        raise RuntimeError("Docker did not return an FB converter container ID")
    container_id = created["Id"]

    try:
        _docker_json("POST", f"/containers/{container_id}/start", body={})
        while True:
            if cancel_requested.is_set():
                try:
                    _docker_request("POST", f"/containers/{container_id}/stop?t=10", body={})
                finally:
                    raise FBConversionCancelled("FB conversion cancelled")

            inspected = _docker_json("GET", f"/containers/{container_id}/json")
            state = inspected.get("State") if isinstance(inspected, dict) else None
            if not isinstance(state, dict):
                raise RuntimeError("Docker returned no state for the FB converter")
            if not state.get("Running"):
                exit_code = state.get("ExitCode")
                status, raw_logs = _docker_request(
                    "GET",
                    f"/containers/{container_id}/logs?stdout=1&stderr=1&tail=200",
                )
                logs = _decode_container_logs(raw_logs) if status == 200 else ""
                if exit_code != 0:
                    tail = logs[-8000:].strip()
                    raise RuntimeError(
                        f"FB converter exited with code {exit_code}: {tail or 'no logs'}"
                    )
                return logs
            cancel_requested.wait(1.0)
    finally:
        try:
            _docker_request("DELETE", f"/containers/{container_id}?force=1")
        except RuntimeError:
            log.warning("Failed to remove FB converter container %s", container_id)


def _shared_data_root(raw_base: Path, lerobot_base: Path) -> Path:
    common = Path(os.path.commonpath([raw_base.resolve(), lerobot_base.resolve()]))
    if common == Path("/"):
        raise RuntimeError("RAW_BASE and LEROBOT_BASE must share a mounted data root")
    return common


def _validate_output(dataset_root: Path) -> None:
    required = [
        dataset_root / "meta" / "info.json",
        dataset_root / "meta" / "episodes",
        dataset_root / "data",
    ]
    missing = [
        str(path.relative_to(dataset_root))
        for path in required
        if not path.exists()
    ]
    if missing:
        raise RuntimeError(
            f"FB converter output is incomplete; missing {', '.join(missing)}"
        )
    if not any((dataset_root / "meta" / "episodes").rglob("*.parquet")):
        raise RuntimeError("FB converter output contains no episode metadata parquet")


def convert_fb_task(
    *,
    raw_base: Path,
    lerobot_base: Path,
    cell_task: str,
    recordings: list[str],
    cancel_requested: Any,
    progress_callback: ProgressCallback | None = None,
) -> None:
    """Rebuild one FB task in staging, then atomically replace its dataset."""
    if not recordings:
        raise ValueError(f"FB conversion has no eligible recordings for {cell_task}")

    task_dir = raw_base / cell_task
    destination = lerobot_base / cell_task
    destination.parent.mkdir(parents=True, exist_ok=True)
    data_root = _shared_data_root(raw_base, lerobot_base)

    with tempfile.TemporaryDirectory(
        prefix=f".{destination.name}.fb-",
        dir=destination.parent,
    ) as temporary:
        stage = Path(temporary)
        input_root = stage / "input"
        output_root = stage / "output"
        input_root.mkdir()
        output_root.mkdir()
        for serial in recordings:
            source = task_dir / serial
            if not source.is_dir():
                raise RuntimeError(f"FB recording disappeared before conversion: {source}")
            (input_root / serial).symlink_to(source, target_is_directory=True)

        if progress_callback is not None:
            progress_callback({
                "phase": "fb_converting",
                "cell_task": cell_task,
                "recording_total": len(recordings),
            })
        logs = _run_gpu_container(
            command=_container_command(input_root, output_root),
            data_root=data_root,
            cancel_requested=cancel_requested,
            name_hint=cell_task,
        )
        if cancel_requested.is_set():
            raise FBConversionCancelled("FB conversion cancelled")

        generated = output_root / "dataset"
        _validate_output(generated)
        (generated / ".conversion_format").write_text("fb\n", encoding="utf-8")

        backup = destination.parent / f".{destination.name}.backup-{uuid.uuid4().hex[:8]}"
        moved_existing = False
        try:
            if destination.exists():
                os.replace(destination, backup)
                moved_existing = True
            os.replace(generated, destination)
        except Exception:
            if moved_existing and not destination.exists() and backup.exists():
                os.replace(backup, destination)
            raise
        else:
            if moved_existing:
                shutil.rmtree(backup)

        if progress_callback is not None:
            progress_callback({
                "phase": "fb_finalized",
                "cell_task": cell_task,
                "converter_log_tail": logs[-2000:].strip(),
            })
