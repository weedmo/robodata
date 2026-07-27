"""Tests for browsing raw recording tasks through the converter API layer."""

import os
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

    def test_keeps_worker_accepted_recording_when_metacard_json_is_malformed(
        self,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.setattr(converter_service, "RAW_BASE", tmp_path)
        self._make(tmp_path, "cell006/pinksponge", "20260226_170029")
        metacard = (
            tmp_path
            / "cell006"
            / "pinksponge"
            / "20260226_170029"
            / "metacard.json"
        )
        metacard.write_text("{malformed", encoding="utf-8")

        recordings = converter_service.list_recordings("cell006/pinksponge")

        assert recordings == [
            {
                "serial": "20260226_170029",
                "recording": "cell006/pinksponge/20260226_170029",
                "task_name": "",
            }
        ]

    def test_keeps_worker_snapshot_membership_when_metadata_enrichment_races(
        self,
        monkeypatch,
    ):
        monkeypatch.setattr(
            converter_service,
            "scan_worker_recordings",
            lambda: {"cell006/pinksponge": ["20260226_170029"]},
        )

        def fail_enrichment(*_args, **_kwargs):
            raise OSError("recording changed after scan")

        monkeypatch.setattr(
            converter_service,
            "inspect_worker_recording",
            fail_enrichment,
        )

        assert converter_service.list_recordings("cell006/pinksponge") == [
            {
                "serial": "20260226_170029",
                "recording": "cell006/pinksponge/20260226_170029",
                "task_name": "",
            }
        ]

    def test_rejects_task_traversal(self, tmp_path, monkeypatch):
        monkeypatch.setattr(converter_service, "RAW_BASE", tmp_path)

        with pytest.raises(ValueError):
            converter_service.list_recordings("../../etc")

    def test_excludes_symlink_recording_but_keeps_plain_hardlink_view(
        self,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.setattr(converter_service, "RAW_BASE", tmp_path)
        task = tmp_path / "cell006" / "pinksponge"
        external = tmp_path / "backing" / "20260226_170029"
        self._make(
            tmp_path,
            "backing",
            "20260226_170029",
        )
        task.mkdir(parents=True)
        (task / "20260226_170029").symlink_to(
            external,
            target_is_directory=True,
        )

        assert converter_service.list_recordings("cell006/pinksponge") == []

        linked = task / "20260226_164701"
        linked.mkdir()
        for source in external.iterdir():
            target_name = (
                "20260226_164701_0.mcap"
                if source.name.endswith("_0.mcap")
                else source.name
            )
            os.link(source, linked / target_name)

        recordings = converter_service.list_recordings("cell006/pinksponge")

        assert [item["serial"] for item in recordings] == ["20260226_164701"]


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

    def test_progress_totals_do_not_advertise_symlink_recordings(
        self,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.setattr(converter_service, "RAW_BASE", tmp_path)
        backing = tmp_path / "cell006" / ".backing" / "20260101_101010"
        self._recording(tmp_path, "cell006/.backing/20260101_101010")
        view = tmp_path / "cell006" / "linked"
        view.mkdir()
        (view / "20260101_101010").symlink_to(
            backing,
            target_is_directory=True,
        )

        assert converter_service.scan_raw_totals() == {}
        assert not converter_service.convert_target_has_recordings("cell006/linked")

    def test_api_scan_applies_same_minimal_permission_repair_as_worker(
        self,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.setattr(converter_service, "RAW_BASE", tmp_path)
        serial = "20260101_101010"
        recording = tmp_path / "cell006" / "repairable" / serial
        self._recording(tmp_path, f"cell006/repairable/{serial}")
        metacard = recording / "metacard.json"
        mcap = recording / f"{serial}_0.mcap"
        nested = recording / "nested.bin"
        nested.write_bytes(b"untouched")
        metacard.chmod(0o200)
        mcap.chmod(0o200)
        nested.chmod(0o200)
        recording.chmod(0o600)

        assert converter_service.scan_raw_totals() == {
            "cell006/repairable": 1
        }
        assert recording.stat().st_mode & 0o777 == 0o700
        assert metacard.stat().st_mode & 0o777 == 0o600
        assert mcap.stat().st_mode & 0o777 == 0o600
        assert nested.stat().st_mode & 0o777 == 0o200

    @pytest.mark.parametrize(
        "linked_name",
        ["metacard.json", "20260101_101010_0.mcap"],
    )
    def test_api_and_worker_both_reject_unreadable_hardlinked_input(
        self,
        tmp_path,
        monkeypatch,
        linked_name,
    ):
        monkeypatch.setattr(converter_service, "RAW_BASE", tmp_path)
        serial = "20260101_101010"
        recording = tmp_path / "cell006" / "linked" / serial
        recording.mkdir(parents=True)
        (recording / "metacard.json").write_text("{}", encoding="utf-8")
        (recording / f"{serial}_0.mcap").write_bytes(b"mcap")
        linked_path = recording / linked_name
        linked_path.unlink()
        external = tmp_path / f"external-{linked_name}"
        external.write_bytes(b"external")
        os.link(external, linked_path)
        external.chmod(0o200)

        scanner_class, _ = converter_service._load_nas_contract()
        worker_tasks = scanner_class(tmp_path).scan()

        assert worker_tasks == {}
        assert converter_service.scan_raw_totals() == {}
        assert not converter_service.convert_target_has_recordings(
            "cell006/linked"
        )
        assert external.stat().st_nlink == 2
        assert linked_path.stat().st_ino == external.stat().st_ino
        assert external.stat().st_mode & 0o777 == 0o200
        assert linked_path.stat().st_mode & 0o777 == 0o200
