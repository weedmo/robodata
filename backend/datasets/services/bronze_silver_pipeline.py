"""Bronze-to-silver batch pipeline state and file movement."""
from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from backend.core.db import db

PIPELINE_LOCK_KEY = "bronze_silver_batch"
STATE_BRONZE_DETECTED = "bronze_detected"
STATE_SILVER_PROCESSING = "silver_processing"
STATE_SILVER_READY_RRD = "silver_ready_rrd"
STATE_HUMAN_CURATING = "human_curating"
STATE_GOLD_READY = "gold_ready"
STATE_SILVER_FAILED = "silver_failed"

TERMINAL_OR_MANUAL_STATES = {
    STATE_SILVER_READY_RRD,
    STATE_HUMAN_CURATING,
    STATE_GOLD_READY,
    STATE_SILVER_FAILED,
}


@dataclass(frozen=True)
class BronzeEpisode:
    serial_number: str
    cell: str
    task: str
    bronze_path: Path
    silver_path: Path
    rrd_path: Path


def _data_root(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _discover_bronze_episodes(data_root: Path) -> list[BronzeEpisode]:
    bronze_root = data_root / "bronze"
    silver_root = data_root / "silver_label_data"
    rrd_root = data_root / "rrd"
    if not bronze_root.exists():
        return []

    episodes: list[BronzeEpisode] = []
    for recording_dir in sorted(p for p in bronze_root.glob("*/*/*") if p.is_dir()):
        rel = recording_dir.relative_to(bronze_root)
        cell, task, serial = rel.parts
        episodes.append(
            BronzeEpisode(
                serial_number=serial,
                cell=cell,
                task=task,
                bronze_path=recording_dir,
                silver_path=silver_root / cell / task / "LeRobot" / serial,
                rrd_path=rrd_root / cell / task / f"{serial}.rrd",
            )
        )
    return episodes


async def _try_batch_lock() -> bool:
    row = await db.fetch_one(
        "SELECT pg_try_advisory_xact_lock(hashtext($1)) AS acquired",
        PIPELINE_LOCK_KEY,
    )
    return bool(row and row["acquired"])


async def _mark_stale_processing_failed(timeout_seconds: int) -> int:
    rows = await db.fetch_all(
        "UPDATE episode_curation_states "
        "SET state = $2, failure_reason = $3, retry_required = TRUE, updated_at = NOW() "
        "WHERE state = $1 "
        "AND processing_started_at IS NOT NULL "
        "AND processing_started_at < NOW() - ($4 * interval '1 second') "
        "RETURNING serial_number",
        STATE_SILVER_PROCESSING,
        STATE_SILVER_FAILED,
        "silver_processing timeout",
        timeout_seconds,
    )
    return len(rows)


async def _upsert_detected(episode: BronzeEpisode) -> None:
    await db.execute(
        "INSERT INTO episode_curation_states "
        "(serial_number, cell, task, bronze_path, silver_path, rrd_path, state) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7) "
        "ON CONFLICT (serial_number) DO UPDATE "
        "SET cell = EXCLUDED.cell, task = EXCLUDED.task, "
        "    bronze_path = EXCLUDED.bronze_path, silver_path = EXCLUDED.silver_path, "
        "    rrd_path = EXCLUDED.rrd_path, updated_at = NOW() "
        "WHERE episode_curation_states.state = $7",
        episode.serial_number,
        episode.cell,
        episode.task,
        str(episode.bronze_path),
        str(episode.silver_path),
        str(episode.rrd_path),
        STATE_BRONZE_DETECTED,
    )


async def _eligible_episodes(limit: int | None) -> list[Mapping[str, Any]]:
    sql = (
        "SELECT serial_number, cell, task, bronze_path, silver_path, rrd_path, state "
        "FROM episode_curation_states "
        "WHERE state = $1 "
        "ORDER BY updated_at, serial_number"
    )
    params: list[Any] = [STATE_BRONZE_DETECTED]
    if limit is not None:
        params.append(limit)
        sql += " LIMIT $2"
    return [dict(row) for row in await db.fetch_all(sql, *params)]


async def _mark_processing(serial_number: str) -> None:
    await db.execute(
        "UPDATE episode_curation_states "
        "SET state = $2, processing_started_at = NOW(), failure_reason = NULL, "
        "    retry_required = FALSE, updated_at = NOW() "
        "WHERE serial_number = $1",
        serial_number,
        STATE_SILVER_PROCESSING,
    )


async def _mark_ready(row: Mapping[str, Any]) -> None:
    dataset_id = await _ensure_silver_dataset_registered(row)
    await db.execute(
        "INSERT INTO episode_serials (dataset_id, episode_index, serial_number) "
        "VALUES ($1, $2, $3) "
        "ON CONFLICT (dataset_id, episode_index) DO UPDATE "
        "SET serial_number = EXCLUDED.serial_number",
        dataset_id,
        0,
        row["serial_number"],
    )
    await db.execute(
        "UPDATE episode_curation_states "
        "SET state = $2, failure_reason = NULL, retry_required = FALSE, updated_at = NOW() "
        "WHERE serial_number = $1",
        row["serial_number"],
        STATE_SILVER_READY_RRD,
    )


async def _mark_failed(serial_number: str, reason: str) -> None:
    await db.execute(
        "UPDATE episode_curation_states "
        "SET state = $2, failure_reason = $3, retry_required = TRUE, updated_at = NOW() "
        "WHERE serial_number = $1",
        serial_number,
        STATE_SILVER_FAILED,
        reason[:1000],
    )


async def _ensure_silver_dataset_registered(row: Mapping[str, Any]) -> int:
    silver_path = str(row["silver_path"])
    existing = await db.fetch_one("SELECT id FROM datasets WHERE path = $1", silver_path)
    if existing is not None:
        return int(existing["id"])
    inserted = await db.fetch_one(
        "INSERT INTO datasets(path, name, cell_name, robot_type, features, total_episodes) "
        "VALUES ($1, $2, $3, $4, $5::jsonb, 1) "
        "ON CONFLICT (path) DO UPDATE SET synced_at = NOW() "
        "RETURNING id",
        silver_path,
        str(row["task"]),
        str(row["cell"]),
        "lerobot",
        json.dumps({"source": "bronze_silver_batch", "rrd_path": str(row["rrd_path"])}),
    )
    assert inserted is not None
    return int(inserted["id"])


def _process_episode_files(row: Mapping[str, Any]) -> None:
    bronze_path = Path(str(row["bronze_path"]))
    silver_path = Path(str(row["silver_path"]))
    rrd_path = Path(str(row["rrd_path"]))

    if bronze_path.exists():
        silver_path.parent.mkdir(parents=True, exist_ok=True)
        if silver_path.exists():
            raise FileExistsError(f"silver output already exists: {silver_path}")
        _write_rerun_rrd(bronze_path, rrd_path)
        shutil.move(str(bronze_path), str(silver_path))
    elif not silver_path.exists():
        raise FileNotFoundError(f"bronze episode not found: {bronze_path}")
    elif not rrd_path.exists():
        _write_rerun_rrd(silver_path, rrd_path)


def _write_rerun_rrd(recording_dir: Path, rrd_path: Path) -> None:
    if rrd_path.exists():
        return
    rrd_path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_converter_repo_on_path()
    from conversion.rerun_viz import visualize_recording_dir

    visualize_recording_dir(
        str(recording_dir),
        save_path=str(rrd_path),
        spawn=False,
    )
    if not rrd_path.exists():
        raise RuntimeError(f"converter did not create rrd output: {rrd_path}")


def _ensure_converter_repo_on_path() -> None:
    repo_path = Path(__file__).resolve().parents[3] / "rosbag2lerobot-svt"
    if repo_path.is_dir() and str(repo_path) not in sys.path:
        sys.path.insert(0, str(repo_path))


async def run_bronze_to_silver_batch(
    *,
    data_root: str | Path,
    processing_timeout_seconds: int = 3600,
    limit: int | None = None,
) -> dict[str, Any]:
    root = _data_root(data_root)
    processed = 0
    failed = 0

    async with db.transaction():
        if not await _try_batch_lock():
            return {"status": "skipped", "reason": "batch_already_running"}

        stale_failed = await _mark_stale_processing_failed(processing_timeout_seconds)
        for episode in _discover_bronze_episodes(root):
            await _upsert_detected(episode)

        eligible = await _eligible_episodes(limit)
        for row in eligible:
            await _mark_processing(str(row["serial_number"]))
            try:
                _process_episode_files(row)
            except Exception as exc:
                failed += 1
                await _mark_failed(str(row["serial_number"]), str(exc))
            else:
                processed += 1
                await _mark_ready(row)

    return {
        "status": "complete",
        "data_root": str(root),
        "processed": processed,
        "failed": failed,
        "stale_failed": stale_failed,
        "eligible": len(eligible),
    }


async def handle_bronze_silver_batch(job: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(job.get("payload") or {})
    data_root = payload.get("data_root")
    if not data_root:
        raise ValueError("bronze_silver_batch payload requires data_root")
    limit = payload.get("limit")
    return await run_bronze_to_silver_batch(
        data_root=data_root,
        processing_timeout_seconds=int(payload.get("processing_timeout_seconds", 3600)),
        limit=int(limit) if limit is not None else None,
    )
