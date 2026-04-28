"""Service for reading and writing episode metadata in LeRobot v3.0 parquet files.

Episode base data is read from parquet files (read-only).
Grade/tags annotations are stored in SQLite (via backend.core.db).
Legacy JSON sidecar files are automatically migrated on first access.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import json as _json
import logging
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from backend.core.config import settings
from backend.core.db import get_db
from backend.datasets.schemas import Episode
from backend.datasets.services.dataset_registry import DatasetContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parquet write-back helpers
# ---------------------------------------------------------------------------


async def _write_annotations_to_parquet(
    updates: dict[int, tuple[str | None, list[str]]],
    ctx: DatasetContext | Any,
) -> None:
    """Write grade/tags back into the episode parquet files.

    *updates* maps ``episode_index`` → ``(grade, tags)``.
    Groups updates by parquet file so each file is read/written at most once.
    """
    ds = ctx
    # Group by parquet file
    file_groups: dict[Path, dict[int, tuple[str | None, list[str]]]] = {}
    for ep_idx, (grade, tags) in updates.items():
        fp = ds.get_file_for_episode(ep_idx)
        if fp is None:
            continue
        file_groups.setdefault(fp, {})[ep_idx] = (grade, tags)

    for file_path, group in file_groups.items():
        lock = ds.get_file_lock(str(file_path))
        async with lock:
            table = await asyncio.to_thread(pq.read_table, file_path)
            indices = table.column("episode_index").to_pylist()

            # Build new grade and tags arrays
            old_grades = (
                table.column("grade").to_pylist()
                if "grade" in table.schema.names
                else [None] * table.num_rows
            )
            old_tags = (
                table.column("tags").to_pylist()
                if "tags" in table.schema.names
                else [None] * table.num_rows
            )

            new_grades = list(old_grades)
            new_tags = list(old_tags)

            for i, ep_idx in enumerate(indices):
                if ep_idx in group:
                    g, t = group[ep_idx]
                    new_grades[i] = g
                    new_tags[i] = t

            # Drop old columns if present, then append updated ones
            drop_cols = [c for c in ("grade", "tags") if c in table.schema.names]
            if drop_cols:
                table = table.drop(drop_cols)

            table = table.append_column("grade", pa.array(new_grades, type=pa.string()))
            table = table.append_column(
                "tags", pa.array(new_tags, type=pa.list_(pa.string())),
            )

            await asyncio.to_thread(pq.write_table, table, file_path)


class EpisodeNotFoundError(Exception):
    """Raised when an episode_index cannot be located in any parquet file."""


def _get_annotations_path() -> Path:
    """Return the directory for storing annotation sidecar files."""
    if settings.annotations_path:
        p = Path(settings.annotations_path)
    else:
        p = Path.home() / ".local" / "share" / "curation-tools" / "annotations"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _sidecar_file(dataset_path: Path) -> Path:
    """Return the sidecar JSON path for a given dataset."""
    path_hash = hashlib.sha256(str(dataset_path.resolve()).encode()).hexdigest()[:16]
    name = f"{dataset_path.name}_{path_hash}.json"
    return _get_annotations_path() / name


def _load_sidecar_json(dataset_path: Path) -> dict[str, Any]:
    """Load the legacy sidecar JSON. Returns {episode_index_str: {grade, tags}}."""
    path = _sidecar_file(dataset_path)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning("Corrupt sidecar file %s, starting fresh", path)
    return {}


# Keep backward-compat alias so external callers (cell_service, export_service,
# distribution_service, test_mockup) that import _load_sidecar still work.
_load_sidecar = _load_sidecar_json


# ---------------------------------------------------------------------------
# DB helper functions
# ---------------------------------------------------------------------------


async def _get_dataset_id(dataset_path: Path) -> int | None:
    db = await get_db()
    async with db.execute("SELECT id FROM datasets WHERE path = ?", (str(dataset_path.resolve()),)) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else None


async def _ensure_dataset_registered(dataset_path: Path) -> int:
    db = await get_db()
    resolved = str(dataset_path.resolve())
    async with db.execute("SELECT id FROM datasets WHERE path = ?", (resolved,)) as cursor:
        row = await cursor.fetchone()
    if row:
        dataset_id = row[0]
    else:
        await db.execute("INSERT INTO datasets (path, name) VALUES (?, ?)", (resolved, dataset_path.name))
        await db.commit()
        async with db.execute("SELECT id FROM datasets WHERE path = ?", (resolved,)) as cursor:
            dataset_id = (await cursor.fetchone())[0]

    # Ensure episode_serials is populated so annotation writes can resolve serials.
    # The lazy sync in cell_service normally handles this, but direct dataset
    # load paths (tests, load-by-path without cell browse) bypass that.
    async with db.execute(
        "SELECT COUNT(*) FROM episode_serials WHERE dataset_id = ?", (dataset_id,)
    ) as cursor:
        n = (await cursor.fetchone())[0]
    if n == 0:
        from backend.datasets.services.cell_service import _rebuild_episode_serials
        await _rebuild_episode_serials(db, dataset_id, dataset_path)
        await db.commit()

    return dataset_id


async def _ensure_migrated(dataset_id: int, dataset_path: Path) -> None:
    db = await get_db()
    migrated_legacy = await _migrate_legacy_episode_annotations(dataset_id)
    if migrated_legacy > 0:
        await _refresh_dataset_stats(dataset_id)
        logger.info(
            "Migrated %d legacy episode_annotations rows for %s",
            migrated_legacy,
            dataset_path.name,
        )

    # If any annotation already exists for any recording in this dataset,
    # assume migration has already run (or the user has entered fresh grades
    # post-v4) and skip to avoid clobbering.
    async with db.execute(
        """SELECT COUNT(*)
           FROM episode_serials es
           JOIN annotations a ON a.serial_number = es.serial_number
           WHERE es.dataset_id = ?""",
        (dataset_id,),
    ) as cursor:
        count_row = await cursor.fetchone()
    if count_row[0] > 0:
        return

    sidecar = _load_sidecar_json(dataset_path)
    if not sidecar:
        return

    migrated = 0
    for idx_str, ann in sidecar.items():
        serial = await _get_serial(db, dataset_id, int(idx_str))
        if serial is None:
            logger.warning(
                "sidecar migration: no serial for ep %s in %s; skipping",
                idx_str, dataset_path,
            )
            continue
        await db.execute(
            """
            INSERT INTO annotations (serial_number, grade, tags, reason)
            VALUES (?, ?, ?, NULL)
            ON CONFLICT (serial_number) DO NOTHING
            """,
            (serial, ann.get("grade"), _json.dumps(ann.get("tags", []))),
        )
        migrated += 1
    await db.commit()
    await _refresh_dataset_stats(dataset_id)
    logger.info(
        "Migrated %d annotations from sidecar for %s (of %d entries)",
        migrated, dataset_path.name, len(sidecar),
    )


async def _migrate_legacy_episode_annotations(dataset_id: int) -> int:
    """Copy legacy dataset_id/episode_index annotations into serial-keyed annotations."""
    db = await get_db()
    if not await _table_exists(db, "episode_annotations"):
        return 0

    async with db.execute(
        """
        SELECT ea.episode_index, ea.grade, ea.tags, ea.reason, es.serial_number
        FROM episode_annotations ea
        LEFT JOIN episode_serials es
          ON es.dataset_id = ea.dataset_id AND es.episode_index = ea.episode_index
        WHERE ea.dataset_id = ?
        ORDER BY ea.episode_index
        """,
        (dataset_id,),
    ) as cursor:
        rows = await cursor.fetchall()

    migrated = 0
    attempted_inserts = 0
    for row in rows:
        serial = row[4]
        if serial is None:
            logger.warning(
                "legacy annotation migration: no serial for dataset_id=%s episode=%s; skipping",
                dataset_id,
                row[0],
            )
            continue
        before = db.total_changes
        attempted_inserts += 1
        await db.execute(
            """
            INSERT INTO annotations (serial_number, grade, tags, reason)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (serial_number) DO NOTHING
            """,
            (serial, row[1], row[2] or "[]", row[3]),
        )
        if db.total_changes > before:
            migrated += 1

    if attempted_inserts > 0:
        await db.commit()
    return migrated


async def _get_serial(db, dataset_id: int, episode_index: int) -> str | None:
    """Resolve the Serial_number for an (dataset_id, episode_index) pair."""
    async with db.execute(
        "SELECT serial_number FROM episode_serials WHERE dataset_id = ? AND episode_index = ?",
        (dataset_id, episode_index),
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row else None


def _read_episode_serial_from_parquet(
    episode_index: int,
    ctx: DatasetContext | Any,
) -> str | None:
    """Resolve Serial_number directly from parquet for episodes not in episode_serials."""
    ds = ctx
    file_path = ds.get_file_for_episode(episode_index)
    if file_path is None:
        return None
    schema = pq.read_schema(file_path)
    if "Serial_number" not in schema.names:
        return None
    table = pq.read_table(file_path, columns=["episode_index", "Serial_number"])
    indices = table.column("episode_index").to_pylist()
    serials = table.column("Serial_number").to_pylist()
    for idx, serial in zip(indices, serials):
        if int(idx) == episode_index and serial not in (None, ""):
            return str(serial)
    return None


async def _get_episode_serial(
    dataset_id: int,
    episode_index: int,
    ctx: DatasetContext | Any,
) -> str | None:
    db = await get_db()
    serial = await _get_serial(db, dataset_id, episode_index)
    if serial is not None:
        return serial
    return await asyncio.to_thread(_read_episode_serial_from_parquet, episode_index, ctx)


async def _table_exists(db, table_name: str) -> bool:
    async with db.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = current_schema() AND table_name = ?
        ) AS exists
        """,
        (table_name,),
    ) as cursor:
        row = await cursor.fetchone()
    return bool(row and row["exists"])


async def _load_annotations_from_db(dataset_id: int) -> dict[int, dict]:
    db = await get_db()
    async with db.execute(
        """SELECT es.episode_index, a.grade, a.tags, a.reason
           FROM episode_serials es
           JOIN annotations a ON a.serial_number = es.serial_number
           WHERE es.dataset_id = ?""",
        (dataset_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return {
        row[0]: {
            "grade": row[1],
            "tags": _json.loads(row[2]) if row[2] else [],
            "reason": row[3],
        }
        for row in rows
    }


async def _load_annotations_by_serials(serials: list[str]) -> dict[str, dict]:
    if not serials:
        return {}

    db = await get_db()
    result: dict[str, dict] = {}
    unique_serials = sorted(set(serials))
    for i in range(0, len(unique_serials), 500):
        chunk = unique_serials[i:i + 500]
        placeholders = ",".join("?" * len(chunk))
        async with db.execute(
            f"SELECT serial_number, grade, tags, reason FROM annotations WHERE serial_number IN ({placeholders})",
            tuple(chunk),
        ) as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            result[row[0]] = {
                "grade": row[1],
                "tags": _json.loads(row[2]) if row[2] else [],
                "reason": row[3],
            }
    return result


async def _save_annotation_to_db(
    dataset_id: int,
    episode_index: int,
    grade: str | None,
    tags: list[str],
    reason: str | None,
    ctx: DatasetContext | Any,
) -> None:
    db = await get_db()
    serial = await _get_episode_serial(dataset_id, episode_index, ctx)
    if serial is None:
        raise ValueError(
            f"no serial_number for dataset_id={dataset_id} episode={episode_index}; "
            "cannot persist annotation"
        )
    await db.execute(
        """INSERT INTO annotations (serial_number, grade, tags, reason, updated_at)
           VALUES (?, ?, ?, ?, NOW())
           ON CONFLICT (serial_number) DO UPDATE SET
             grade=excluded.grade, tags=excluded.tags, reason=excluded.reason,
             updated_at=excluded.updated_at""",
        (serial, grade, _json.dumps(tags), reason),
    )
    await db.commit()


async def _refresh_dataset_stats(dataset_id: int) -> None:
    db = await get_db()

    # Get grade counts from annotations (joined via episode_serials so each
    # dataset only sees grades for its own recordings).
    async with db.execute(
        """SELECT
             COUNT(a.grade),
             SUM(CASE WHEN a.grade='good' THEN 1 ELSE 0 END),
             SUM(CASE WHEN a.grade='normal' THEN 1 ELSE 0 END),
             SUM(CASE WHEN a.grade='bad' THEN 1 ELSE 0 END)
           FROM episode_serials es
           LEFT JOIN annotations a ON a.serial_number = es.serial_number
           WHERE es.dataset_id = ?""",
        (dataset_id,),
    ) as cursor:
        row = await cursor.fetchone()
    graded_count = row[0] or 0
    good_count = row[1] or 0
    normal_count = row[2] or 0
    bad_count = row[3] or 0

    # Compute durations from episode lengths + annotations
    total_dur = 0.0
    good_dur = 0.0
    normal_dur = 0.0
    bad_dur = 0.0

    async with db.execute("SELECT path, fps FROM datasets WHERE id = ?", (dataset_id,)) as cursor:
        ds_row = await cursor.fetchone()
    if ds_row:
        ds_path = Path(ds_row[0])
        fps = ds_row[1] or 0
        if fps > 0:
            annotations = await _load_annotations_from_db(dataset_id)
            if annotations:
                from glob import glob as _glob
                episode_lengths: dict[int, int] = {}
                parquet_files = sorted(_glob(str(ds_path / "meta" / "episodes" / "chunk-*" / "file-*.parquet")))
                for f in parquet_files:
                    table = await asyncio.to_thread(pq.read_table, f, columns=["episode_index", "length"])
                    indices = table.column("episode_index").to_pylist()
                    lengths = table.column("length").to_pylist()
                    for idx, length in zip(indices, lengths):
                        episode_lengths[idx] = length or 0

                for ep_idx, ann in annotations.items():
                    length = episode_lengths.get(ep_idx, 0)
                    total_dur += length / fps
                    grade = ann.get("grade")
                    if grade == "good":
                        good_dur += length / fps
                    elif grade == "normal":
                        normal_dur += length / fps
                    elif grade == "bad":
                        bad_dur += length / fps

    await db.execute(
        """INSERT INTO dataset_stats (
             dataset_id, graded_count, good_count, normal_count, bad_count,
             total_duration_sec, good_duration_sec, normal_duration_sec, bad_duration_sec,
             updated_at
           )
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NOW())
           ON CONFLICT (dataset_id) DO UPDATE SET
             graded_count=excluded.graded_count, good_count=excluded.good_count,
             normal_count=excluded.normal_count, bad_count=excluded.bad_count,
             total_duration_sec=excluded.total_duration_sec,
             good_duration_sec=excluded.good_duration_sec,
             normal_duration_sec=excluded.normal_duration_sec,
             bad_duration_sec=excluded.bad_duration_sec,
             updated_at=excluded.updated_at""",
        (dataset_id, graded_count, good_count, normal_count, bad_count,
         total_dur, good_dur, normal_dur, bad_dur),
    )
    await db.commit()


class EpisodeService:
    """Reads episode base data from parquet, merges annotations from SQLite DB."""

    async def get_episodes(
        self,
        ctx: DatasetContext | Any,
    ) -> list[dict[str, Any]]:
        ds = ctx
        if ds.episodes_cache is not None:
            return list(ds.episodes_cache.values())

        episodes: dict[int, dict[str, Any]] = {}
        tasks_map = await ds.get_tasks_map()

        dataset_id = await _ensure_dataset_registered(ds.dataset_path)
        await _ensure_migrated(dataset_id, ds.dataset_path)
        annotations = await _load_annotations_from_db(dataset_id)
        serial_annotations: dict[str, dict] | None = None

        for file_path in ds.iter_episode_parquet_files():
            table = await asyncio.to_thread(pq.read_table, file_path)
            for row in _iter_rows(table):
                ep = _row_to_episode(row, tasks_map)
                # Merge DB annotations
                ann = annotations.get(ep["episode_index"])
                if ann is None and row.get("Serial_number") not in (None, ""):
                    if serial_annotations is None:
                        serials = [
                            str(r.get("Serial_number"))
                            for fp in ds.iter_episode_parquet_files()
                            for r in _iter_rows(await asyncio.to_thread(pq.read_table, fp))
                            if r.get("Serial_number") not in (None, "")
                        ]
                        serial_annotations = await _load_annotations_by_serials(serials)
                    ann = serial_annotations.get(str(row.get("Serial_number")))
                if ann:
                    ep["grade"] = ann.get("grade")
                    ep["tags"] = ann.get("tags", [])
                    ep["reason"] = ann.get("reason")
                episodes[ep["episode_index"]] = ep

        ds.episodes_cache = episodes
        return list(episodes.values())

    async def get_episode(
        self,
        ctx: DatasetContext | Any,
        episode_index: int,
    ) -> dict[str, Any]:
        ds = ctx
        if ds.episodes_cache is not None:
            try:
                return ds.episodes_cache[episode_index]
            except KeyError:
                raise EpisodeNotFoundError(
                    f"Episode {episode_index} not found in cache."
                )

        tasks_map = await ds.get_tasks_map()
        file_path = ds.get_file_for_episode(episode_index)
        if file_path is None:
            raise EpisodeNotFoundError(
                f"Episode {episode_index} not found in any parquet file."
            )

        dataset_id = await _ensure_dataset_registered(ds.dataset_path)
        await _ensure_migrated(dataset_id, ds.dataset_path)
        annotations = await _load_annotations_from_db(dataset_id)

        table = await asyncio.to_thread(pq.read_table, file_path)

        for row in _iter_rows(table):
            if row.get("episode_index") == episode_index:
                ep = _row_to_episode(row, tasks_map)
                ann = annotations.get(episode_index)
                if ann is None and row.get("Serial_number") not in (None, ""):
                    serial_annotations = await _load_annotations_by_serials(
                        [str(row.get("Serial_number"))],
                    )
                    ann = serial_annotations.get(str(row.get("Serial_number")))
                if ann:
                    ep["grade"] = ann.get("grade")
                    ep["tags"] = ann.get("tags", [])
                    ep["reason"] = ann.get("reason")
                return ep

        raise EpisodeNotFoundError(
            f"Episode {episode_index} not found in {file_path}."
        )

    async def update_episode(
        self,
        ctx: DatasetContext | Any,
        episode_index: int,
        grade: str | None = None,
        tags: list[str] | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Persist grade, tags, and reason to the SQLite DB."""
        if tags is None:
            raise TypeError("update_episode requires tags")

        ds = ctx
        if ds.episodes_cache is not None:
            if episode_index not in ds.episodes_cache:
                raise EpisodeNotFoundError(f"Episode {episode_index} not found.")
        else:
            file_path = ds.get_file_for_episode(episode_index)
            if file_path is None:
                raise EpisodeNotFoundError(f"Episode {episode_index} not found.")

        # Reason is meaningless without bad/normal grade; null it out for good or unset.
        effective_reason = reason if grade in ("bad", "normal") else None

        dataset_id = await _ensure_dataset_registered(ds.dataset_path)
        await _ensure_migrated(dataset_id, ds.dataset_path)
        await _save_annotation_to_db(dataset_id, episode_index, grade, tags, effective_reason, ds)
        await _refresh_dataset_stats(dataset_id)

        # Parquet write does NOT include reason — by design.
        await _write_annotations_to_parquet({episode_index: (grade, tags)}, ds)

        ds.distribution_cache.clear()

        if ds.episodes_cache is not None:
            ep = ds.episodes_cache.get(episode_index)
            if ep:
                ep["grade"] = grade
                ep["tags"] = tags
                ep["reason"] = effective_reason
                return ep

        return await self.get_episode(ds, episode_index)

    async def bulk_grade(
        self,
        ctx: DatasetContext | Any,
        episode_indices: list[int],
        grade: str,
        reason: str | None = None,
    ) -> int:
        """Set grade and reason for multiple episodes at once. Returns count updated."""
        ds = ctx
        dataset_id = await _ensure_dataset_registered(ds.dataset_path)
        await _ensure_migrated(dataset_id, ds.dataset_path)

        effective_reason = reason if grade in ("bad", "normal") else None

        existing_annotations = await _load_annotations_from_db(dataset_id)
        parquet_updates: dict[int, tuple[str | None, list[str]]] = {}
        for idx in episode_indices:
            existing = existing_annotations.get(idx, {})
            tags = existing.get("tags", [])
            await _save_annotation_to_db(dataset_id, idx, grade, tags, effective_reason, ds)
            parquet_updates[idx] = (grade, tags)
        await _refresh_dataset_stats(dataset_id)

        await _write_annotations_to_parquet(parquet_updates, ds)

        ds.distribution_cache.clear()

        if ds.episodes_cache is not None:
            for idx in episode_indices:
                ep = ds.episodes_cache.get(idx)
                if ep:
                    ep["grade"] = grade
                    ep["reason"] = effective_reason

        return len(episode_indices)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _iter_rows(table: pa.Table):
    """Yield each row of *table* as a plain Python dict."""
    col_names = table.schema.names
    for batch in table.to_batches():
        col_arrays = {name: batch.column(name).to_pylist() for name in col_names}
        n = batch.num_rows
        for i in range(n):
            yield {name: col_arrays[name][i] for name in col_names}


def _parse_created_at(serial_number: Any) -> str | None:
    """Extract YYYY-MM-DD date from serial_number like '20260115_...'."""
    if serial_number is None:
        return None
    import re
    s = str(serial_number).strip().replace("-", "").replace("_", " ")
    m = re.match(r"^(\d{4})(\d{2})(\d{2})", s)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def _row_to_episode(
    row: dict[str, Any],
    tasks_map: dict[int, str],
) -> dict[str, Any]:
    """Convert a raw parquet row dict into an Episode-compatible dict."""
    task_index: int = row.get("task_index", 0)
    task_instruction: str = tasks_map.get(task_index, "")

    raw_tags = row.get("tags")
    tags: list[str] = raw_tags if isinstance(raw_tags, list) else []

    return Episode(
        episode_index=row["episode_index"],
        length=int(row.get("dataset_to_index", 0)) - int(row.get("dataset_from_index", 0)),
        task_index=task_index,
        task_instruction=task_instruction,
        chunk_index=int(row.get("data/chunk_index", 0)),
        file_index=int(row.get("data/file_index", 0)),
        dataset_from_index=int(row.get("dataset_from_index", 0)),
        dataset_to_index=int(row.get("dataset_to_index", 0)),
        grade=row.get("grade"),
        tags=tags,
        created_at=_parse_created_at(row.get("Serial_number")),
    ).model_dump()


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

episode_service = EpisodeService()
