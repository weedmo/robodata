import asyncio
import json
import shutil
import threading
import time
import types
from pathlib import Path
from unittest.mock import ANY

import pytest
import pyarrow as pa
import pyarrow.parquet as pq

import backend.converter.validation_service as validation_service
from backend.datasets.services.task_parquet import get_task_text_column_name
from backend.converter.validation_service import (
    ValidationAlreadyRunningError,
    ensure_not_running,
    mark_validation_running,
    read_validation_state,
    write_validation_state,
)


MOCK_DATASET_ROOT = Path(__file__).parent / "mock_dataset"
CELL_TASK = "cell001/task_a"
REQUIRED_DATA_COLUMNS = ("episode_index", "frame_index", "index", "task_index", "timestamp")


@pytest.fixture(autouse=True)
def isolated_validation_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_file = tmp_path / "convert_validation_state.json"
    monkeypatch.setattr(
        "backend.converter.validation_service.VALIDATION_STATE_FILE",
        state_file,
    )
    validation_service._validation_locks.clear()
    monkeypatch.setattr(
        validation_service,
        "_validation_locks_mutex",
        threading.Lock(),
    )
    monkeypatch.setattr(
        validation_service,
        "_state_write_lock",
        threading.Lock(),
    )
    return state_file


@pytest.fixture
def validation_dataset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    lerobot_root = tmp_path / "lerobot"
    dataset_dir = lerobot_root / CELL_TASK
    shutil.copytree(MOCK_DATASET_ROOT, dataset_dir)
    _add_required_data_columns(dataset_dir)
    _normalize_info_for_validation(dataset_dir)
    _add_video_file(dataset_dir)
    monkeypatch.setattr(validation_service, "LEROBOT_BASE", lerobot_root)
    return dataset_dir


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _missing_module(name: str) -> ModuleNotFoundError:
    return ModuleNotFoundError(
        f"No module named {name!r}",
        name=name,
    )


def _write_table(path: Path, table: pa.Table) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)


def _add_required_data_columns(dataset_dir: Path) -> None:
    data_path = dataset_dir / "data" / "chunk-000" / "file-000.parquet"
    data_table = pq.read_table(data_path)

    episode_rows: list[dict] = []
    for episode_file in sorted((dataset_dir / "meta" / "episodes").glob("chunk-*/file-*.parquet")):
        episode_rows.extend(pq.read_table(episode_file).to_pylist())

    task_by_index: dict[int, int] = {}
    frame_by_index: dict[int, int] = {}
    for row in episode_rows:
        start = int(row["dataset_from_index"])
        end = int(row["dataset_to_index"])
        task_index = int(row["task_index"])
        for idx in range(start, end):
            task_by_index[idx] = task_index
            frame_by_index[idx] = idx - start

    row_count = data_table.num_rows
    columns = {name: data_table.column(name) for name in data_table.column_names}
    columns["frame_index"] = pa.array(
        [frame_by_index.get(idx, idx) for idx in range(row_count)],
        type=pa.int64(),
    )
    columns["index"] = pa.array(range(row_count), type=pa.int64())
    columns["task_index"] = pa.array(
        [task_by_index.get(idx, 0) for idx in range(row_count)],
        type=pa.int64(),
    )
    ordered_names = list(data_table.column_names) + ["frame_index", "index", "task_index"]
    _write_table(
        data_path,
        pa.table({name: columns[name] for name in ordered_names}),
    )


def _normalize_info_for_validation(dataset_dir: Path) -> None:
    info_path = dataset_dir / "meta" / "info.json"
    info = _read_json(info_path)
    data_rows = pq.read_table(dataset_dir / "data" / "chunk-000" / "file-000.parquet").num_rows
    info["total_frames"] = data_rows
    info["features"]["frame_index"] = {"dtype": "int64", "shape": [1]}
    info["features"]["index"] = {"dtype": "int64", "shape": [1]}
    info["features"]["task_index"] = {"dtype": "int64", "shape": [1]}
    info["features"]["observation.images.cam_top"] = {
        "dtype": "video",
        "shape": [480, 640, 3],
        "names": ["height", "width", "channels"],
        "video_info": {"video.fps": info["fps"]},
    }
    _write_json(info_path, info)


def _add_video_file(dataset_dir: Path, rel_path: str = "videos/observation.images.cam_top/chunk-000/file-000.mp4") -> None:
    video_path = dataset_dir / rel_path
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(b"FAKE_MP4")


def _drop_columns(path: Path, columns_to_drop: set[str]) -> None:
    table = pq.read_table(path)
    _write_table(
        path,
        pa.table(
            {
                name: table.column(name)
                for name in table.column_names
                if name not in columns_to_drop
            }
        ),
    )


def _replace_with_zero_rows(path: Path) -> None:
    table = pq.read_table(path)
    _write_table(path, table.slice(0, 0))


def _add_episode_file(dataset_dir: Path, rows: dict[str, list[int]], chunk_index: int) -> None:
    _write_table(
        dataset_dir / "meta" / "episodes" / f"chunk-{chunk_index:03d}" / "file-000.parquet",
        pa.table({key: pa.array(values, type=pa.int64()) for key, values in rows.items()}),
    )


def _replace_episode_task_index_with_tasks(dataset_dir: Path, episode_path: Path) -> None:
    table = pq.read_table(episode_path)
    tasks_table = pq.read_table(dataset_dir / "meta" / "tasks.parquet")
    task_text_column = get_task_text_column_name(tasks_table)
    assert task_text_column is not None
    task_lookup = dict(
        zip(
            tasks_table.column("task_index").to_pylist(),
            tasks_table.column(task_text_column).to_pylist(),
            strict=True,
        )
    )
    task_names = [
        [str(task_lookup[int(task_index)])]
        for task_index in table.column("task_index").to_pylist()
    ]
    columns = {
        name: table.column(name)
        for name in table.column_names
        if name != "task_index"
    }
    columns["tasks"] = pa.array(task_names, type=pa.list_(pa.string()))
    ordered_names = list(columns)
    _write_table(episode_path, pa.table({name: columns[name] for name in ordered_names}))


def _set_episode_multi_tasks(dataset_dir: Path, episode_path: Path) -> None:
    table = pq.read_table(episode_path)
    tasks_table = pq.read_table(dataset_dir / "meta" / "tasks.parquet")
    task_text_column = get_task_text_column_name(tasks_table)
    assert task_text_column is not None
    all_task_names = [str(name) for name in tasks_table.column(task_text_column).to_pylist()]
    assert len(all_task_names) >= 2

    row_count = table.num_rows
    multi_tasks = [list(all_task_names) for _ in range(row_count)]
    columns = {
        name: table.column(name)
        for name in table.column_names
        if name != "task_index"
    }
    columns["tasks"] = pa.array(multi_tasks, type=pa.list_(pa.string()))
    _write_table(episode_path, pa.table({name: values for name, values in columns.items()}))


def _add_data_file(dataset_dir: Path, chunk_index: int, row_count: int, missing_columns: set[str] | None = None) -> None:
    missing = missing_columns or set()
    full_columns = {
        "episode_index": pa.array(range(row_count), type=pa.int64()),
        "frame_index": pa.array(range(row_count), type=pa.int64()),
        "index": pa.array(range(row_count), type=pa.int64()),
        "task_index": pa.array([0] * row_count, type=pa.int64()),
        "timestamp": pa.array([float(idx) for idx in range(row_count)], type=pa.float32()),
    }
    _write_table(
        dataset_dir / "data" / f"chunk-{chunk_index:03d}" / "file-000.parquet",
        pa.table({name: values for name, values in full_columns.items() if name not in missing}),
    )


def _replace_column_values(path: Path, column_name: str, values) -> None:
    table = pq.read_table(path)
    columns = {
        name: (pa.array(values, type=table.schema.field(name).type) if name == column_name else table.column(name))
        for name in table.column_names
    }
    _write_table(path, pa.table(columns))


class TestValidationStatePersistence:
    def test_read_validation_state_returns_empty_when_file_missing(self):
        assert read_validation_state() == {}

    def test_write_validation_state_round_trip(self):
        state = {
            "cell001/task_a": {
                "quick": {
                    "status": "passed",
                    "summary": "all good",
                    "checked_at": "2026-04-18T00:00:00+00:00",
                },
            },
        }

        write_validation_state(state)

        assert read_validation_state() == state

    @pytest.mark.asyncio
    async def test_mark_validation_running_persists_running_status_and_summary(self):
        result = await mark_validation_running("cell001/task_a", "quick")

        assert result is None

        assert read_validation_state() == {
            "cell001/task_a": {
                "quick": {
                    "status": "running",
                    "summary": "Quick check running",
                    "checked_at": ANY,
                },
            },
        }

    @pytest.mark.asyncio
    async def test_ensure_not_running_raises_when_same_lock_is_held(self):
        await mark_validation_running("cell001/task_a", "full")

        with pytest.raises(ValidationAlreadyRunningError):
            ensure_not_running("cell001/task_a", "full")

    @pytest.mark.asyncio
    async def test_concurrent_writes_preserve_both_task_entries(self, monkeypatch: pytest.MonkeyPatch):
        original_write = validation_service.write_validation_state

        def slow_write(state: dict) -> None:
            time.sleep(0.02)
            original_write(state)

        monkeypatch.setattr(validation_service, "write_validation_state", slow_write)

        await asyncio.gather(
            mark_validation_running("cell001/task_a", "quick"),
            mark_validation_running("cell002/task_b", "full"),
        )

        assert read_validation_state() == {
            "cell001/task_a": {
                "quick": {
                    "status": "running",
                    "summary": "Quick check running",
                    "checked_at": ANY,
                },
            },
            "cell002/task_b": {
                "full": {
                    "status": "running",
                    "summary": "Full check running",
                    "checked_at": ANY,
                },
            },
        }

    @pytest.mark.asyncio
    async def test_same_key_contention_allows_one_start_and_rejects_second(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        original_persist = validation_service._persist_running_result

        def slow_persist(cell_task: str, mode: str, result) -> None:
            time.sleep(0.02)
            original_persist(cell_task, mode, result)

        monkeypatch.setattr(validation_service, "_persist_running_result", slow_persist)

        first_task = asyncio.create_task(mark_validation_running("cell001/task_a", "quick"))
        await asyncio.sleep(0)
        second_task = asyncio.create_task(mark_validation_running("cell001/task_a", "quick"))

        first_result, second_result = await asyncio.gather(
            first_task,
            second_task,
            return_exceptions=True,
        )

        assert first_result is None
        assert isinstance(second_result, ValidationAlreadyRunningError)

    @pytest.mark.asyncio
    async def test_persistence_failure_releases_lock(self, monkeypatch: pytest.MonkeyPatch):
        def failing_persist(cell_task: str, mode: str, result) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(validation_service, "_persist_running_result", failing_persist)

        with pytest.raises(OSError, match="disk full"):
            await mark_validation_running("cell001/task_a", "quick")

        assert validation_service._lock_for("cell001/task_a", "quick").locked() is False
        ensure_not_running("cell001/task_a", "quick")

    def test_lock_for_returns_single_lock_under_threaded_same_key_first_access(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        original_lock_factory = validation_service.asyncio.Lock
        creation_barrier = threading.Barrier(2)
        start_barrier = threading.Barrier(3)
        created_locks: list[asyncio.Lock] = []
        returned_locks: list[asyncio.Lock] = []

        def slow_lock_factory() -> asyncio.Lock:
            try:
                creation_barrier.wait(timeout=0.1)
            except threading.BrokenBarrierError:
                pass
            lock = original_lock_factory()
            created_locks.append(lock)
            return lock

        def worker() -> None:
            start_barrier.wait(timeout=1)
            returned_locks.append(validation_service._lock_for("cell001/task_a", "quick"))

        monkeypatch.setattr(validation_service.asyncio, "Lock", slow_lock_factory)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        start_barrier.wait(timeout=1)
        for thread in threads:
            thread.join(timeout=1)

        assert len(created_locks) == 1
        assert len(returned_locks) == 2
        assert returned_locks[0] is returned_locks[1]
        assert returned_locks[0] is created_locks[0]


class TestQuickAndFullValidation:
    def test_run_quick_validation_sync_fails_when_dataset_dir_missing(self):
        result = validation_service.run_quick_validation_sync(CELL_TASK)

        assert result["status"] == "failed"
        assert "missing dataset directory" in result["summary"]
        assert read_validation_state()[CELL_TASK]["quick"] == result

    def test_run_quick_validation_sync_passes_for_mock_dataset(self, validation_dataset: Path):
        result = validation_service.run_quick_validation_sync(CELL_TASK)

        assert result == {
            "status": "passed",
            "summary": "Quick passed: 5 episodes, 0 warnings",
            "checked_at": ANY,
        }
        assert read_validation_state()[CELL_TASK]["quick"] == result

    def test_run_quick_validation_for_path_sync_does_not_persist_state(
        self,
        validation_dataset: Path,
    ):
        result = validation_service.run_quick_validation_for_path_sync(
            validation_dataset,
        )

        assert result == {
            "status": "passed",
            "summary": "Quick passed: 5 episodes, 0 warnings",
            "checked_at": ANY,
        }
        assert read_validation_state() == {}

    def test_quick_validation_reads_data_schema_without_loading_data_table(
        self,
        validation_dataset: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        original = validation_service._read_parquet_table

        def reject_data_table_load(path: Path):
            if "data" in path.parts:
                pytest.fail("quick validation must not load full data parquet")
            return original(path)

        monkeypatch.setattr(
            validation_service,
            "_read_parquet_table",
            reject_data_table_load,
        )

        result = validation_service.run_quick_validation_for_path_sync(
            validation_dataset,
        )

        assert result["status"] == "passed"

    def test_run_full_validation_sync_accepts_official_tasks_schema(
        self,
        validation_dataset: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(
            validation_service,
            "_run_official_loader_smoke_test",
            lambda dataset_dir: ("passed", "Full passed: official loader OK"),
            raising=False,
        )
        pq.write_table(
            pa.table(
                {
                    "task_index": [0, 1],
                    "__index_level_0__": [
                        "Pick up the blue cube",
                        "Place the cube on the plate",
                    ],
                }
            ),
            validation_dataset / "meta" / "tasks.parquet",
        )

        result = validation_service.run_full_validation_sync(CELL_TASK)

        assert result == {
            "status": "passed",
            "summary": "Full passed: official loader OK",
            "checked_at": ANY,
        }
        assert read_validation_state()[CELL_TASK]["full"] == result

    def test_run_full_validation_for_path_sync_does_not_persist_state(
        self,
        validation_dataset: Path,
        monkeypatch: pytest.MonkeyPatch,
        isolated_validation_state: Path,
    ):
        monkeypatch.setattr(
            validation_service,
            "_run_official_loader_smoke_test",
            lambda dataset_dir: ("passed", "Full passed: official loader OK"),
        )
        monkeypatch.setattr(
            validation_service,
            "_persist_result",
            lambda *args, **kwargs: pytest.fail("read-only validation must not persist"),
        )

        result = validation_service.run_full_validation_for_path_sync(
            validation_dataset,
        )

        assert result == {
            "status": "passed",
            "summary": "Full passed: official loader OK",
            "checked_at": ANY,
        }
        assert not isolated_validation_state.exists()

    def test_run_full_validation_sync_accepts_named_pandas_index_tasks_schema(
        self,
        validation_dataset: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(
            validation_service,
            "_run_official_loader_smoke_test",
            lambda dataset_dir: ("passed", "Full passed: official loader OK"),
            raising=False,
        )
        pq.write_table(
            pa.table(
                {
                    "task_index": [0, 1],
                    "task_name": [
                        "Pick up the blue cube",
                        "Place the cube on the plate",
                    ],
                }
            ).replace_schema_metadata(
                {
                    b"pandas": (
                        b'{"index_columns":["task_name"],"column_indexes":[{"name":null,'
                        b'"field_name":null,"pandas_type":"unicode","numpy_type":"str",'
                        b'"metadata":{"encoding":"UTF-8"}}],"columns":[{"name":"task_index",'
                        b'"field_name":"task_index","pandas_type":"int64","numpy_type":"int64",'
                        b'"metadata":null},{"name":"task_name","field_name":"task_name",'
                        b'"pandas_type":"unicode","numpy_type":"str","metadata":null}],'
                        b'"creator":{"library":"pyarrow","version":"0.0.0"},"pandas_version":"0.0.0"}'
                    ),
                }
            ),
            validation_dataset / "meta" / "tasks.parquet",
        )

        result = validation_service.run_full_validation_sync(CELL_TASK)

        assert result == {
            "status": "passed",
            "summary": "Full passed: official loader OK",
            "checked_at": ANY,
        }
        assert read_validation_state()[CELL_TASK]["full"] == result

    def test_run_quick_validation_sync_fails_when_tasks_parquet_missing(
        self,
        validation_dataset: Path,
    ):
        (validation_dataset / "meta" / "tasks.parquet").unlink()

        result = validation_service.run_quick_validation_sync(CELL_TASK)

        assert result["status"] == "failed"
        assert "meta/tasks.parquet" in result["summary"]
        assert result["checked_at"]
        assert read_validation_state()[CELL_TASK]["quick"] == result

    def test_run_quick_validation_sync_fails_when_info_json_missing(self, validation_dataset: Path):
        (validation_dataset / "meta" / "info.json").unlink()

        result = validation_service.run_quick_validation_sync(CELL_TASK)

        assert result["status"] == "failed"
        assert "meta/info.json" in result["summary"]

    def test_run_quick_validation_sync_fails_when_episodes_dir_missing(self, validation_dataset: Path):
        shutil.rmtree(validation_dataset / "meta" / "episodes")

        result = validation_service.run_quick_validation_sync(CELL_TASK)

        assert result["status"] == "failed"
        assert "meta/episodes" in result["summary"]

    def test_run_quick_validation_sync_fails_when_no_episode_parquet_files(self, validation_dataset: Path):
        shutil.rmtree(validation_dataset / "meta" / "episodes")
        (validation_dataset / "meta" / "episodes").mkdir(parents=True)

        result = validation_service.run_quick_validation_sync(CELL_TASK)

        assert result["status"] == "failed"
        assert "no episode parquet files" in result["summary"].lower()

    def test_run_quick_validation_sync_fails_when_data_dir_missing(self, validation_dataset: Path):
        shutil.rmtree(validation_dataset / "data")

        result = validation_service.run_quick_validation_sync(CELL_TASK)

        assert result["status"] == "failed"
        assert "required directory data" in result["summary"].lower()

    def test_run_quick_validation_sync_fails_when_no_data_parquet_files(self, validation_dataset: Path):
        shutil.rmtree(validation_dataset / "data")
        (validation_dataset / "data").mkdir(parents=True)

        result = validation_service.run_quick_validation_sync(CELL_TASK)

        assert result["status"] == "failed"
        assert "no data parquet files" in result["summary"].lower()

    def test_run_quick_validation_sync_fails_when_required_info_json_key_missing(
        self,
        validation_dataset: Path,
    ):
        info_path = validation_dataset / "meta" / "info.json"
        info = _read_json(info_path)
        info.pop("total_frames")
        _write_json(info_path, info)

        result = validation_service.run_quick_validation_sync(CELL_TASK)

        assert result["status"] == "failed"
        assert "total_frames" in result["summary"]

    def test_run_quick_validation_sync_fails_when_total_frames_mismatches_data_rows(
        self,
        validation_dataset: Path,
    ):
        info_path = validation_dataset / "meta" / "info.json"
        info = _read_json(info_path)
        info["total_frames"] = 499
        _write_json(info_path, info)

        result = validation_service.run_quick_validation_sync(CELL_TASK)

        assert result["status"] == "failed"
        assert "total_frames" in result["summary"]

    def test_quick_path_rejects_zero_row_completion_skeleton(
        self,
        validation_dataset: Path,
    ):
        _replace_with_zero_rows(
            validation_dataset
            / "meta"
            / "episodes"
            / "chunk-000"
            / "file-000.parquet"
        )
        _replace_with_zero_rows(
            validation_dataset / "data" / "chunk-000" / "file-000.parquet"
        )
        info_path = validation_dataset / "meta" / "info.json"
        info = _read_json(info_path)
        info["total_episodes"] = 0
        info["total_frames"] = 0
        _write_json(info_path, info)

        result = validation_service.run_quick_validation_for_path_sync(
            validation_dataset,
        )

        assert result["status"] == "failed"
        assert "at least 1 episode" in result["summary"]

    def test_quick_path_rejects_zero_episode_rows(
        self,
        validation_dataset: Path,
    ):
        _replace_with_zero_rows(
            validation_dataset
            / "meta"
            / "episodes"
            / "chunk-000"
            / "file-000.parquet"
        )

        result = validation_service.run_quick_validation_for_path_sync(
            validation_dataset,
        )

        assert result["status"] == "failed"
        assert "episode parquet must contain at least 1 episode" in result["summary"]

    def test_quick_path_rejects_zero_data_rows(
        self,
        validation_dataset: Path,
    ):
        _replace_with_zero_rows(
            validation_dataset / "data" / "chunk-000" / "file-000.parquet"
        )

        result = validation_service.run_quick_validation_for_path_sync(
            validation_dataset,
        )

        assert result["status"] == "failed"
        assert "data parquet must contain at least 1 frame" in result["summary"]

    def test_quick_path_rejects_zero_task_rows(
        self,
        validation_dataset: Path,
    ):
        _replace_with_zero_rows(
            validation_dataset / "meta" / "tasks.parquet"
        )

        result = validation_service.run_quick_validation_for_path_sync(
            validation_dataset,
        )

        assert result["status"] == "failed"
        assert "at least 1 task" in result["summary"]

    @pytest.mark.parametrize(
        ("field", "value", "unit"),
        [
            ("total_episodes", 0, "episode"),
            ("total_episodes", -1, "episode"),
            ("total_frames", 0, "frame"),
            ("total_frames", -1, "frame"),
        ],
    )
    def test_quick_path_rejects_non_positive_info_totals(
        self,
        validation_dataset: Path,
        field: str,
        value: int,
        unit: str,
    ):
        info_path = validation_dataset / "meta" / "info.json"
        info = _read_json(info_path)
        info[field] = value
        _write_json(info_path, info)

        result = validation_service.run_quick_validation_for_path_sync(
            validation_dataset,
        )

        assert result["status"] == "failed"
        assert f"{field} must be at least 1 {unit}" in result["summary"]

    def test_quick_path_rejects_total_episodes_mismatch(
        self,
        validation_dataset: Path,
    ):
        info_path = validation_dataset / "meta" / "info.json"
        info = _read_json(info_path)
        info["total_episodes"] = int(info["total_episodes"]) - 1
        _write_json(info_path, info)

        result = validation_service.run_quick_validation_for_path_sync(
            validation_dataset,
        )

        assert result["status"] == "failed"
        assert "total_episodes" in result["summary"]
        assert "episodes parquet" in result["summary"]

    def test_quick_path_keeps_data_validation_schema_only(
        self,
        validation_dataset: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        data_file = (
            validation_dataset / "data" / "chunk-000" / "file-000.parquet"
        )
        original_read = validation_service._read_parquet_table
        read_paths: list[Path] = []

        def tracked_read(path: Path):
            read_paths.append(path)
            return original_read(path)

        monkeypatch.setattr(
            validation_service,
            "_read_parquet_table",
            tracked_read,
        )

        result = validation_service.run_quick_validation_for_path_sync(
            validation_dataset,
        )

        assert result["status"] == "passed"
        assert data_file not in read_paths

    def test_run_quick_validation_sync_fails_when_required_data_column_missing(
        self,
        validation_dataset: Path,
    ):
        _drop_columns(
            validation_dataset / "data" / "chunk-000" / "file-000.parquet",
            {"frame_index"},
        )

        result = validation_service.run_quick_validation_sync(CELL_TASK)

        assert result["status"] == "failed"
        assert "frame_index" in result["summary"]

    def test_run_quick_validation_sync_fails_when_later_data_parquet_missing_required_column(
        self,
        validation_dataset: Path,
    ):
        _add_data_file(
            validation_dataset,
            chunk_index=1,
            row_count=5,
            missing_columns={"task_index"},
        )

        result = validation_service.run_quick_validation_sync(CELL_TASK)

        assert result["status"] == "failed"
        assert "task_index" in result["summary"]

    def test_run_quick_validation_sync_fails_when_required_episode_column_missing(
        self,
        validation_dataset: Path,
    ):
        _drop_columns(
            validation_dataset / "meta" / "episodes" / "chunk-000" / "file-000.parquet",
            {"task_index"},
        )

        result = validation_service.run_quick_validation_sync(CELL_TASK)

        assert result["status"] == "failed"
        assert "task_index" in result["summary"]

    def test_run_full_validation_sync_accepts_episode_tasks_schema(
        self,
        validation_dataset: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(
            validation_service,
            "_run_official_loader_smoke_test",
            lambda dataset_dir: ("passed", "Full passed: official loader OK"),
            raising=False,
        )
        _replace_episode_task_index_with_tasks(
            validation_dataset,
            validation_dataset / "meta" / "episodes" / "chunk-000" / "file-000.parquet",
        )

        result = validation_service.run_full_validation_sync(CELL_TASK)

        assert result == {
            "status": "passed",
            "summary": "Full passed: official loader OK",
            "checked_at": ANY,
        }
        assert read_validation_state()[CELL_TASK]["full"] == result

    def test_run_full_validation_sync_accepts_multi_task_episode(
        self,
        validation_dataset: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(
            validation_service,
            "_run_official_loader_smoke_test",
            lambda dataset_dir: ("passed", "Full passed: official loader OK"),
            raising=False,
        )
        _set_episode_multi_tasks(
            validation_dataset,
            validation_dataset / "meta" / "episodes" / "chunk-000" / "file-000.parquet",
        )
        _replace_column_values(
            validation_dataset / "data" / "chunk-000" / "file-000.parquet",
            "task_index",
            [index % 2 for index in range(500)],
        )

        result = validation_service.run_full_validation_sync(CELL_TASK)

        assert result == {
            "status": "passed",
            "summary": "Full passed: official loader OK",
            "checked_at": ANY,
        }
        assert read_validation_state()[CELL_TASK]["full"] == result

    def test_run_quick_validation_sync_fails_when_later_episode_parquet_missing_required_column(
        self,
        validation_dataset: Path,
    ):
        _add_episode_file(
            validation_dataset,
            rows={
                "episode_index": [5],
                "data/chunk_index": [1],
                "data/file_index": [0],
                "dataset_from_index": [500],
                "dataset_to_index": [505],
            },
            chunk_index=1,
        )

        result = validation_service.run_quick_validation_sync(CELL_TASK)

        assert result["status"] == "failed"
        assert "task_index" in result["summary"]

    def test_run_quick_validation_sync_fails_on_row_count_consistency_mismatch_across_episode_files(
        self,
        validation_dataset: Path,
    ):
        _add_episode_file(
            validation_dataset,
            rows={
                "episode_index": [5],
                "task_index": [1],
                "data/chunk_index": [1],
                "data/file_index": [0],
                "dataset_from_index": [500],
                "dataset_to_index": [550],
            },
            chunk_index=1,
        )
        info_path = validation_dataset / "meta" / "info.json"
        info = _read_json(info_path)
        info["total_episodes"] = 6
        _write_json(info_path, info)

        result = validation_service.run_quick_validation_sync(CELL_TASK)

        assert result["status"] == "failed"
        assert "data rows" in result["summary"].lower()

    def test_run_quick_validation_sync_requires_mp4_for_video_dataset(
        self,
        validation_dataset: Path,
    ):
        shutil.rmtree(validation_dataset / "videos")

        result = validation_service.run_quick_validation_sync(CELL_TASK)

        assert result["status"] == "failed"
        assert ".mp4" in result["summary"]

    def test_run_full_validation_sync_returns_partial_when_official_loader_skipped(
        self,
        validation_dataset: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(
            validation_service,
            "_run_official_loader_smoke_test",
            lambda dataset_dir: (
                "partial",
                "Full partial: dataset OK, official loader skipped",
            ),
            raising=False,
        )

        result = validation_service.run_full_validation_sync(CELL_TASK)

        assert result == {
            "status": "partial",
            "summary": "Full partial: dataset OK, official loader skipped",
            "checked_at": ANY,
        }
        assert read_validation_state()[CELL_TASK]["full"] == result

    def test_run_full_validation_sync_fails_on_task_index_mismatch(
        self,
        validation_dataset: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(
            validation_service,
            "_run_official_loader_smoke_test",
            lambda dataset_dir: ("passed", "Full passed: official loader OK"),
            raising=False,
        )
        pq.write_table(
            pa.table(
                {
                    "task_index": [10, 11],
                    "task": [
                        "Pick up the blue cube",
                        "Place the cube on the plate",
                    ],
                }
            ),
            validation_dataset / "meta" / "tasks.parquet",
        )

        result = validation_service.run_full_validation_sync(CELL_TASK)

        assert result["status"] == "failed"
        assert "task index" in result["summary"].lower()
        assert result["checked_at"]
        assert read_validation_state()[CELL_TASK]["full"] == result

    def test_run_full_validation_sync_fails_when_data_task_reference_disagrees_with_episode_metadata(
        self,
        validation_dataset: Path,
    ):
        _replace_column_values(
            validation_dataset / "data" / "chunk-000" / "file-000.parquet",
            "task_index",
            [1] * 100 + [0] * 400,
        )

        result = validation_service.run_full_validation_sync(CELL_TASK)

        assert result["status"] == "failed"
        assert "data task reference" in result["summary"].lower()

    def test_run_full_validation_sync_fails_when_data_episode_reference_disagrees_with_episode_metadata(
        self,
        validation_dataset: Path,
    ):
        _replace_column_values(
            validation_dataset / "data" / "chunk-000" / "file-000.parquet",
            "episode_index",
            [4] * 100 + list(range(100, 500)),
        )

        result = validation_service.run_full_validation_sync(CELL_TASK)

        assert result["status"] == "failed"
        assert "data episode reference" in result["summary"].lower()

    def test_run_full_validation_sync_fails_when_video_file_is_not_accessible(
        self,
        validation_dataset: Path,
    ):
        video_path = validation_dataset / "videos" / "observation.images.cam_top" / "chunk-000" / "file-000.mp4"
        video_path.write_bytes(b"")

        result = validation_service.run_full_validation_sync(CELL_TASK)

        assert result["status"] == "failed"
        assert "video" in result["summary"].lower()


class TestOfficialLoaderSmokeTest:
    def test_missing_loader_import_returns_partial_skip(self, validation_dataset: Path, monkeypatch: pytest.MonkeyPatch):
        def missing_loader(name: str):
            raise _missing_module(name)

        monkeypatch.setattr(validation_service.importlib, "import_module", missing_loader)

        status, summary = validation_service._run_official_loader_smoke_test(validation_dataset)

        assert status == "partial"
        assert summary == "Full partial: dataset OK, official loader skipped"

    def test_root_import_missing_internal_dependency_is_failed(
        self,
        validation_dataset: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        def missing_internal_dependency(name: str):
            assert name == "lerobot"
            raise _missing_module("lerobot_native_dependency")

        monkeypatch.setattr(
            validation_service.importlib,
            "import_module",
            missing_internal_dependency,
        )

        status, summary = validation_service._run_official_loader_smoke_test(
            validation_dataset,
        )

        assert status == "failed"
        assert "lerobot_native_dependency" in summary

    def test_loader_import_missing_internal_dependency_is_failed(
        self,
        validation_dataset: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        fake_root = types.SimpleNamespace(__version__="0.5.1")

        def missing_internal_dependency(name: str):
            if name == "lerobot":
                return fake_root
            if name == "lerobot.datasets.lerobot_dataset":
                raise _missing_module("loader_native_dependency")
            pytest.fail("legacy fallback must not hide an internal dependency failure")

        monkeypatch.setattr(
            validation_service.importlib,
            "import_module",
            missing_internal_dependency,
        )

        status, summary = validation_service._run_official_loader_smoke_test(
            validation_dataset,
        )

        assert status == "failed"
        assert "loader_native_dependency" in summary

    def test_metadata_import_missing_internal_dependency_is_failed(
        self,
        validation_dataset: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        fake_root = types.SimpleNamespace(__version__="0.5.1")

        class FakeMetadata:
            pass

        FakeMetadata.__module__ = "lerobot.datasets.dataset_metadata"

        class FakeDataset:
            def __init__(self, **_kwargs):
                pass

        fake_loader_module = types.SimpleNamespace(
            LeRobotDataset=FakeDataset,
            LeRobotDatasetMetadata=FakeMetadata,
        )

        def missing_metadata_dependency(name: str):
            if name == "lerobot":
                return fake_root
            if name == "lerobot.datasets.lerobot_dataset":
                return fake_loader_module
            if name == "lerobot.datasets.dataset_metadata":
                raise _missing_module("metadata_native_dependency")
            raise _missing_module(name)

        monkeypatch.setattr(
            validation_service.importlib,
            "import_module",
            missing_metadata_dependency,
        )

        status, summary = validation_service._run_official_loader_smoke_test(
            validation_dataset,
        )

        assert status == "failed"
        assert "metadata_native_dependency" in summary

    def test_actual_lerobot_051_loader_and_metadata_classes_are_private(
        self,
    ):
        dataset_module = validation_service.importlib.import_module(
            "lerobot.datasets.lerobot_dataset"
        )
        metadata_module = validation_service.importlib.import_module(
            "lerobot.datasets.dataset_metadata"
        )
        assert (
            validation_service.importlib.import_module("lerobot").__version__
            == "0.5.1"
        )
        shared_dataset_download = dataset_module.LeRobotDataset._download
        shared_metadata_pull = (
            metadata_module.LeRobotDatasetMetadata._pull_from_repo
        )
        shared_dataset_safe_version = dataset_module.get_safe_version
        shared_metadata_safe_version = metadata_module.get_safe_version

        isolated_dataset, isolated_metadata = (
            validation_service._isolate_current_loader_modules(dataset_module)
        )
        validation_service._install_local_only_loader_guards(
            isolated_dataset,
            isolated_metadata,
        )

        assert isolated_dataset is not dataset_module
        assert isolated_metadata is not metadata_module
        assert (
            isolated_dataset.LeRobotDataset
            is not dataset_module.LeRobotDataset
        )
        assert (
            isolated_metadata.LeRobotDatasetMetadata
            is not metadata_module.LeRobotDatasetMetadata
        )
        assert (
            isolated_dataset.LeRobotDatasetMetadata
            is isolated_metadata.LeRobotDatasetMetadata
        )
        assert (
            isolated_dataset.LeRobotDataset.__init__.__globals__[
                "LeRobotDatasetMetadata"
            ]
            is isolated_metadata.LeRobotDatasetMetadata
        )
        assert dataset_module.LeRobotDataset._download is shared_dataset_download
        assert (
            metadata_module.LeRobotDatasetMetadata._pull_from_repo
            is shared_metadata_pull
        )
        assert dataset_module.get_safe_version is shared_dataset_safe_version
        assert metadata_module.get_safe_version is shared_metadata_safe_version
        assert (
            isolated_dataset.LeRobotDataset._download
            is not shared_dataset_download
        )
        assert (
            isolated_metadata.LeRobotDatasetMetadata._pull_from_repo
            is not shared_metadata_pull
        )

    def test_broken_loader_import_is_reported_as_failed(
        self,
        validation_dataset: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        def broken_loader(name: str):
            if name == "lerobot":
                return types.SimpleNamespace(__version__="0.5.1")
            raise RuntimeError("broken native dependency")

        monkeypatch.setattr(
            validation_service.importlib,
            "import_module",
            broken_loader,
        )

        status, summary = validation_service._run_official_loader_smoke_test(
            validation_dataset,
        )

        assert status == "failed"
        assert "broken native dependency" in summary

    def test_old_lerobot_version_returns_partial_skip(
        self,
        validation_dataset: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        fake_root = types.SimpleNamespace(__version__="0.0.1")
        monkeypatch.setattr(
            validation_service.importlib,
            "import_module",
            lambda name: fake_root if name == "lerobot" else pytest.fail("loader module should not be imported"),
        )

        status, summary = validation_service._run_official_loader_smoke_test(validation_dataset)

        assert status == "partial"
        assert summary == "Full partial: dataset OK, official loader skipped"

    def test_old_loader_api_without_from_local_returns_partial_skip(
        self,
        validation_dataset: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        fake_root = types.SimpleNamespace(__version__="999.0.0")
        fake_loader_module = types.SimpleNamespace(LeRobotDataset=type("FakeDataset", (), {}))

        def fake_import(name: str):
            if name == "lerobot":
                return fake_root
            if name == "lerobot.common.datasets.lerobot_dataset":
                return fake_loader_module
            raise _missing_module(name)

        monkeypatch.setattr(validation_service.importlib, "import_module", fake_import)

        status, summary = validation_service._run_official_loader_smoke_test(validation_dataset)

        assert status == "partial"
        assert summary == "Full partial: dataset OK, official loader skipped"

    def test_legacy_loader_from_local_passes(
        self,
        validation_dataset: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        loaded_paths: list[str] = []
        fake_root = types.SimpleNamespace(__version__="0.3.0")
        fake_loader_module = types.SimpleNamespace(
            LeRobotDataset=types.SimpleNamespace(
                from_local=lambda path: loaded_paths.append(path),
            )
        )

        def fake_import(name: str):
            if name == "lerobot":
                return fake_root
            if name == "lerobot.datasets.lerobot_dataset":
                raise _missing_module(name)
            if name == "lerobot.common.datasets.lerobot_dataset":
                return fake_loader_module
            raise _missing_module(name)

        monkeypatch.setattr(validation_service.importlib, "import_module", fake_import)

        status, summary = validation_service._run_official_loader_smoke_test(
            validation_dataset,
        )

        assert status == "passed"
        assert summary == "Full passed: dataset OK, official loader smoke test passed"
        assert loaded_paths == [str(validation_dataset)]

    def test_current_loader_uses_existing_local_root_without_videos_download(
        self,
        validation_dataset: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        constructor_calls: list[dict] = []
        fake_root = types.SimpleNamespace(__version__="0.5.1")

        class FakeDataset:
            def __init__(self, **kwargs):
                constructor_calls.append(kwargs)

        fake_loader_module = types.SimpleNamespace(LeRobotDataset=FakeDataset)

        def fake_import(name: str):
            if name == "lerobot":
                return fake_root
            if name == "lerobot.datasets.lerobot_dataset":
                return fake_loader_module
            raise _missing_module(name)

        monkeypatch.setattr(validation_service.importlib, "import_module", fake_import)

        status, summary = validation_service._run_official_loader_smoke_test(
            validation_dataset,
        )

        assert status == "passed"
        assert summary == "Full passed: dataset OK, official loader smoke test passed"
        assert constructor_calls == [
            {
                "repo_id": f"local/{validation_dataset.name}",
                "root": validation_dataset,
                "download_videos": False,
            }
        ]

    def test_current_loader_failure_is_not_reported_as_unavailable(
        self,
        validation_dataset: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        fake_root = types.SimpleNamespace(__version__="0.5.1")

        class FailingDataset:
            def __init__(self, **kwargs):
                raise ValueError("dataset is invalid")

        fake_loader_module = types.SimpleNamespace(LeRobotDataset=FailingDataset)

        def fake_import(name: str):
            if name == "lerobot":
                return fake_root
            if name == "lerobot.datasets.lerobot_dataset":
                return fake_loader_module
            raise _missing_module(name)

        monkeypatch.setattr(validation_service.importlib, "import_module", fake_import)

        status, summary = validation_service._run_official_loader_smoke_test(
            validation_dataset,
        )

        assert status == "failed"
        assert "dataset is invalid" in summary

    def test_current_loader_never_calls_hub_download_in_local_only_smoke(
        self,
        validation_dataset: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        calls = {
            "safe_version": 0,
            "metadata_pull": 0,
            "dataset_download": 0,
        }
        fake_root = types.SimpleNamespace(__version__="0.5.1")

        def get_safe_version(*_args, **_kwargs):
            calls["safe_version"] += 1

        class FakeMetadata:
            def _pull_from_repo(self):
                calls["metadata_pull"] += 1

        class FakeDataset:
            def __init__(self, **kwargs):
                fake_loader_module.get_safe_version("local/test", "v3.0")
                FakeMetadata()._pull_from_repo()

            def _download(self):
                calls["dataset_download"] += 1

        fake_loader_module = types.SimpleNamespace(
            LeRobotDataset=FakeDataset,
            LeRobotDatasetMetadata=FakeMetadata,
            get_safe_version=get_safe_version,
        )

        def fake_import(name: str):
            if name == "lerobot":
                return fake_root
            if name == "lerobot.datasets.lerobot_dataset":
                return fake_loader_module
            if name == FakeMetadata.__module__:
                return fake_loader_module
            raise _missing_module(name)

        monkeypatch.setattr(validation_service.importlib, "import_module", fake_import)

        status, summary = validation_service._run_official_loader_smoke_test(
            validation_dataset,
        )

        assert status == "failed"
        assert "offline local-only validation refused remote access" in summary
        assert calls == {
            "safe_version": 0,
            "metadata_pull": 0,
            "dataset_download": 0,
        }
