"""Tests for browsing raw recording tasks through the converter API layer."""

from pathlib import Path

import pytest

from backend.converter import service as converter_service


class TestListRecordings:
    def _make(self, base, task, serial, *, with_mcap=True):
        directory = Path(base) / task / serial
        directory.mkdir(parents=True)
        (directory / "metacard.json").write_text(
            '{"task_name": "pinksponge"}', encoding="utf-8"
        )
        if with_mcap:
            (directory / f"{serial}_0.mcap").write_bytes(b"")

    def test_lists_recordings_with_mcap_under_task(self, tmp_path, monkeypatch):
        monkeypatch.setattr(converter_service, "RAW_BASE", tmp_path)
        self._make(tmp_path, "cell006/pinksponge", "20260226_170029")
        self._make(tmp_path, "cell006/pinksponge", "20260226_164701")
        self._make(
            tmp_path,
            "cell006/pinksponge",
            "20260226_999999",
            with_mcap=False,
        )

        recordings = converter_service.list_recordings("cell006/pinksponge")

        serials = {recording["serial"] for recording in recordings}
        assert serials == {"20260226_170029", "20260226_164701"}
        selected = next(
            recording
            for recording in recordings
            if recording["serial"] == "20260226_170029"
        )
        assert selected["recording"] == "cell006/pinksponge/20260226_170029"
        assert selected["task_name"] == "pinksponge"

    def test_rejects_task_traversal(self, tmp_path, monkeypatch):
        monkeypatch.setattr(converter_service, "RAW_BASE", tmp_path)

        with pytest.raises(ValueError):
            converter_service.list_recordings("../../etc")


class TestListTasks:
    def _recording(self, base, relative_path):
        directory = Path(base) / relative_path
        directory.mkdir(parents=True)
        serial = directory.name
        (directory / "metacard.json").write_text("{}", encoding="utf-8")
        (directory / f"{serial}_0.mcap").write_bytes(b"")

    def test_lists_direct_and_nested_tasks(self, tmp_path, monkeypatch):
        monkeypatch.setattr(converter_service, "RAW_BASE", tmp_path)
        self._recording(tmp_path, "cell006/HZ_pick/20260101_101010")
        self._recording(tmp_path, "cell006/HZ_pick/20260101_111111")
        self._recording(tmp_path, "cell006/dualpick/spray/20260102_101010")

        tasks = converter_service.list_tasks("cell006")

        keys = {task["task"] for task in tasks}
        assert "cell006/HZ_pick" in keys
        assert "cell006/dualpick/spray" in keys
        counts = {task["task"]: task["count"] for task in tasks}
        assert counts["cell006/HZ_pick"] == 2
        assert counts["cell006/dualpick/spray"] == 1

    def test_rejects_cell_traversal(self, tmp_path, monkeypatch):
        monkeypatch.setattr(converter_service, "RAW_BASE", tmp_path)

        with pytest.raises(ValueError):
            converter_service.list_tasks("../etc")
