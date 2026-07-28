import pytest
import pyarrow as pa
import pyarrow.parquet as pq
import threading
from pathlib import Path
from types import SimpleNamespace
from backend.jobs import repo as jobs_repo
from backend.core.db import db, init_db
from backend.converter.queue_adapter import (
    PendingConversionRecoveryError,
    _pending_recordings,
    _require_recovered_output,
    _run_conversion,
    process_one_queued,
)

pytestmark = pytest.mark.usefixtures("_point_settings_at_test_db")


class _FakeStateReconciliation:
    def reconcile_persisted_serials(self, cell_task, persisted):
        return 0

    def flush(self):
        return None


@pytest.fixture(autouse=True)
async def clean():
    await init_db()
    await db.execute("DELETE FROM worker_heartbeats")
    await db.execute("DELETE FROM jobs")
    await db.execute("UPDATE worker_controls SET desired_state='running', note=NULL")
    yield
    await db.execute("DELETE FROM worker_heartbeats")
    await db.execute("DELETE FROM jobs")


def _recovery_guard_fake(tmp_path):
    return SimpleNamespace(LEROBOT_BASE=_lerobot_root(tmp_path))


def _lerobot_root(tmp_path):
    root = tmp_path / "lerobot"
    root.mkdir(exist_ok=True)
    return root


def _write_complete_output_skeleton(path):
    episodes = path / "meta" / "episodes" / "chunk-000"
    data = path / "data" / "chunk-000"
    episodes.mkdir(parents=True)
    data.mkdir(parents=True)
    (path / "meta" / "info.json").write_text(
        (
            '{"codebase_version":"v3.0","features":{},"fps":30,'
            '"total_episodes":1,"total_frames":1}'
        ),
        encoding="utf-8",
    )
    pq.write_table(
        pa.table({"task_index": [0], "task": ["task"]}),
        path / "meta" / "tasks.parquet",
    )
    pq.write_table(
        pa.table(
            {
                "episode_index": [0],
                "task_index": [0],
                "data/chunk_index": [0],
                "data/file_index": [0],
                "dataset_from_index": [0],
                "dataset_to_index": [1],
            }
        ),
        episodes / "file-000.parquet",
    )
    pq.write_table(
        pa.table(
            {
                "episode_index": [0],
                "frame_index": [0],
                "index": [0],
                "task_index": [0],
                "timestamp": [0.0],
            }
        ),
        data / "file-000.parquet",
    )


@pytest.mark.parametrize(
    ("marker_name", "phase"),
    [
        (".task.finalization-pending.json", "armed"),
        (".task.rebuild-journal.json", "prepared"),
    ],
)
def test_recovery_guard_blocks_unfinished_transaction(
    tmp_path, marker_name, phase
):
    fake = _recovery_guard_fake(tmp_path)
    marker = fake.LEROBOT_BASE / "cell" / marker_name
    marker.parent.mkdir(parents=True)
    marker.write_text(f'{{"phase":"{phase}"}}', encoding="utf-8")

    with pytest.raises(PendingConversionRecoveryError, match="conversion blocked"):
        _require_recovered_output(fake, "cell/task")


def test_recovery_guard_blocks_unproven_verified_rebuild_journal(tmp_path):
    fake = _recovery_guard_fake(tmp_path)
    marker = fake.LEROBOT_BASE / "cell" / ".task.rebuild-journal.json"
    marker.parent.mkdir(parents=True)
    marker.write_text('{"phase":"verified"}', encoding="utf-8")

    with pytest.raises(PendingConversionRecoveryError, match="conversion blocked"):
        _require_recovered_output(fake, "cell/task")


@pytest.mark.parametrize("phase", ["committed", "rolled_back"])
def test_recovery_guard_blocks_bare_terminal_rebuild_journal(tmp_path, phase):
    fake = _recovery_guard_fake(tmp_path)
    marker = fake.LEROBOT_BASE / "cell" / ".task.rebuild-journal.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(f'{{"phase":"{phase}"}}', encoding="utf-8")

    with pytest.raises(PendingConversionRecoveryError, match="conversion blocked"):
        _require_recovered_output(fake, "cell/task")


def test_recovery_guard_rejects_symlink_marker(tmp_path):
    fake = _recovery_guard_fake(tmp_path)
    marker = fake.LEROBOT_BASE / "cell" / ".task.rebuild-journal.json"
    marker.parent.mkdir(parents=True)
    target = tmp_path / "outside.json"
    target.write_text('{"phase":"verified"}', encoding="utf-8")
    marker.symlink_to(target)

    with pytest.raises(
        PendingConversionRecoveryError,
        match="conversion blocked",
    ):
        _require_recovered_output(fake, "cell/task")


def test_recovery_guard_blocks_active_durable_intent(tmp_path):
    fake = _recovery_guard_fake(tmp_path)
    marker = fake.LEROBOT_BASE / "cell" / ".task.recovery-intent.json"
    marker.parent.mkdir(parents=True)
    marker.write_text('{"phase":"state_backup_pending"}', encoding="utf-8")

    with pytest.raises(PendingConversionRecoveryError, match="conversion blocked"):
        _require_recovered_output(fake, "cell/task")


@pytest.mark.parametrize(
    "missing",
    [
        "meta/info.json",
        "meta/tasks.parquet",
        "meta/episodes",
        "data",
    ],
)
def test_recovery_guard_blocks_markerless_incomplete_output(tmp_path, missing):
    fake = _recovery_guard_fake(tmp_path)
    output = fake.LEROBOT_BASE / "cell" / "task"
    _write_complete_output_skeleton(output)
    target = output / missing
    if target.is_dir():
        for child in sorted(target.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink()
            else:
                child.rmdir()
        target.rmdir()
    else:
        target.unlink()

    with pytest.raises(
        PendingConversionRecoveryError,
        match="incomplete-output",
    ):
        _require_recovered_output(fake, "cell/task")


def test_recovery_guard_allows_markerless_complete_output_skeleton(tmp_path):
    fake = _recovery_guard_fake(tmp_path)
    _write_complete_output_skeleton(
        fake.LEROBOT_BASE / "cell" / "task",
    )

    _require_recovered_output(fake, "cell/task")


def test_recovery_guard_blocks_markerless_corrupt_output(tmp_path):
    fake = _recovery_guard_fake(tmp_path)
    output = fake.LEROBOT_BASE / "cell" / "task"
    _write_complete_output_skeleton(output)
    (output / "meta" / "info.json").write_text("{broken", encoding="utf-8")

    with pytest.raises(
        PendingConversionRecoveryError,
        match="incomplete-output",
    ):
        _require_recovered_output(fake, "cell/task")


def test_pending_recordings_checks_recovery_before_loading_state_or_scanning(
    tmp_path,
):
    marker = tmp_path / "lerobot" / "cell" / ".task.finalization-pending.json"
    marker.parent.mkdir(parents=True)
    marker.write_text('{"phase":"armed"}', encoding="utf-8")

    class MustNotConstruct:
        def __init__(self, _path):
            raise AssertionError("state/scanner must not run before recovery guard")

    fake = SimpleNamespace(
        RAW_BASE=tmp_path / "raw",
        LEROBOT_BASE=_lerobot_root(tmp_path),
        STATE_FILE=tmp_path / "state.json",
        NASScanner=MustNotConstruct,
        ConvertState=MustNotConstruct,
    )

    with pytest.raises(PendingConversionRecoveryError):
        _pending_recordings(fake, "cell/task")


def test_pending_recordings_ignores_retry_serial_moved_to_another_metadata_task(
    tmp_path,
):
    class FakeScanner:
        def __init__(self, raw_base):
            self.raw_base = raw_base

        def scan(self):
            return {"cell/task": ["current"]}

        def find_pending_recordings(self, serials, converted, failed, transient):
            return list(serials)

    class FakeState(_FakeStateReconciliation):
        def __init__(self, state_file):
            self.state_file = state_file

        def load(self):
            return None

        def get_failed_serials(self, cell_task):
            return set()

        def get_transient_failed(self, cell_task):
            return {"moved": {"next_retry_at": 0}}

        def get_retry_eligible(self, cell_task):
            return ["moved"]

    fake = SimpleNamespace(
        RAW_BASE=tmp_path / "raw",
        LEROBOT_BASE=_lerobot_root(tmp_path),
        STATE_FILE=tmp_path / "state.json",
        NASScanner=FakeScanner,
        ConvertState=FakeState,
        _load_converted_serials=lambda output_root: set(),
    )

    recordings, _state = _pending_recordings(fake, "cell/task")

    assert recordings == ["current"]


def test_pending_recordings_reconciles_stale_retry_with_durable_output(
    tmp_path,
):
    class FakeScanner:
        def __init__(self, raw_base):
            self.raw_base = raw_base

        def scan(self):
            return {"cell/task": ["persisted", "pending"]}

        def find_pending_recordings(self, serials, converted, failed, transient):
            return []

    class FakeState(_FakeStateReconciliation):
        def __init__(self, state_file):
            self.state_file = state_file
            self.transient = {"persisted", "pending"}
            self.flushed = False

        def load(self):
            return None

        def reconcile_persisted_serials(self, cell_task, persisted):
            removed = self.transient.intersection(persisted)
            self.transient.difference_update(removed)
            return len(removed)

        def flush(self):
            self.flushed = True

        def get_failed_serials(self, cell_task):
            return set()

        def get_transient_failed(self, cell_task):
            return {serial: {} for serial in self.transient}

        def get_retry_eligible(self, cell_task):
            return sorted(self.transient)

    fake = SimpleNamespace(
        RAW_BASE=tmp_path / "raw",
        LEROBOT_BASE=_lerobot_root(tmp_path),
        STATE_FILE=tmp_path / "state.json",
        NASScanner=FakeScanner,
        ConvertState=FakeState,
        _load_converted_serials=lambda output_root: {"persisted"},
    )

    recordings, state = _pending_recordings(fake, "cell/task")

    assert recordings == ["pending"]
    assert state.flushed is True


@pytest.mark.asyncio
async def test_picks_up_queued_convert_and_completes(monkeypatch):
    convert_calls = []

    async def fake_convert(payload, *, job_id=None, check_cancel=None):
        convert_calls.append(dict(payload))

    monkeypatch.setattr(
        "backend.converter.queue_adapter._run_conversion", fake_convert,
    )
    enq = await jobs_repo.enqueue(type_="convert", payload={"cell": "a/b"})
    await process_one_queued(idle_sleep=0)
    job = await jobs_repo.fetch(enq["id"])
    assert job["status"] == "complete"
    assert convert_calls == [{"cell": "a/b"}]


@pytest.mark.asyncio
async def test_handles_cancel_mid_chunk(monkeypatch):
    async def with_cancel(payload, *, job_id=None, check_cancel=None):
        await db.execute(
            "UPDATE jobs SET status='cancel_requested' WHERE status='running'"
        )
        if await check_cancel():
            from backend.workers.runtime import CancelledNormally
            return CancelledNormally(cleanup="partial output removed")

    monkeypatch.setattr(
        "backend.converter.queue_adapter._run_conversion", with_cancel,
    )
    enq = await jobs_repo.enqueue(type_="convert", payload={})
    await process_one_queued(idle_sleep=0)
    job = await jobs_repo.fetch(enq["id"])
    assert job["status"] == "cancelled"
    assert "partial output removed" in (job["error"] or "")


@pytest.mark.asyncio
async def test_default_run_conversion_calls_auto_converter_for_cell_task(monkeypatch, tmp_path):
    convert_calls = []

    class FakeScanner:
        def __init__(self, raw_base):
            self.raw_base = raw_base

        def scan(self):
            return {"cell/task": ["s1", "s2"]}

        def find_pending_recordings(self, serials, converted, failed, transient):
            assert serials == ["s1", "s2"]
            assert converted == set()
            assert failed == set()
            assert transient == set()
            return ["s1", "s2"]

    class FakeState(_FakeStateReconciliation):
        def __init__(self, state_file):
            self.state_file = state_file
            self.count = 0

        def load(self):
            return None

        def get_failed_serials(self, cell_task):
            return set()

        def get_transient_failed(self, cell_task):
            return {}

        def get_retry_eligible(self, cell_task):
            return []

        def get_converted_count(self, cell_task):
            return self.count

    fake = SimpleNamespace(
        RAW_BASE=tmp_path / "raw",
        LEROBOT_BASE=_lerobot_root(tmp_path),
        STATE_FILE=tmp_path / "state.json",
        NASScanner=FakeScanner,
        ConvertState=FakeState,
        shutdown_event=threading.Event(),
        _check_stop_requested=lambda: False,
        _has_other_task_request=lambda cell_task: False,
        _clear_stop_flag=lambda: None,
        _load_converted_serials=lambda output_root: set(),
    )

    def fake_convert_task(cell, task, recordings, state):
        state.count += len(recordings)
        convert_calls.append((cell, task, recordings, state.state_file))
        return True

    fake.convert_task = fake_convert_task
    monkeypatch.setattr(
        "backend.converter.queue_adapter._load_auto_converter_module",
        lambda: fake,
    )

    await _run_conversion({"cell_task": "cell/task"})

    assert convert_calls == [("cell", "task", ["s1", "s2"], tmp_path / "state.json")]


@pytest.mark.asyncio
async def test_run_conversion_propagates_target_fps(monkeypatch, tmp_path):
    convert_calls = []

    class FakeScanner:
        def __init__(self, raw_base):
            self.raw_base = raw_base

        def scan(self):
            return {"cell/task": ["s1"]}

        def find_pending_recordings(self, serials, converted, failed, transient):
            return list(serials)

    class FakeState(_FakeStateReconciliation):
        def __init__(self, state_file):
            self.state_file = state_file
            self.count = 0

        def load(self):
            return None

        def get_failed_serials(self, cell_task):
            return set()

        def get_transient_failed(self, cell_task):
            return {}

        def get_retry_eligible(self, cell_task):
            return []

        def get_converted_count(self, cell_task):
            return self.count

    fake = SimpleNamespace(
        RAW_BASE=tmp_path / "raw",
        LEROBOT_BASE=_lerobot_root(tmp_path),
        STATE_FILE=tmp_path / "state.json",
        NASScanner=FakeScanner,
        ConvertState=FakeState,
        shutdown_event=threading.Event(),
        _check_stop_requested=lambda: False,
        _has_other_task_request=lambda cell_task: False,
        _clear_stop_flag=lambda: None,
        _load_converted_serials=lambda output_root: set(),
    )

    def fake_convert_task(
        cell,
        task,
        recordings,
        state,
        *,
        target_fps,
    ):
        state.count += len(recordings)
        convert_calls.append((cell, task, target_fps))
        return True

    fake.convert_task = fake_convert_task
    monkeypatch.setattr(
        "backend.converter.queue_adapter._load_auto_converter_module",
        lambda: fake,
    )

    await _run_conversion({"cell_task": "cell/task", "target_fps": 24})

    assert convert_calls == [("cell", "task", 24)]


@pytest.mark.asyncio
@pytest.mark.parametrize("target_fps", [True, 0, -1, 24.0, "24"])
async def test_run_conversion_rejects_invalid_target_fps_before_scan(
    monkeypatch,
    target_fps,
):
    class ScannerMustNotRun:
        def __init__(self, raw_base):
            raise AssertionError("scanner must not run")

    fake = SimpleNamespace(
        RAW_BASE=Path("/raw"),
        NASScanner=ScannerMustNotRun,
        shutdown_event=threading.Event(),
    )
    monkeypatch.setattr(
        "backend.converter.queue_adapter._load_auto_converter_module",
        lambda: fake,
    )

    with pytest.raises(ValueError, match="target_fps"):
        await _run_conversion(
            {"cell_task": "cell/task", "target_fps": target_fps}
        )


@pytest.mark.asyncio
async def test_run_conversion_rejects_target_fps_without_task_before_scan(
    monkeypatch,
):
    class ScannerMustNotRun:
        def __init__(self, raw_base):
            raise AssertionError("scanner must not run")

    fake = SimpleNamespace(
        RAW_BASE=Path("/raw"),
        NASScanner=ScannerMustNotRun,
        shutdown_event=threading.Event(),
    )
    monkeypatch.setattr(
        "backend.converter.queue_adapter._load_auto_converter_module",
        lambda: fake,
    )

    with pytest.raises(ValueError, match="requires.*cell_task"):
        await _run_conversion({"target_fps": 24})


@pytest.mark.asyncio
async def test_default_run_conversion_scans_all_pending_tasks_for_empty_payload(monkeypatch, tmp_path):
    convert_calls = []

    class FakeScanner:
        def __init__(self, raw_base):
            self.raw_base = raw_base

        def scan(self):
            return {
                "cell_a/task_one": ["a1", "a2"],
                "cell_b/task_two": ["b1"],
            }

        def find_pending_recordings(self, serials, converted, failed, transient):
            assert converted == set()
            assert failed == set()
            assert transient == set()
            return list(serials)

    class FakeState(_FakeStateReconciliation):
        def __init__(self, state_file):
            self.state_file = state_file
            self.count = 0

        def load(self):
            return None

        def get_failed_serials(self, cell_task):
            return set()

        def get_transient_failed(self, cell_task):
            return {}

        def get_retry_eligible(self, cell_task):
            return []

        def get_converted_count(self, cell_task):
            return self.count

    fake = SimpleNamespace(
        RAW_BASE=tmp_path / "raw",
        LEROBOT_BASE=_lerobot_root(tmp_path),
        STATE_FILE=tmp_path / "state.json",
        NASScanner=FakeScanner,
        ConvertState=FakeState,
        shutdown_event=threading.Event(),
        _check_stop_requested=lambda: False,
        _has_other_task_request=lambda cell_task: False,
        _clear_stop_flag=lambda: None,
        _load_converted_serials=lambda output_root: set(),
    )

    def fake_convert_task(cell, task, recordings, state):
        state.count += len(recordings)
        convert_calls.append((cell, task, recordings, state.state_file))
        return True

    fake.convert_task = fake_convert_task
    monkeypatch.setattr(
        "backend.converter.queue_adapter._load_auto_converter_module",
        lambda: fake,
    )

    await _run_conversion({})

    assert convert_calls == [
        ("cell_a", "task_one", ["a1", "a2"], tmp_path / "state.json"),
        ("cell_b", "task_two", ["b1"], tmp_path / "state.json"),
    ]


@pytest.mark.asyncio
async def test_run_conversion_records_current_recording_progress(monkeypatch, tmp_path):
    class FakeScanner:
        def __init__(self, raw_base):
            self.raw_base = raw_base

        def scan(self):
            return {"cell/task": ["s1"]}

        def find_pending_recordings(self, serials, converted, failed, transient):
            return list(serials)

    class FakeState(_FakeStateReconciliation):
        def __init__(self, state_file):
            self.state_file = state_file
            self.count = 0

        def load(self):
            return None

        def get_failed_serials(self, cell_task):
            return set()

        def get_transient_failed(self, cell_task):
            return {}

        def get_retry_eligible(self, cell_task):
            return []

        def get_converted_count(self, cell_task):
            return self.count

    fake = SimpleNamespace(
        RAW_BASE=tmp_path / "raw",
        LEROBOT_BASE=_lerobot_root(tmp_path),
        STATE_FILE=tmp_path / "state.json",
        NASScanner=FakeScanner,
        ConvertState=FakeState,
        shutdown_event=threading.Event(),
        _check_stop_requested=lambda: False,
        _has_other_task_request=lambda cell_task: False,
        _clear_stop_flag=lambda: None,
        _load_converted_serials=lambda output_root: set(),
        _emit_event=lambda event: None,
    )

    def fake_convert_task(cell, task, recordings, state):
        fake._emit_event({
            "type": "recording_start",
            "recording": f"{cell}/{task}/{recordings[0]}",
            "index": 1,
            "total": 1,
        })
        state.count += 1
        return SimpleNamespace(mount_ok=True, converted_count=1)

    fake.convert_task = fake_convert_task
    monkeypatch.setattr(
        "backend.converter.queue_adapter._load_auto_converter_module",
        lambda: fake,
    )

    enq = await jobs_repo.enqueue(type_="convert", payload={})
    await db.execute(
        "UPDATE jobs SET status='running', worker_id='converter' WHERE id=$1",
        enq["id"],
    )

    await _run_conversion({}, job_id=enq["id"])

    job = await jobs_repo.fetch(enq["id"])
    assert job["progress"]["cell_task"] == "cell/task"
    assert job["progress"]["recording"] == "cell/task/s1"
    assert job["progress"]["recording_index"] == 1
    assert job["progress"]["recording_total"] == 1


@pytest.mark.asyncio
async def test_default_run_conversion_stops_when_task_makes_no_durable_progress(monkeypatch, tmp_path):
    convert_calls = []

    class FakeScanner:
        def __init__(self, raw_base):
            self.raw_base = raw_base

        def scan(self):
            return {
                "cell_a/task_one": ["a1"],
                "cell_b/task_two": ["b1"],
            }

        def find_pending_recordings(self, serials, converted, failed, transient):
            return list(serials)

    class FakeState(_FakeStateReconciliation):
        def __init__(self, state_file):
            self.state_file = state_file

        def load(self):
            return None

        def get_failed_serials(self, cell_task):
            return set()

        def get_transient_failed(self, cell_task):
            return {}

        def get_retry_eligible(self, cell_task):
            return []

        def get_converted_count(self, cell_task):
            return 0

    fake = SimpleNamespace(
        RAW_BASE=tmp_path / "raw",
        LEROBOT_BASE=_lerobot_root(tmp_path),
        STATE_FILE=tmp_path / "state.json",
        NASScanner=FakeScanner,
        ConvertState=FakeState,
        shutdown_event=threading.Event(),
        _check_stop_requested=lambda: False,
        _has_other_task_request=lambda cell_task: False,
        _clear_stop_flag=lambda: None,
        _load_converted_serials=lambda output_root: set(),
    )

    def fake_convert_task(cell, task, recordings, state):
        convert_calls.append(f"{cell}/{task}")
        return True

    fake.convert_task = fake_convert_task
    monkeypatch.setattr(
        "backend.converter.queue_adapter._load_auto_converter_module",
        lambda: fake,
    )

    with pytest.raises(RuntimeError, match="made no durable progress"):
        await _run_conversion({})

    assert convert_calls == ["cell_a/task_one"]


@pytest.mark.asyncio
async def test_run_conversion_accepts_output_progress_when_state_is_stale(monkeypatch, tmp_path):
    """A rebuilt dataset may regain an old state count without changing it."""
    output_serials: set[str] = set()

    class FakeScanner:
        def __init__(self, raw_base):
            self.raw_base = raw_base

        def scan(self):
            return {"cell/task": ["s1"]}

        def find_pending_recordings(self, serials, converted, failed, transient):
            return list(serials)

    class FakeState(_FakeStateReconciliation):
        def __init__(self, state_file):
            self.state_file = state_file

        def load(self):
            return None

        def get_failed_serials(self, cell_task):
            return set()

        def get_transient_failed(self, cell_task):
            return {}

        def get_retry_eligible(self, cell_task):
            return []

        def get_converted_count(self, cell_task):
            return 90

    fake = SimpleNamespace(
        RAW_BASE=tmp_path / "raw",
        LEROBOT_BASE=_lerobot_root(tmp_path),
        STATE_FILE=tmp_path / "state.json",
        NASScanner=FakeScanner,
        ConvertState=FakeState,
        shutdown_event=threading.Event(),
        _check_stop_requested=lambda: False,
        _has_other_task_request=lambda cell_task: False,
        _clear_stop_flag=lambda: None,
        _load_converted_serials=lambda output_root: set(output_serials),
    )

    def fake_convert_task(cell, task, recordings, state):
        output_serials.add("s1")
        return True

    fake.convert_task = fake_convert_task
    monkeypatch.setattr(
        "backend.converter.queue_adapter._load_auto_converter_module",
        lambda: fake,
    )

    await _run_conversion({"cell_task": "cell/task"})

    assert output_serials == {"s1"}


@pytest.mark.asyncio
async def test_default_run_conversion_stops_when_converter_state_regresses(monkeypatch, tmp_path):
    convert_calls = []

    class FakeScanner:
        def __init__(self, raw_base):
            self.raw_base = raw_base

        def scan(self):
            return {
                "cell_a/task_one": ["a1"],
                "cell_b/task_two": ["b1"],
            }

        def find_pending_recordings(self, serials, converted, failed, transient):
            return list(serials)

    class FakeState(_FakeStateReconciliation):
        def __init__(self, state_file):
            self.state_file = state_file
            self.count = 5

        def load(self):
            return None

        def get_failed_serials(self, cell_task):
            return set()

        def get_transient_failed(self, cell_task):
            return {}

        def get_retry_eligible(self, cell_task):
            return []

        def get_converted_count(self, cell_task):
            return self.count

    fake = SimpleNamespace(
        RAW_BASE=tmp_path / "raw",
        LEROBOT_BASE=_lerobot_root(tmp_path),
        STATE_FILE=tmp_path / "state.json",
        NASScanner=FakeScanner,
        ConvertState=FakeState,
        shutdown_event=threading.Event(),
        _check_stop_requested=lambda: False,
        _has_other_task_request=lambda cell_task: False,
        _clear_stop_flag=lambda: None,
        _load_converted_serials=lambda output_root: set(),
    )

    def fake_convert_task(cell, task, recordings, state):
        convert_calls.append(f"{cell}/{task}")
        state.count = 0
        return SimpleNamespace(mount_ok=True, converted_count=0)

    fake.convert_task = fake_convert_task
    monkeypatch.setattr(
        "backend.converter.queue_adapter._load_auto_converter_module",
        lambda: fake,
    )

    with pytest.raises(RuntimeError, match="state regressed"):
        await _run_conversion({})

    assert convert_calls == ["cell_a/task_one"]
