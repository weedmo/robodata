import pytest

from backend.core.db import db, init_db

pytestmark = pytest.mark.usefixtures("_point_settings_at_test_db")


@pytest.fixture(autouse=True)
async def ensure_schema():
    await init_db()
    await db.execute("DELETE FROM jobs")
    yield
    await db.execute("DELETE FROM jobs")


@pytest.mark.asyncio
async def test_db_facade_fetch_one_and_fetch_all():
    await db.execute(
        "INSERT INTO jobs(type, payload, dedupe_key) VALUES($1, $2::jsonb, $3)",
        "convert",
        "{}",
        "facade-1",
    )
    row = await db.fetch_one("SELECT type, dedupe_key FROM jobs WHERE dedupe_key=$1", "facade-1")
    assert row["type"] == "convert"
    assert row["dedupe_key"] == "facade-1"

    rows = await db.fetch_all("SELECT id FROM jobs WHERE dedupe_key=$1", "facade-1")
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_db_facade_transaction_keeps_statements_on_same_connection():
    async with db.transaction():
        await db.execute(
            "INSERT INTO jobs(type, payload, dedupe_key) VALUES($1, $2::jsonb, $3)",
            "convert",
            "{}",
            "facade-txn",
        )
        row = await db.fetch_one("SELECT dedupe_key FROM jobs WHERE dedupe_key=$1", "facade-txn")

    assert row["dedupe_key"] == "facade-txn"
