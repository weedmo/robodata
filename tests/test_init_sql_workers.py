import pytest

from backend.core.db import db, init_db

pytestmark = pytest.mark.usefixtures("_point_settings_at_test_db")


@pytest.fixture(autouse=True)
async def ensure_schema():
    await init_db()


@pytest.mark.asyncio
async def test_worker_state_enum_exists():
    rows = await db.fetch_all(
        "SELECT enumlabel FROM pg_enum e "
        "JOIN pg_type t ON e.enumtypid = t.oid "
        "WHERE t.typname = 'worker_state' ORDER BY enumsortorder"
    )
    assert [r["enumlabel"] for r in rows] == [
        "running", "paused", "draining", "stopped",
    ]


@pytest.mark.asyncio
async def test_worker_controls_seeded():
    rows = await db.fetch_all(
        "SELECT worker_id, desired_state FROM worker_controls ORDER BY worker_id"
    )
    assert [(r["worker_id"], r["desired_state"]) for r in rows] == [
        ("converter", "running"),
        ("curation-worker", "running"),
    ]


@pytest.mark.asyncio
async def test_worker_heartbeats_table_exists():
    cols = await db.fetch_all(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'worker_heartbeats' ORDER BY column_name"
    )
    assert {c["column_name"] for c in cols} == {
        "worker_id", "actual_state", "pid", "container_id",
        "last_beat_at", "in_flight_job_id", "detail",
    }
