"""Tests for unified dataset job handlers."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.mark.asyncio
async def test_split_handler_calls_engine_and_returns_result(monkeypatch):
    from backend.datasets.services import split_handler

    split = MagicMock()
    monkeypatch.setattr(split_handler.engine, "split_dataset", split)

    result = await split_handler.handle_split(
        {"payload": {"source_path": "/tmp/source", "episode_ids": [1, 2], "target_name": "out"}}
    )

    split.assert_called_once_with(Path("/tmp/source"), [1, 2], Path("/tmp/out"))
    assert result == {"result_path": "/tmp/out"}


@pytest.mark.asyncio
async def test_merge_handler_calls_engine_and_returns_result(monkeypatch):
    from backend.datasets.services import merge_handler

    merge = MagicMock()
    monkeypatch.setattr(merge_handler.engine, "merge_datasets", merge)

    result = await merge_handler.handle_merge(
        {"payload": {"source_paths": ["/tmp/a", "/tmp/b"], "target_name": "merged"}}
    )

    merge.assert_called_once_with([Path("/tmp/a"), Path("/tmp/b")], Path("/tmp/merged"))
    assert result == {"result_path": "/tmp/merged"}


@pytest.mark.asyncio
async def test_delete_handler_uses_rollback_for_in_place(monkeypatch):
    from backend.datasets.services import delete_handler

    calls = []

    def fake_rollback(path, fn):
        calls.append(path)
        fn(Path("/tmp/source.bak"), path)

    delete = MagicMock()
    monkeypatch.setattr(delete_handler, "run_in_place_with_rollback", fake_rollback)
    monkeypatch.setattr(delete_handler.engine, "delete_episodes", delete)

    result = await delete_handler.handle_delete(
        {"payload": {"source_path": "/tmp/source", "episode_ids": [3]}}
    )

    assert calls == [Path("/tmp/source")]
    delete.assert_called_once_with(Path("/tmp/source.bak"), [3], Path("/tmp/source"))
    assert result == {"result_path": "/tmp/source"}


@pytest.mark.asyncio
async def test_sync_good_episodes_handler_returns_summary(monkeypatch):
    from backend.datasets.services import sync_good_episodes_handler

    sync = MagicMock(
        return_value=SimpleNamespace(
            destination_path="/tmp/dest",
            mode="merge",
            created=2,
            skipped_duplicates=1,
        )
    )
    monkeypatch.setattr(sync_good_episodes_handler, "load_sync_selected_episodes", lambda: sync)

    result = await sync_good_episodes_handler.handle_sync_good_episodes(
        {"payload": {"source_path": "/tmp/source", "episode_ids": [1, 2], "destination_path": "/tmp/dest"}}
    )

    sync.assert_called_once_with(Path("/tmp/source"), [1, 2], Path("/tmp/dest"))
    assert result == {
        "result_path": "/tmp/dest",
        "summary": {"mode": "merge", "created": 2, "skipped_duplicates": 1},
    }
