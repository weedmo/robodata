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

    async def fake_convert(payload, *, check_cancel=None):
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
    async def with_cancel(payload, *, check_cancel):
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

        def load(self):
            return None

        def get_failed_serials(self, cell_task):
            return set()

        def get_transient_failed(self, cell_task):
            return {}

        def get_retry_eligible(self, cell_task):
            return []

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
        convert_calls.append((cell, task, recordings, state.state_file))
        return True

    fake.convert_task = fake_convert_task
    monkeypatch.setattr(
        "backend.converter.queue_adapter._load_auto_converter_module",
        lambda: fake,
    )

    await _run_conversion({"cell_task": "cell/task"})

    assert convert_calls == [("cell", "task", ["s1", "s2"], tmp_path / "state.json")]
