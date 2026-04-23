"""Regression tests for MCAP pre-flight size skipping."""

from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path


SUBMODULE_ROOT = Path(__file__).resolve().parent.parent / "rosbag2lerobot-svt"


def _install_auto_converter_stubs(monkeypatch) -> None:
    fake_conversion = types.ModuleType("conversion")
    fake_conversion.__path__ = []  # type: ignore[attr-defined]

    fake_data_creator = types.ModuleType("conversion.data_creator")
    fake_data_creator.DataCreator = object

    fake_mcap_reader = types.ModuleType("conversion.mcap_reader")
    fake_mcap_reader.build_extraction_config = lambda **kwargs: None

    fake_pipeline = types.ModuleType("conversion.pipeline")
    fake_pipeline.convert_single_recording = lambda **kwargs: 0

    fake_conversion.data_creator = fake_data_creator
    fake_conversion.mcap_reader = fake_mcap_reader
    fake_conversion.pipeline = fake_pipeline

    fake_nas = types.ModuleType("nas")
    fake_nas.HEALTH_FILE = Path("/tmp/healthy")
    fake_nas.HZ_MIN_RATIO = 0.7
    fake_nas.LEROBOT_BASE = Path("/tmp/lerobot")
    fake_nas.RAW_BASE = Path("/tmp/raw")
    fake_nas.SCAN_INTERVAL = 60
    fake_nas.STATE_FILE = Path("/tmp/state.json")
    fake_nas.CircuitBreaker = object
    fake_nas.ConvertState = object
    fake_nas.ErrorCategory = object
    fake_nas.NASScanner = object
    fake_nas.classify_error = lambda exc: None
    fake_nas.is_mount_healthy = lambda path: True

    monkeypatch.setitem(sys.modules, "conversion", fake_conversion)
    monkeypatch.setitem(sys.modules, "conversion.data_creator", fake_data_creator)
    monkeypatch.setitem(sys.modules, "conversion.mcap_reader", fake_mcap_reader)
    monkeypatch.setitem(sys.modules, "conversion.pipeline", fake_pipeline)
    monkeypatch.setitem(sys.modules, "nas", fake_nas)


def _load_auto_converter(monkeypatch):
    monkeypatch.syspath_prepend(str(SUBMODULE_ROOT))
    sys.modules.pop("auto_converter", None)
    return importlib.import_module("auto_converter")


def test_exceeds_mcap_size_limit_uses_env_and_file_size(tmp_path, monkeypatch):
    _install_auto_converter_stubs(monkeypatch)
    module = _load_auto_converter(monkeypatch)

    monkeypatch.setenv("MAX_MCAP_GB", "0")

    empty_mcap = tmp_path / "empty.mcap"
    empty_mcap.write_bytes(b"")

    tiny_mcap = tmp_path / "tiny.mcap"
    tiny_mcap.write_bytes(b"x")

    assert module._exceeds_mcap_size_limit(empty_mcap) is False
    assert module._exceeds_mcap_size_limit(tiny_mcap) is True


def test_read_max_mcap_gb_falls_back_to_default_for_invalid_values(monkeypatch):
    _install_auto_converter_stubs(monkeypatch)
    module = _load_auto_converter(monkeypatch)

    monkeypatch.setenv("MAX_MCAP_GB", "")
    assert module._read_max_mcap_gb() == 20

    monkeypatch.setenv("MAX_MCAP_GB", "not-a-number")
    assert module._read_max_mcap_gb() == 20

    monkeypatch.setenv("MAX_MCAP_GB", "-1")
    assert module._read_max_mcap_gb() == 20


class _RecordingState:
    def __init__(self) -> None:
        self.failed: list[tuple[str, str]] = []
        self.flushed = False

    def add_failed(self, cell_task: str, serial: str) -> None:
        self.failed.append((cell_task, serial))

    def flush(self) -> None:
        self.flushed = True


def test_convert_task_skips_oversized_mcap_before_setup(tmp_path, monkeypatch):
    _install_auto_converter_stubs(monkeypatch)
    monkeypatch.setenv("MAX_MCAP_GB", "0")
    module = _load_auto_converter(monkeypatch)

    raw_root = tmp_path / "raw"
    lerobot_root = tmp_path / "lerobot"
    monkeypatch.setattr(module, "RAW_BASE", raw_root)
    monkeypatch.setattr(module, "LEROBOT_BASE", lerobot_root)

    def _should_not_run(**kwargs):
        raise AssertionError("conversion setup should not run for oversized MCAPs")

    class _FailingCreator:
        def __init__(self, *args, **kwargs):
            raise AssertionError("DataCreator should not be constructed for oversized MCAPs")

    monkeypatch.setattr(module, "DataCreator", _FailingCreator)
    monkeypatch.setattr(module.mcap_reader, "build_extraction_config", _should_not_run)
    monkeypatch.setattr(module, "convert_single_recording", _should_not_run)

    serial = "20260416_173324_931248"
    recording_dir = raw_root / "cell005" / "Amore_dualpick" / serial
    recording_dir.mkdir(parents=True, exist_ok=True)
    (recording_dir / "metacard.json").write_text(
        json.dumps(
            {
                "fps": 30,
                "robot_type": "test_robot",
                "tags": ["tag-a"],
                "intervention": False,
                "is_succeed": True,
            }
        ),
        encoding="utf-8",
    )
    (recording_dir / f"{serial}_0.mcap").write_bytes(b"x")

    state = _RecordingState()

    ok = module.convert_task(
        cell="cell005",
        task="Amore_dualpick",
        recordings=[serial],
        state=state,
    )

    assert ok is True
    assert state.failed == [("cell005/Amore_dualpick", serial)]
    assert state.flushed is True


def test_convert_task_marks_stat_errors_failed_before_setup(tmp_path, monkeypatch):
    _install_auto_converter_stubs(monkeypatch)
    module = _load_auto_converter(monkeypatch)

    raw_root = tmp_path / "raw"
    lerobot_root = tmp_path / "lerobot"
    monkeypatch.setattr(module, "RAW_BASE", raw_root)
    monkeypatch.setattr(module, "LEROBOT_BASE", lerobot_root)

    def _should_not_run(**kwargs):
        raise AssertionError("conversion setup should not run when MCAP stat fails")

    class _FailingCreator:
        def __init__(self, *args, **kwargs):
            raise AssertionError("DataCreator should not be constructed when MCAP stat fails")

    monkeypatch.setattr(module, "DataCreator", _FailingCreator)
    monkeypatch.setattr(module.mcap_reader, "build_extraction_config", _should_not_run)
    monkeypatch.setattr(module, "convert_single_recording", _should_not_run)

    serial = "20260416_173324_931248"
    recording_dir = raw_root / "cell005" / "Amore_dualpick" / serial
    recording_dir.mkdir(parents=True, exist_ok=True)
    (recording_dir / "metacard.json").write_text(
        json.dumps({"fps": 30, "robot_type": "test_robot"}),
        encoding="utf-8",
    )
    mcap_path = recording_dir / f"{serial}_0.mcap"
    mcap_path.write_bytes(b"x")

    original_stat = Path.stat

    def _broken_stat(path: Path):
        if path == mcap_path:
            raise OSError("stat failed")
        return original_stat(path)

    monkeypatch.setattr(Path, "stat", _broken_stat)

    state = _RecordingState()
    ok = module.convert_task(
        cell="cell005",
        task="Amore_dualpick",
        recordings=[serial],
        state=state,
    )

    assert ok is True
    assert state.failed == [("cell005/Amore_dualpick", serial)]
    assert state.flushed is True
