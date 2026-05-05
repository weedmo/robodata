"""Shared helpers for jobs that rewrite a dataset in place."""
from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")


def run_in_place_with_rollback(target_path: Path, fn: Callable[[Path, Path], T]) -> T:
    """Run ``fn(backup, target)`` after moving target aside, restoring on failure."""
    backup = target_path.with_suffix(target_path.suffix + ".bak")
    target_path.rename(backup)
    try:
        result = fn(backup, target_path)
        shutil.rmtree(backup)
        return result
    except Exception:
        if target_path.exists():
            shutil.rmtree(target_path)
        backup.rename(target_path)
        raise
