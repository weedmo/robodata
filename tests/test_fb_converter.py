import threading
from pathlib import Path

import pytest

from backend.converter import fb_converter


def _make_recording(raw_base: Path, cell_task: str, serial: str) -> None:
    recording = raw_base / cell_task / serial
    recording.mkdir(parents=True)
    (recording / "metacard.json").write_text("{}", encoding="utf-8")


def _write_valid_output(command: list[str]) -> Path:
    output_root = Path(command[command.index("--output") + 1]) / "dataset"
    (output_root / "meta" / "episodes" / "chunk-000").mkdir(parents=True)
    (output_root / "meta" / "info.json").write_text("{}", encoding="utf-8")
    (output_root / "meta" / "episodes" / "chunk-000" / "file-000.parquet").touch()
    (output_root / "data").mkdir()
    return output_root


@pytest.mark.parametrize(
    ("gpu_model", "expected_parallel", "expected_nvenc_parallel"),
    [
        ("NVIDIA GeForce RTX 5090", "3", "3"),
        ("NVIDIA GeForce RTX 4090", "3", "2"),
        ("NVIDIA GeForce RTX 5060 Ti", "1", "1"),
    ],
)
def test_container_command_selects_gpu_profile(
    tmp_path,
    monkeypatch,
    gpu_model,
    expected_parallel,
    expected_nvenc_parallel,
):
    monkeypatch.delenv("FB_CONVERTER_PARALLEL", raising=False)
    monkeypatch.delenv("FB_CONVERTER_NVENC_PARALLEL", raising=False)
    monkeypatch.setattr(fb_converter, "_detect_gpu_model", lambda: gpu_model)

    command = fb_converter._container_command(tmp_path / "input", tmp_path / "output")

    assert command[command.index("--parallel") + 1] == expected_parallel
    assert command[command.index("--nvenc-parallel") + 1] == expected_nvenc_parallel


def test_container_command_explicit_parallelism_overrides_gpu_profile(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        fb_converter,
        "_detect_gpu_model",
        lambda: "NVIDIA GeForce RTX 5090",
    )
    monkeypatch.setenv("FB_CONVERTER_PARALLEL", "5")
    monkeypatch.setenv("FB_CONVERTER_NVENC_PARALLEL", "2")

    command = fb_converter._container_command(tmp_path / "input", tmp_path / "output")

    assert command[command.index("--parallel") + 1] == "5"
    assert command[command.index("--nvenc-parallel") + 1] == "2"


def test_detect_gpu_model_reads_nvidia_proc_information(tmp_path, monkeypatch):
    monkeypatch.delenv("FB_CONVERTER_GPU_MODEL", raising=False)
    information = tmp_path / "0000:01:00.0" / "information"
    information.parent.mkdir()
    information.write_text(
        "Model: NVIDIA GeForce RTX 4090\nGPU UUID: GPU-test\n",
        encoding="utf-8",
    )

    assert fb_converter._detect_gpu_model(tmp_path) == "NVIDIA GeForce RTX 4090"


def test_fb_task_is_staged_then_atomically_replaces_destination(tmp_path, monkeypatch):
    raw_base = tmp_path / "raw"
    lerobot_base = tmp_path / "lerobot"
    cell_task = "cell/task"
    _make_recording(raw_base, cell_task, "001")
    _make_recording(raw_base, cell_task, "002")
    destination = lerobot_base / cell_task
    destination.mkdir(parents=True)
    (destination / "old-output").touch()

    def fake_run(**kwargs):
        command = kwargs["command"]
        input_root = Path(command[command.index("--input") + 1])
        assert (input_root / "001").is_symlink()
        assert (input_root / "002").is_symlink()
        _write_valid_output(command)
        return "converted two recordings"

    monkeypatch.setattr(fb_converter, "_run_gpu_container", fake_run)

    fb_converter.convert_fb_task(
        raw_base=raw_base,
        lerobot_base=lerobot_base,
        cell_task=cell_task,
        recordings=["001", "002"],
        cancel_requested=threading.Event(),
    )

    assert not (destination / "old-output").exists()
    assert (destination / ".conversion_format").read_text(encoding="utf-8") == "fb\n"
    assert (destination / "meta" / "info.json").is_file()
    assert not list(destination.parent.glob(".task.backup-*"))
    assert not list(destination.parent.glob(".task.fb-*"))


def test_invalid_fb_output_keeps_existing_destination(tmp_path, monkeypatch):
    raw_base = tmp_path / "raw"
    lerobot_base = tmp_path / "lerobot"
    cell_task = "cell/task"
    _make_recording(raw_base, cell_task, "001")
    destination = lerobot_base / cell_task
    destination.mkdir(parents=True)
    (destination / "old-output").write_text("keep", encoding="utf-8")

    def fake_run(**kwargs):
        output_root = Path(
            kwargs["command"][kwargs["command"].index("--output") + 1]
        ) / "dataset"
        output_root.mkdir(parents=True)
        return "incomplete"

    monkeypatch.setattr(fb_converter, "_run_gpu_container", fake_run)

    with pytest.raises(RuntimeError, match="output is incomplete"):
        fb_converter.convert_fb_task(
            raw_base=raw_base,
            lerobot_base=lerobot_base,
            cell_task=cell_task,
            recordings=["001"],
            cancel_requested=threading.Event(),
        )

    assert (destination / "old-output").read_text(encoding="utf-8") == "keep"
