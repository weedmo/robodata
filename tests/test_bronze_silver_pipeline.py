from pathlib import Path

import pytest

from backend.core.db import close_db, db, get_db, init_db, _reset
from backend.datasets.services import bronze_silver_pipeline as pipeline
from backend.datasets.services.bronze_silver_pipeline import (
    PIPELINE_LOCK_KEY,
    STATE_SILVER_FAILED,
    STATE_SILVER_READY,
    run_bronze_to_silver_batch,
)

pytestmark = pytest.mark.usefixtures("_point_settings_at_test_db")


@pytest.fixture(autouse=True)
async def reset_db():
    _reset()
    await init_db()
    await db.execute(
        "TRUNCATE TABLE episode_curation_states, jobs, dataset_stats, "
        "episode_serials, datasets, annotations RESTART IDENTITY CASCADE"
    )
    yield
    await close_db()


def _bronze_episode(root: Path, serial: str = "20260616_120000") -> Path:
    episode = root / "bronze" / "cell001" / "pick_part" / serial
    episode.mkdir(parents=True)
    (episode / "recording.mcap").write_text("raw")
    return episode


async def test_batch_moves_bronze_episode_to_silver_and_registers_dataset(tmp_path):
    bronze_path = _bronze_episode(tmp_path)

    result = await run_bronze_to_silver_batch(data_root=tmp_path)

    assert result["status"] == "complete"
    assert result["processed"] == 1
    assert result["failed"] == 0
    assert not bronze_path.exists()

    serial = "20260616_120000"
    silver_path = tmp_path / "silver_label_data" / "cell001" / "pick_part" / "LeRobot" / serial
    assert (silver_path / "recording.mcap").read_text() == "raw"
    assert not (tmp_path / "rrd").exists()

    state = await db.fetch_one(
        "SELECT state, retry_required, silver_path "
        "FROM episode_curation_states WHERE serial_number = $1",
        serial,
    )
    assert state is not None
    assert state["state"] == STATE_SILVER_READY
    assert state["retry_required"] is False
    assert state["silver_path"] == str(silver_path)

    serial_row = await db.fetch_one(
        "SELECT es.serial_number, d.features->>'source' AS source, "
        "jsonb_exists(d.features, 'rrd_path') AS has_rrd_path "
        "FROM episode_serials es "
        "JOIN datasets d ON d.id = es.dataset_id "
        "WHERE d.path = $1",
        str(silver_path),
    )
    assert serial_row is not None
    assert serial_row["serial_number"] == serial
    assert serial_row["source"] == "bronze_silver_batch"
    assert serial_row["has_rrd_path"] is False


async def test_batch_marks_failure_without_automatic_retry(tmp_path, monkeypatch):
    bronze_path = _bronze_episode(tmp_path, "20260616_130000")

    def fail(_row):
        raise RuntimeError("converter failed")

    monkeypatch.setattr(pipeline, "_process_episode_files", fail)

    result = await run_bronze_to_silver_batch(data_root=tmp_path)

    assert result["processed"] == 0
    assert result["failed"] == 1
    assert bronze_path.exists()
    state = await db.fetch_one(
        "SELECT state, failure_reason, retry_required "
        "FROM episode_curation_states WHERE serial_number = $1",
        "20260616_130000",
    )
    assert state is not None
    assert state["state"] == STATE_SILVER_FAILED
    assert state["failure_reason"] == "converter failed"
    assert state["retry_required"] is True

    retry = await run_bronze_to_silver_batch(data_root=tmp_path)
    assert retry["eligible"] == 0
    assert retry["processed"] == 0


async def test_batch_skips_when_advisory_lock_is_held(tmp_path):
    _bronze_episode(tmp_path)
    direct = await get_db()
    async with direct.transaction():
        async with direct.execute(
            "SELECT pg_advisory_xact_lock(hashtext(?))",
            (PIPELINE_LOCK_KEY,),
        ) as cur:
            await cur.fetchone()

        result = await run_bronze_to_silver_batch(data_root=tmp_path)

    assert result == {"status": "skipped", "reason": "batch_already_running"}


async def test_stale_processing_transitions_to_failed(tmp_path):
    serial = "20260616_140000"
    await db.execute(
        "INSERT INTO episode_curation_states "
        "(serial_number, cell, task, bronze_path, silver_path, state, processing_started_at) "
        "VALUES ($1, 'cell001', 'pick_part', $2, $3, $4, NOW() - interval '2 hours')",
        serial,
        str(tmp_path / "bronze" / "cell001" / "pick_part" / serial),
        str(tmp_path / "silver_label_data" / "cell001" / "pick_part" / "LeRobot" / serial),
        pipeline.STATE_SILVER_PROCESSING,
    )

    result = await run_bronze_to_silver_batch(
        data_root=tmp_path,
        processing_timeout_seconds=60,
    )

    assert result["stale_failed"] == 1
    state = await db.fetch_one(
        "SELECT state, failure_reason, retry_required "
        "FROM episode_curation_states WHERE serial_number = $1",
        serial,
    )
    assert state is not None
    assert state["state"] == STATE_SILVER_FAILED
    assert state["failure_reason"] == "silver_processing timeout"
    assert state["retry_required"] is True
