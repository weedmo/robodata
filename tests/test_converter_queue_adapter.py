import pytest
import threading
from types import SimpleNamespace
from backend.jobs import repo as jobs_repo
from backend.core.db import db, init_db
from backend.converter.input_format import (
    TaskFormatSelection,
    select_task_recordings as real_select_task_recordings,
)
from backend.converter.queue_adapter import (
    _convert_payload_sync,
    _run_conversion,
    process_one_queued,
)

pytestmark = pytest.mark.usefixtures("_point_settings_at_test_db")


@pytest.fixture(autouse=True)
def default_recordings_are_mcap(monkeypatch):
    """Existing queue tests use fake scanners without creating raw files."""
    monkeypatch.setattr(
        "backend.converter.queue_adapter.select_task_recordings",
        lambda task_dir, serials, requested_format: TaskFormatSelection(
            task_format="mcap",
            recordings=tuple(serials),
            skipped=(),
        ),
    )


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
async def test_picks_up_mcap_to_fb_convert_and_stores_result(monkeypatch, tmp_path):
    calls = []

    def fake_convert(payload, *, raw_base, cancel_requested, progress_callback):
        calls.append((dict(payload), raw_base, cancel_requested.is_set()))
        progress_callback({"phase": "complete"})
        return {
            "source_cell_task": "cell/task",
            "output_cell_task": "cell/task_fb",
            "converted_recordings": [],
        }

    monkeypatch.setenv("RAW_BASE", str(tmp_path / "raw"))
    monkeypatch.setattr(
        "backend.converter.mcap_to_fb_job._convert_task_sync",
        fake_convert,
    )
    enqueued = await jobs_repo.enqueue(
        type_="mcap_to_fb_convert",
        payload={"cell_task": "cell/task"},
    )

    await process_one_queued(idle_sleep=0)

    job = await jobs_repo.fetch(enqueued["id"])
    assert job["status"] == "complete"
    assert job["result"]["output_cell_task"] == "cell/task_fb"
    assert calls == [({"cell_task": "cell/task"}, tmp_path / "raw", False)]


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


@pytest.mark.asyncio
async def test_fb_format_rebuilds_task_with_gpu_converter(monkeypatch, tmp_path):
    converted_serials = set()
    fb_calls = []

    class FakeScanner:
        def __init__(self, raw_base):
            self.raw_base = raw_base

        def scan(self):
            return {"cell/task": ["s1", "s2"]}

    class FakeState:
        def __init__(self, state_file):
            self.updated = None

        def load(self):
            return None

        def update(self, cell_task, serial, count):
            self.updated = (cell_task, serial, count)

        def flush(self):
            return None

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
        _load_converted_serials=lambda output_root: set(converted_serials),
    )

    monkeypatch.setattr(
        "backend.converter.queue_adapter.select_task_recordings",
        lambda task_dir, serials, requested_format: TaskFormatSelection(
            task_format="fb",
            recordings=("s1", "s2"),
            skipped=(),
        ),
    )

    def fake_fb_convert(**kwargs):
        fb_calls.append(kwargs)
        converted_serials.update(kwargs["recordings"])

    monkeypatch.setattr(
        "backend.converter.queue_adapter.convert_fb_task",
        fake_fb_convert,
    )
    monkeypatch.setattr(
        "backend.converter.queue_adapter._load_auto_converter_module",
        lambda: fake,
    )

    await _run_conversion({"cell_task": "cell/task", "format": "fb"})

    assert len(fb_calls) == 1
    assert fb_calls[0]["cell_task"] == "cell/task"
    assert fb_calls[0]["recordings"] == ["s1", "s2"]


def test_auto_routing_converts_first_format_and_reports_mismatch(monkeypatch, tmp_path):
    raw_base = tmp_path / "raw"
    task_dir = raw_base / "cell" / "task"
    mcap_dir = task_dir / "001"
    mcap_dir.mkdir(parents=True)
    (mcap_dir / "metacard.json").write_text("{}", encoding="utf-8")
    (mcap_dir / "001_0.mcap").touch()
    fb_dir = task_dir / "002"
    (fb_dir / "images" / "cam_head").mkdir(parents=True)
    (fb_dir / "state").mkdir()
    (fb_dir / "metacard.json").write_text("{}", encoding="utf-8")
    (fb_dir / "images" / "cam_head" / "000.fb").touch()
    (fb_dir / "state" / "state_0.mcap").touch()
    convert_calls = []
    progress = []

    class FakeScanner:
        def __init__(self, raw_path):
            self.raw_path = raw_path

        def scan(self):
            return {"cell/task": ["001", "002"]}

        def find_pending_recordings(self, serials, converted, failed, transient):
            return list(serials)

    class FakeState:
        def __init__(self, state_file):
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
        RAW_BASE=raw_base,
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
        convert_calls.append(recordings)
        state.count += len(recordings)
        return True

    fake.convert_task = fake_convert_task
    monkeypatch.setattr(
        "backend.converter.queue_adapter.select_task_recordings",
        real_select_task_recordings,
    )

    _convert_payload_sync(
        fake,
        {"cell_task": "cell/task", "format": ""},
        cancel_requested=threading.Event(),
        progress_callback=progress.append,
    )

    assert convert_calls == [["001"]]
    assert progress[-1]["phase"] == "complete"
    assert progress[-1]["task_format"] == "mcap"
    assert progress[-1]["skipped_recordings"][0]["serial"] == "002"
    assert "use one format per task" in progress[-1]["skipped_recordings"][0]["reason"]
