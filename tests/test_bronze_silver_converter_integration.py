import sys
import types
from pathlib import Path

from backend.converter import queue_adapter
from backend.datasets.services import bronze_silver_pipeline as pipeline
from backend.workers import curation_worker


def test_write_rerun_rrd_uses_converter_rerun_viz(monkeypatch, tmp_path):
    calls = []
    conversion_pkg = types.ModuleType("conversion")
    rerun_viz = types.ModuleType("conversion.rerun_viz")

    def visualize_recording_dir(recording_dir, *, save_path, spawn):
        calls.append((recording_dir, save_path, spawn))
        Path(save_path).write_bytes(b"rrd")
        return []

    rerun_viz.visualize_recording_dir = visualize_recording_dir
    monkeypatch.setitem(sys.modules, "conversion", conversion_pkg)
    monkeypatch.setitem(sys.modules, "conversion.rerun_viz", rerun_viz)

    recording_dir = tmp_path / "bronze" / "cell001" / "pick_part" / "20260616_150000"
    recording_dir.mkdir(parents=True)
    rrd_path = tmp_path / "rrd" / "cell001" / "pick_part" / "20260616_150000.rrd"

    pipeline._write_rerun_rrd(recording_dir, rrd_path)

    assert rrd_path.read_bytes() == b"rrd"
    assert calls == [(str(recording_dir), str(rrd_path), False)]


def test_bronze_silver_batch_is_claimed_by_converter_worker_only():
    assert "bronze_silver_batch" not in curation_worker.HANDLERS
    # The converter image has the ROS/Rerun dependencies needed by the rrd export.
    assert queue_adapter.HANDLERS["bronze_silver_batch"] is pipeline.handle_bronze_silver_batch
