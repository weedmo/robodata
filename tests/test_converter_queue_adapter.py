import pytest
import threading
from types import SimpleNamespace
from backend.jobs import repo as jobs_repo
from backend.core.db import db, init_db
from backend.converter.queue_adapter import _run_conversion, process_one_queued

pytestmark = pytest.mark.usefixtures("_point_settings_at_test_db")


@pytest.fixture(autouse=True)
async def clean():
    await init_db()
    await db.execute("DELETE FROM worker_heartbeats")
    await db.execute("DELETE FROM jobs")
    await db.execute("UPDATE worker_controls SET desired_state='running', note=NULL")
    yield
    await db.execute("DELETE FROM worker_heartbeats")
    await db.execute("DELETE FROM jobs")


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

    class FakeState:
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
        LEROBOT_BASE=tmp_path / "lerobot",
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

    class FakeState:
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
        LEROBOT_BASE=tmp_path / "lerobot",
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

    class FakeState:
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
        LEROBOT_BASE=tmp_path / "lerobot",
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

    class FakeState:
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
        LEROBOT_BASE=tmp_path / "lerobot",
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

    class FakeState:
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
        LEROBOT_BASE=tmp_path / "lerobot",
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

    class FakeState:
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
        LEROBOT_BASE=tmp_path / "lerobot",
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
