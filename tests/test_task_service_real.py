"""Tests for TaskService against real LeRobot v3.0 datasets."""

import pytest
import pyarrow.parquet as pq

from backend.core.config import settings
from backend.datasets.services.dataset_registry import DatasetRegistry
from backend.services import task_service


def _setup_services(dataset_path):
    """Return a DatasetContext for the given dataset."""
    root = str(dataset_path.parent)
    if root not in settings.allowed_dataset_roots:
        settings.allowed_dataset_roots = settings.allowed_dataset_roots + [root]
    return DatasetRegistry(max_size=2).get(dataset_path)


# ---------------------------------------------------------------------------
# get_tasks
# ---------------------------------------------------------------------------

class TestGetTasks:
    def test_basic_aic_returns_two_tasks(self, basic_aic_path):
        ctx = _setup_services(basic_aic_path)
        tasks = task_service.get_tasks(ctx)
        assert len(tasks) == 2

    def test_hojun_returns_one_task(self, hojun_path):
        ctx = _setup_services(hojun_path)
        tasks = task_service.get_tasks(ctx)
        assert len(tasks) == 1

    def test_task_has_required_fields(self, basic_aic_path):
        ctx = _setup_services(basic_aic_path)
        tasks = task_service.get_tasks(ctx)
        for t in tasks:
            assert "task_index" in t
            assert "task_instruction" in t

    def test_basic_aic_task_instructions(self, basic_aic_path):
        ctx = _setup_services(basic_aic_path)
        tasks = task_service.get_tasks(ctx)
        task_map = {t["task_index"]: t["task_instruction"] for t in tasks}
        assert task_map[0] == "insert sfp cable into port"
        assert task_map[1] == "default_task"


# ---------------------------------------------------------------------------
# get_task
# ---------------------------------------------------------------------------

class TestGetTask:
    def test_returns_single_task(self, basic_aic_path):
        ctx = _setup_services(basic_aic_path)
        t = task_service.get_task(0, ctx)
        assert t["task_index"] == 0
        assert t["task_instruction"] == "insert sfp cable into port"

    def test_raises_for_nonexistent_task(self, basic_aic_path):
        ctx = _setup_services(basic_aic_path)
        with pytest.raises(KeyError):
            task_service.get_task(9999, ctx)


# ---------------------------------------------------------------------------
# update_task
# ---------------------------------------------------------------------------

class TestUpdateTask:
    @pytest.mark.asyncio
    async def test_update_task_instruction(self, writable_basic_aic):
        ctx = _setup_services(writable_basic_aic)
        result = await task_service.update_task(0, "new instruction", ctx)
        assert result["task_instruction"] == "new instruction"

    @pytest.mark.asyncio
    async def test_update_persists_to_parquet(self, writable_basic_aic):
        ctx = _setup_services(writable_basic_aic)
        await task_service.update_task(0, "persisted instruction", ctx)

        # Re-read directly from disk
        tasks_file = ctx.dataset_path / "meta" / "tasks.parquet"
        table = pq.read_table(tasks_file)
        task_indices = table.column("task_index").to_pylist()
        row_pos = task_indices.index(0)
        assert table.column("task").to_pylist()[row_pos] == "persisted instruction"

    @pytest.mark.asyncio
    async def test_update_reloads_cache(self, writable_basic_aic):
        ctx = _setup_services(writable_basic_aic)
        await task_service.update_task(0, "updated via service", ctx)

        # get_tasks should reflect the change
        tasks = task_service.get_tasks(ctx)
        task_map = {t["task_index"]: t["task_instruction"] for t in tasks}
        assert task_map[0] == "updated via service"

    @pytest.mark.asyncio
    async def test_update_nonexistent_raises(self, writable_basic_aic):
        ctx = _setup_services(writable_basic_aic)
        with pytest.raises(KeyError):
            await task_service.update_task(9999, "should fail", ctx)
