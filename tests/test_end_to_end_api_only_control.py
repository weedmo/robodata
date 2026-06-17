"""End-to-end smoke for the API-only control plane (in-process drive).

Drives backend.converter.queue_adapter.process_one_queued from inside the
test so we can verify the full HTTP → DB → runtime → handler → status path
without needing a long-running worker container. Container-lifecycle
guarantees (no restart on convert) are enforced at code level by
scripts/verify_no_host_control.sh and proven structurally there.
"""
import asyncio
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from backend.main import app
from backend.core.db import db, init_db, _reset
from backend.converter.queue_adapter import process_one_queued
from backend.datasets.services import bronze_silver_pipeline as pipeline
from backend.workers.runtime import CancelledNormally

pytestmark = pytest.mark.usefixtures("_point_settings_at_test_db")


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


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
async def test_post_jobs_then_drive_runtime_marks_complete(monkeypatch):
    completed_payloads = []

    async def fake_convert(payload, *, job_id=None, check_cancel=None):
        completed_payloads.append(dict(payload))

    monkeypatch.setattr(
        "backend.converter.queue_adapter._run_conversion", fake_convert,
    )

    async with _client() as ac:
        resp = await ac.post(
            "/api/jobs",
            json={"type": "convert", "payload": {"cell": "smoke/test"}},
            headers={"X-User-Name": "smoke"},
        )
        assert resp.status_code == 201
        job_id = resp.json()["id"]

        await process_one_queued(idle_sleep=0)

        r = await ac.get(f"/api/jobs/{job_id}")

    assert r.json()["status"] == "complete"
    assert completed_payloads == [{"cell": "smoke/test"}]


@pytest.mark.asyncio
async def test_bronze_silver_batch_runs_through_converter_worker(monkeypatch, tmp_path):
    def fake_rrd(recording_dir: Path, rrd_path: Path) -> None:
        rrd_path.parent.mkdir(parents=True, exist_ok=True)
        rrd_path.write_text(f"rrd-from:{Path(recording_dir).name}", encoding="utf-8")

    monkeypatch.setattr(pipeline, "_write_rerun_rrd", fake_rrd)
    _reset()
    await init_db()
    await db.execute(
        "TRUNCATE TABLE episode_curation_states, jobs, dataset_stats, "
        "episode_serials, datasets, annotations RESTART IDENTITY CASCADE"
    )
    await db.execute("UPDATE worker_controls SET desired_state='running', note=NULL")

    serial = "20260616_160000"
    bronze = tmp_path / "bronze" / "cell001" / "pick_part" / serial
    bronze.mkdir(parents=True)
    (bronze / "metacard.json").write_text('{"task_name":"pick_part","fps":30}', encoding="utf-8")
    (bronze / f"{serial}_0.mcap").write_bytes(b"mcap")

    async with _client() as ac:
        created = await ac.post(
            "/api/jobs",
            json={"type": "bronze_silver_batch", "payload": {"data_root": str(tmp_path)}},
        )
        assert created.status_code == 201, created.text
        job_id = created.json()["id"]

        await process_one_queued(idle_sleep=0)

        fetched = await ac.get(f"/api/jobs/{job_id}")

    silver = tmp_path / "silver_label_data" / "cell001" / "pick_part" / "LeRobot" / serial
    rrd = tmp_path / "rrd" / "cell001" / "pick_part" / f"{serial}.rrd"
    body = fetched.json()
    assert body["status"] == "complete", body
    assert body["worker_id"] == "converter"
    assert body["result"]["processed"] == 1
    assert not bronze.exists()
    assert (silver / f"{serial}_0.mcap").read_bytes() == b"mcap"
    assert rrd.read_text(encoding="utf-8") == f"rrd-from:{serial}"


@pytest.mark.asyncio
async def test_pause_blocks_new_jobs(monkeypatch):
    """When the converter worker is paused, runtime.tick must not pick up the queued job."""
    convert_calls = []

    async def fake_convert(payload, *, job_id=None, check_cancel=None):
        convert_calls.append(dict(payload))

    monkeypatch.setattr(
        "backend.converter.queue_adapter._run_conversion", fake_convert,
    )

    async with _client() as ac:
        # Pause via /api/workers requires a fresh heartbeat first.
        await db.execute(
            "INSERT INTO worker_heartbeats(worker_id, actual_state) VALUES('converter','running') "
            "ON CONFLICT(worker_id) DO UPDATE SET actual_state='running', last_beat_at=NOW()"
        )
        pr = await ac.patch(
            "/api/workers/converter",
            json={"desired_state": "paused"},
            headers={"X-User-Name": "smoke"},
        )
        assert pr.status_code == 200

        resp = await ac.post(
            "/api/jobs", json={"type": "convert", "payload": {}},
        )
        job_id = resp.json()["id"]

        # Drive a tick — should NOT pick up the queued job (worker paused).
        await process_one_queued(idle_sleep=0)

        r = await ac.get(f"/api/jobs/{job_id}")

    assert r.json()["status"] == "queued"
    assert convert_calls == []


@pytest.mark.asyncio
async def test_cancel_during_handler_marks_cancelled(monkeypatch):
    """A cancel observed mid-handler returns CancelledNormally and marks the job cancelled."""
    async def slow_with_cancel(payload, *, job_id=None, check_cancel=None):
        # Simulate the user canceling mid-conversion.
        await db.execute("UPDATE jobs SET status='cancel_requested' WHERE status='running'")
        if await check_cancel():
            return CancelledNormally(cleanup="partial output removed")

    monkeypatch.setattr(
        "backend.converter.queue_adapter._run_conversion", slow_with_cancel,
    )

    async with _client() as ac:
        resp = await ac.post(
            "/api/jobs", json={"type": "convert", "payload": {"cell": "smoke/cancel"}},
        )
        job_id = resp.json()["id"]

        await process_one_queued(idle_sleep=0)

        r = await ac.get(f"/api/jobs/{job_id}")

    assert r.json()["status"] == "cancelled"
    assert "partial output removed" in (r.json().get("error") or "")


@pytest.mark.asyncio
async def test_no_host_control_artifacts_left_in_codebase():
    """Structural guarantee that container lifecycle is decoupled from job lifecycle.

    The verify_no_host_control.sh gate is the source of truth — running it as
    part of the test pins the contract so a future regression in service.py /
    router.py would fail this test.
    """
    import subprocess
    from pathlib import Path
    repo_root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [str(repo_root / "scripts" / "verify_no_host_control.sh")],
        cwd=repo_root, capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        f"verify_no_host_control.sh failed (exit {proc.returncode}):\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
