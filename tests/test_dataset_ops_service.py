"""Tests for unified dataset job handlers."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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


@pytest.mark.asyncio
async def test_operation_intake_builds_split_payload_and_dedupe(monkeypatch, tmp_path):
    from backend.datasets.services import operation_intake

    source = tmp_path / "source"
    output = tmp_path / "out"
    source.mkdir()
    output.mkdir()
    enqueue = AsyncMock(return_value={"external_id": "job-1"})
    monkeypatch.setattr(operation_intake.jobs_repo, "enqueue", enqueue)
    req = SimpleNamespace(
        source_path=str(source),
        episode_ids=[1, 2],
        target_name="target",
        output_dir=str(output),
    )

    operation = await operation_intake.intake_split(
        req,
        validate_path=lambda path: Path(path).resolve(),
        validate_optional_path=lambda path: str(Path(path).resolve()) if path else None,
    )

    assert operation.job_id == "job-1"
    assert operation.operation == "split"
    enqueue.assert_awaited_once_with(
        type_="split",
        payload={
            "source_path": str(source.resolve()),
            "episode_ids": [1, 2],
            "target_name": "target",
            "output_dir": str(output.resolve()),
        },
        dedupe_key=f"split:{source.resolve()}:target",
    )


@pytest.mark.asyncio
async def test_operation_intake_translates_duplicate_dedupe(monkeypatch, tmp_path):
    from backend.datasets.services import operation_intake
    from backend.jobs import repo as jobs_repo

    source = tmp_path / "source"
    source.mkdir()
    enqueue = AsyncMock(side_effect=jobs_repo.DuplicateDedupe(77))
    monkeypatch.setattr(operation_intake.jobs_repo, "enqueue", enqueue)
    req = SimpleNamespace(source_path=str(source), overwrite=False)

    with pytest.raises(operation_intake.DuplicateDatasetOperation) as excinfo:
        await operation_intake.intake_stamp_cycles(
            req,
            validate_path=lambda path: Path(path).resolve(),
        )

    assert excinfo.value.existing_job_id == 77


def test_operation_intake_projects_terminal_job_status():
    from backend.datasets.services.operation_intake import project_job_status

    status = project_job_status(
        {
            "external_id": "ext-1",
            "type": "merge",
            "status": "failed",
            "created_at": "2024-01-01T00:00:00+00:00",
            "finished_at": "2024-01-01T00:01:00+00:00",
            "error": "boom",
            "result": {"result_path": "/tmp/out", "summary": {"created": 0}},
        }
    )

    assert status.job_id == "ext-1"
    assert status.operation == "merge"
    assert status.status == "failed"
    assert status.completed_at == "2024-01-01T00:01:00+00:00"
    assert status.error == "boom"
    assert status.result_path == "/tmp/out"
    assert status.summary == {"created": 0}
