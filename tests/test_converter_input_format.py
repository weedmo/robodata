from pathlib import Path

import pytest

from backend.converter.input_format import (
    normalize_requested_format,
    select_task_recordings,
)


def _recording(task_dir: Path, serial: str, format_: str, *, complete: bool = True) -> None:
    recording_dir = task_dir / serial
    recording_dir.mkdir(parents=True)
    (recording_dir / "metacard.json").write_text("{}", encoding="utf-8")
    if format_ in {"fb", "mixed"}:
        camera_dir = recording_dir / "images" / "cam_head"
        camera_dir.mkdir(parents=True)
        (camera_dir / "000.fb").touch()
        if complete:
            state_dir = recording_dir / "state"
            state_dir.mkdir()
            (state_dir / "state_0.mcap").touch()
    if format_ in {"mcap", "mixed"}:
        (recording_dir / f"{serial}_0.mcap").touch()


@pytest.mark.parametrize("raw", [None, "", "  ", "auto", " AUTO "])
def test_empty_format_is_auto(raw):
    assert normalize_requested_format(raw) == "auto"


def test_invalid_format_is_rejected():
    with pytest.raises(ValueError, match="auto, fb, mcap"):
        normalize_requested_format("parquet")


def test_auto_uses_first_recognizable_format_and_excludes_mismatch(tmp_path):
    task_dir = tmp_path / "cell" / "task"
    _recording(task_dir, "001", "fb")
    _recording(task_dir, "002", "mcap")
    _recording(task_dir, "003", "fb")

    selection = select_task_recordings(task_dir, ["003", "002", "001"], "auto")

    assert selection.task_format == "fb"
    assert selection.recordings == ("001", "003")
    assert [item.serial for item in selection.skipped] == ["002"]
    assert "use one format per task" in selection.skipped[0].reason


def test_state_mcap_does_not_make_fb_recording_mixed(tmp_path):
    task_dir = tmp_path / "cell" / "task"
    _recording(task_dir, "001", "fb")

    selection = select_task_recordings(task_dir, ["001"], "auto")

    assert selection.task_format == "fb"
    assert selection.recordings == ("001",)
    assert selection.skipped == ()


def test_fb_marker_selects_fb_even_while_state_upload_is_incomplete(tmp_path):
    task_dir = tmp_path / "cell" / "task"
    _recording(task_dir, "001", "fb", complete=False)
    _recording(task_dir, "002", "mcap")

    selection = select_task_recordings(task_dir, ["001", "002"], "auto")

    assert selection.task_format == "fb"
    assert selection.recordings == ()
    assert [item.serial for item in selection.skipped] == ["001", "002"]
    assert "missing state/*.mcap" in selection.skipped[0].reason
    assert "use one format per task" in selection.skipped[1].reason


def test_mixed_recording_is_excluded_and_does_not_choose_auto_format(tmp_path):
    task_dir = tmp_path / "cell" / "task"
    _recording(task_dir, "001", "mixed")
    _recording(task_dir, "002", "mcap")

    selection = select_task_recordings(task_dir, ["001", "002"], "auto")

    assert selection.task_format == "mcap"
    assert selection.recordings == ("002",)
    assert selection.skipped[0].serial == "001"
    assert selection.skipped[0].detected_format == "mixed"


def test_explicit_format_excludes_other_recordings(tmp_path):
    task_dir = tmp_path / "cell" / "task"
    _recording(task_dir, "001", "fb")
    _recording(task_dir, "002", "mcap")

    selection = select_task_recordings(task_dir, ["001", "002"], "mcap")

    assert selection.task_format == "mcap"
    assert selection.recordings == ("002",)
    assert selection.skipped[0].serial == "001"


def test_auto_excludes_task_when_no_recording_format_is_recognizable(tmp_path):
    task_dir = tmp_path / "cell" / "task"
    recording_dir = task_dir / "001"
    recording_dir.mkdir(parents=True)
    (recording_dir / "metacard.json").write_text("{}", encoding="utf-8")

    selection = select_task_recordings(task_dir, ["001"], "auto")

    assert selection.task_format is None
    assert selection.recordings == ()
    assert selection.skipped[0].serial == "001"
