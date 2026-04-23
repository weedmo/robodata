"""Tests for converter router log parsing."""

from backend.converter.router import _parse_log_line


def test_parse_converted_line_without_duration():
    event = _parse_log_line(
        "2026-04-17 06:51:34 [INFO] Converted: "
        "Amore_spray_clean_pick/20260417_115509_881292 (1943 frames)",
    )

    assert event == {
        "type": "converted",
        "ts": "2026-04-17 06:51:34",
        "recording": "Amore_spray_clean_pick/20260417_115509_881292",
        "frames": 1943,
        "duration": None,
    }


def test_parse_converted_line_with_duration():
    event = _parse_log_line(
        "2026-04-17 06:51:34 [INFO] Converted: "
        "Amore_spray_clean_pick/20260417_115509_881292 (1943 frames, 64.8s)",
    )

    assert event == {
        "type": "converted",
        "ts": "2026-04-17 06:51:34",
        "recording": "Amore_spray_clean_pick/20260417_115509_881292",
        "frames": 1943,
        "duration": 64.8,
    }


def test_parse_finalizing_line():
    event = _parse_log_line(
        "2026-04-21 12:34:56 [INFO]   Finalizing: cell001/pick_and_place",
    )

    assert event == {
        "type": "finalizing",
        "ts": "2026-04-21 12:34:56",
        "task": "cell001/pick_and_place",
    }


def test_parse_finalized_line():
    event = _parse_log_line(
        "2026-04-21 12:35:12 [INFO]   Finalized: cell001/pick_and_place",
    )

    assert event == {
        "type": "finalized",
        "ts": "2026-04-21 12:35:12",
        "task": "cell001/pick_and_place",
    }


def test_parse_finalizing_line_with_three_level_task():
    event = _parse_log_line(
        "2026-04-21 12:34:56 [INFO]   Finalizing: cell001/outer/inner",
    )

    assert event == {
        "type": "finalizing",
        "ts": "2026-04-21 12:34:56",
        "task": "cell001/outer/inner",
    }
