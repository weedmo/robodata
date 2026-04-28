"""Service for reading and writing task instructions in meta/tasks.parquet."""

from __future__ import annotations

import os
import tempfile

import pyarrow as pa
import pyarrow.parquet as pq

from backend.datasets.services.dataset_service import dataset_service
from backend.datasets.services.dataset_registry import DatasetContext
from backend.datasets.services.task_parquet import get_task_text_column_name


def _default_ctx(ctx: DatasetContext | None = None):
    return ctx if ctx is not None else dataset_service


def get_tasks(ctx: DatasetContext | None = None) -> list[dict]:
    """Return all tasks as list of {task_index, task_instruction} dicts."""
    ds = _default_ctx(ctx)
    return [
        {"task_index": int(t["task_index"]), "task_instruction": str(t.get("task", ""))}
        for t in ds.tasks
    ]


def get_task(task_index: int, ctx: DatasetContext | None = None) -> dict:
    """Return a single task by index. Raises KeyError if not found."""
    ds = _default_ctx(ctx)
    for t in ds.tasks:
        if int(t["task_index"]) == task_index:
            return {"task_index": int(t["task_index"]), "task_instruction": str(t.get("task", ""))}
    raise KeyError(f"task_index {task_index!r} not found")


async def update_task(
    task_index: int,
    task_instruction: str,
    ctx: DatasetContext | None = None,
) -> dict:
    """Update task instruction in meta/tasks.parquet atomically."""
    ds = _default_ctx(ctx)
    file_path = ds.dataset_path / "meta" / "tasks.parquet"
    lock = ds.get_file_lock(file_path)

    async with lock:
        table: pa.Table = pq.read_table(file_path)
        task_indices = table.column("task_index").to_pylist()
        task_column = get_task_text_column_name(table)

        if task_index not in task_indices:
            raise KeyError(f"task_index {task_index!r} not found")
        if task_column is None:
            raise KeyError("tasks.parquet is missing a task text column")

        row_pos = task_indices.index(task_index)
        old_tasks: list[str] = table.column(task_column).to_pylist()
        old_tasks[row_pos] = task_instruction

        updated_table = table.set_column(
            table.schema.get_field_index(task_column),
            task_column,
            pa.array(
                old_tasks,
                type=table.schema.field(task_column).type,
            ),
        )

        tmp_fd, tmp_path = tempfile.mkstemp(dir=file_path.parent, suffix=".tmp")
        os.close(tmp_fd)
        try:
            pq.write_table(updated_table, tmp_path)
            os.replace(tmp_path, file_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        ds.reload_tasks()

    return {"task_index": task_index, "task_instruction": task_instruction}
