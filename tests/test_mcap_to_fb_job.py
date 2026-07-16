from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from backend.converter.mcap_to_fb_job import _convert_task_sync, _parse_payload
from backend.converter.mcap_to_fb_converter import ConversionCancelled
from tests.test_mcap_to_fb import _write_metacard, _write_source_mcap


def _source_episode(raw_base, cell_task, serial="20260716_120000"):
    episode = raw_base / cell_task / serial
    episode.mkdir(parents=True)
    _write_source_mcap(episode / f"{serial}_0.mcap")
    _write_metacard(episode / "metacard.json")
    return episode


def test_job_type_is_registered_on_converter_worker():
    queue_adapter = (
        Path(__file__).resolve().parents[1] / "backend/converter/queue_adapter.py"
    ).read_text(encoding="utf-8")
    assert '"mcap_to_fb_convert": handle_mcap_to_fb_convert' in queue_adapter


def test_payload_defaults_to_sibling_fb_task():
    assert _parse_payload({"cell_task": "cell001/fold"}) == (
        "cell001/fold",
        "cell001/fold_fb",
        300,
        False,
    )


@pytest.mark.parametrize(
    "payload, message",
    [
        ({}, "cell_task"),
        ({"cell_task": "/absolute/task"}, "form <cell>/<task>"),
        ({"cell_task": "cell/task", "output_cell_task": "cell/task"}, "must differ"),
        ({"cell_task": "cell/task", "messages_per_chunk": 0}, "greater than zero"),
        ({"cell_task": "cell/task", "force": "yes"}, "boolean"),
    ],
)
def test_payload_rejects_unsafe_or_invalid_values(payload, message):
    with pytest.raises(ValueError, match=message):
        _parse_payload(payload)


def test_job_converts_complete_recordings_and_reports_output(tmp_path):
    raw_base = tmp_path / "raw"
    serial = "20260716_120000"
    _source_episode(raw_base, "cell001/fold", serial)
    incomplete = raw_base / "cell001/fold/20260716_120001"
    incomplete.mkdir()
    (incomplete / "metacard.json").write_text("{}", encoding="utf-8")
    progress = []

    result = _convert_task_sync(
        {"cell_task": "cell001/fold", "messages_per_chunk": 2},
        raw_base=raw_base,
        cancel_requested=threading.Event(),
        progress_callback=progress.append,
    )

    output_episode = raw_base / "cell001/fold_fb" / serial
    assert result["output_cell_task"] == "cell001/fold_fb"
    assert result["output_path"] == str(raw_base / "cell001/fold_fb")
    assert result["converted_recordings"][0]["serial"] == serial
    assert result["skipped_recordings"] == [{
        "serial": "20260716_120001",
        "reason": "missing 20260716_120001_0.mcap",
    }]
    assert sorted((output_episode / "images/cam_head").glob("*.fb"))
    assert (output_episode / "state/state_0.mcap").is_file()
    manifest = json.loads((output_episode / "conversion_manifest.json").read_text())
    assert manifest["serial"] == serial
    assert progress[-1]["phase"] == "complete"


def test_job_cancel_does_not_publish_current_episode(tmp_path):
    raw_base = tmp_path / "raw"
    serial = "20260716_120000"
    _source_episode(raw_base, "cell001/fold", serial)
    cancelled = threading.Event()
    cancelled.set()

    with pytest.raises(ConversionCancelled):
        _convert_task_sync(
            {"cell_task": "cell001/fold"},
            raw_base=raw_base,
            cancel_requested=cancelled,
        )

    assert not (raw_base / "cell001/fold_fb" / serial).exists()
