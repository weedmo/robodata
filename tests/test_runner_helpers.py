from pathlib import Path

import pytest

from backend.jobs.runner_helpers import run_in_place_with_rollback


def test_run_in_place_with_rollback_removes_backup_on_success(tmp_path: Path):
    target = tmp_path / "dataset"
    target.mkdir()
    (target / "original.txt").write_text("original")

    def rewrite(backup: Path, destination: Path) -> str:
        assert backup.exists()
        assert not destination.exists()
        destination.mkdir()
        (destination / "new.txt").write_text("new")
        return "done"

    assert run_in_place_with_rollback(target, rewrite) == "done"

    assert target.exists()
    assert not (tmp_path / "dataset.bak").exists()
    assert not (target / "original.txt").exists()
    assert (target / "new.txt").read_text() == "new"


def test_run_in_place_with_rollback_restores_backup_on_failure(tmp_path: Path):
    target = tmp_path / "dataset"
    target.mkdir()
    (target / "original.txt").write_text("original")

    def partially_rewrite_then_fail(backup: Path, destination: Path) -> None:
        assert backup.exists()
        destination.mkdir()
        (destination / "partial.txt").write_text("partial")
        raise RuntimeError("rewrite failed")

    with pytest.raises(RuntimeError, match="rewrite failed"):
        run_in_place_with_rollback(target, partially_rewrite_then_fail)

    assert target.exists()
    assert not (tmp_path / "dataset.bak").exists()
    assert (target / "original.txt").read_text() == "original"
    assert not (target / "partial.txt").exists()
