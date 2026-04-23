"""Mockup test: exercises backend services directly against the mock dataset."""

import asyncio
import sys
from pathlib import Path

# Ensure the project root is on sys.path so 'backend' is importable
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.dataset_service import DatasetService
from backend.services.episode_service import EpisodeService
from backend.services import task_service
from backend.services.export_service import export_dataset
MOCK_DATASET = Path(__file__).parent / "mock_dataset"


def reset_mock_dataset() -> None:
    """Recreate the mock dataset from scratch so tests are idempotent."""
    from tests.create_mock_dataset import create_tasks_parquet, create_episodes_parquet, create_data_parquet
    create_tasks_parquet()
    create_episodes_parquet()
    create_data_parquet()


def make_services() -> tuple[DatasetService, EpisodeService]:
    """Create fresh service instances pointing at the mock dataset."""
    from backend.config import settings
    original_roots = settings.allowed_dataset_roots
    if str(MOCK_DATASET.parent) not in original_roots:
        settings.allowed_dataset_roots = original_roots + [str(MOCK_DATASET.parent)]
    ds = DatasetService()
    ds.load_dataset(MOCK_DATASET)

    # Patch the module-level singleton used by episode_service and task_service
    # Patch both shim modules and canonical modules
    import backend.services.dataset_service as ds_mod
    import backend.services.episode_service as ep_mod
    import backend.services.task_service as ts_mod
    import backend.services.export_service as ex_mod
    import backend.datasets.services.dataset_service as ds_canon
    import backend.datasets.services.episode_service as ep_canon
    import backend.datasets.services.task_service as ts_canon
    import backend.datasets.services.export_service as ex_canon

    ds_mod.dataset_service = ds
    ep_mod.dataset_service = ds
    ts_mod.dataset_service = ds
    ex_mod.dataset_service = ds
    ds_canon.dataset_service = ds
    ep_canon.dataset_service = ds
    ts_canon.dataset_service = ds
    ex_canon.dataset_service = ds

    es = EpisodeService()
    return ds, es


async def run_tests() -> None:
    reset_mock_dataset()

    # Initialise the SQLite DB (episode annotations now live in DB)
    import tempfile as _tmpmod
    from backend.core.db import init_db, close_db, _reset as _db_reset
    import backend.core.db as _db_mod
    _db_mod._db_path_override = str(Path(_tmpmod.mkdtemp()) / "test_mockup.db")
    await init_db()

    ds, es = make_services()

    # --- get_info ---
    info = ds.get_info()
    assert info["fps"] == 30, f"Expected fps=30, got {info['fps']}"
    assert info["total_episodes"] == 5, f"Expected total_episodes=5, got {info['total_episodes']}"
    assert info["total_tasks"] == 2, f"Expected total_tasks=2, got {info['total_tasks']}"
    assert info["robot_type"] == "mock_robot"
    print("PASS: get_info() returns correct metadata")

    # --- get_episodes ---
    episodes = await es.get_episodes()
    assert len(episodes) == 5, f"Expected 5 episodes, got {len(episodes)}"
    ep_indices = sorted(e["episode_index"] for e in episodes)
    assert ep_indices == [0, 1, 2, 3, 4], f"Unexpected episode indices: {ep_indices}"
    print("PASS: get_episodes() returns 5 episodes")

    # --- get_tasks ---
    tasks = task_service.get_tasks()
    assert len(tasks) == 2, f"Expected 2 tasks, got {len(tasks)}"
    task_instructions = {t["task_index"]: t["task_instruction"] for t in tasks}
    assert task_instructions[0] == "Pick up the red cube", f"Unexpected task 0: {task_instructions[0]}"
    assert task_instructions[1] == "Place the cube on the plate", f"Unexpected task 1: {task_instructions[1]}"
    print("PASS: get_tasks() returns 2 tasks with correct instructions")

    # --- update_episode 0 with grade="good" and tags=["good", "clean"] ---
    updated = await es.update_episode(episode_index=0, grade="good", tags=["good", "clean"])
    assert updated["grade"] == "good", f"Expected grade='good', got {updated['grade']}"
    assert updated["tags"] == ["good", "clean"], f"Unexpected tags: {updated['tags']}"
    print("PASS: update_episode() returns updated record with grade and tags")

    # Verify persistence: re-read from DB
    import json as json_verify
    from backend.core.db import get_db
    db = await get_db()
    async with db.execute(
        """SELECT a.grade, a.tags
           FROM annotations a
           JOIN episode_serials es ON es.serial_number = a.serial_number
           WHERE es.episode_index = 0"""
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None, "Episode 0 annotation not found in DB"
    assert row[0] == "good", f"Persisted grade mismatch: {row[0]}"
    assert json_verify.loads(row[1]) == ["good", "clean"], f"Persisted tags mismatch: {row[1]}"
    print("PASS: Episode update persisted correctly to DB")

    # --- update_task 0 instruction ---
    new_instruction = "Pick up the blue cube"
    result = await task_service.update_task(task_index=0, task_instruction=new_instruction)
    assert result["task_instruction"] == new_instruction, f"Unexpected result: {result}"
    print("PASS: update_task() returns updated task")

    # Verify persistence: re-read tasks.parquet directly
    import pyarrow.parquet as pq
    tasks_file = MOCK_DATASET / "meta" / "tasks.parquet"
    tasks_table = pq.read_table(tasks_file)
    task_idx_col = tasks_table.column("task_index").to_pylist()
    task_row_pos = task_idx_col.index(0)
    persisted_task = tasks_table.column("task").to_pylist()[task_row_pos]
    assert persisted_task == new_instruction, f"Persisted task mismatch: {persisted_task}"
    print("PASS: Task update persisted correctly to parquet")

    # --- export_dataset: mark episode 1 as Bad, export excluding Bad ---
    import tempfile, shutil, json as json_mod
    await es.update_episode(episode_index=1, grade="bad", tags=["collision"])
    export_dir = Path(tempfile.mkdtemp(prefix="curation_export_")) / "exported"
    result = export_dataset(str(export_dir), exclude_grades=["bad"])
    assert result["total_episodes"] == 4, f"Expected 4 exported episodes, got {result['total_episodes']}"
    assert result["excluded_count"] == 1, f"Expected 1 excluded, got {result['excluded_count']}"
    # Verify info.json
    exported_info = json_mod.loads((export_dir / "meta" / "info.json").read_text())
    assert exported_info["total_episodes"] == 4, f"Exported info.json total_episodes mismatch"
    # Verify tasks.parquet was copied
    assert (export_dir / "meta" / "tasks.parquet").exists(), "tasks.parquet not exported"
    # Verify episode parquet only has 4 episodes
    import pyarrow.parquet as pq2
    ep_files = list((export_dir / "meta" / "episodes").rglob("*.parquet"))
    assert len(ep_files) > 0, "No episode parquet files exported"
    total_exported_eps = 0
    for ef in ep_files:
        t = pq2.read_table(ef)
        total_exported_eps += t.num_rows
    assert total_exported_eps == 4, f"Expected 4 episode rows, got {total_exported_eps}"
    exported_indices = set()
    for ef in ep_files:
        t = pq2.read_table(ef)
        exported_indices.update(t.column("episode_index").to_pylist())
    assert 1 not in exported_indices, "Bad episode 1 should not be in export"
    # Verify data file was copied
    assert (export_dir / "data" / "chunk-000" / "file-000.parquet").exists(), "Data parquet not exported"
    # Verify exporting to existing path raises ValueError
    try:
        export_dataset(str(export_dir), exclude_grades=["bad"])
        assert False, "Should have raised ValueError for existing output path"
    except ValueError:
        pass
    print("PASS: export_dataset() filters Bad episodes and creates valid output")
    shutil.rmtree(export_dir.parent, ignore_errors=True)

    print()
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(run_tests())
