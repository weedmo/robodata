"""One-shot data fix: assign unique Serial_number per episode for split datasets.

Background
----------
LeRobot v3.0 episode metadata parquet has one row per episode_index, plus a
`Serial_number` column identifying the source recording. When a single
recording is split into multiple episodes (cycle-level chunking), all derived
episodes share the same `Serial_number`. The annotation table is keyed by
`serial_number`, so grading one episode appears to spill over to its
siblings (same Serial_number → same annotation row).

This script rewrites duplicate Serial_numbers within a single dataset to be
unique by appending `_1`, `_2`, ... suffixes ordered by `episode_index` ASC.
It also rebuilds the `episode_serials` and `annotations` tables in Postgres
so that:

    * per-episode grades from parquet become per-serial annotations in DB
    * reasons stored in DB are reattached to the lowest-index episode whose
      parquet grade matches the DB grade (best-effort recovery)

The script is idempotent and supports a dry-run mode (default).

Usage
-----
    python scripts/fix_split_serial_numbers.py \
        --dataset /mnt/synology/.../dataset_path

    # then, after reviewing the plan:
    python scripts/fix_split_serial_numbers.py \
        --dataset /mnt/synology/.../dataset_path --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import asyncpg
import pyarrow as pa
import pyarrow.parquet as pq

DEFAULT_DB_URL = "postgresql://curation:dev-only-change-me@127.0.0.1:5433/curation"


def _episode_files(dataset_path: Path) -> list[Path]:
    pattern = "meta/episodes/chunk-*/file-*.parquet"
    return sorted(dataset_path.glob(pattern))


def _load_episodes(dataset_path: Path) -> list[dict[str, Any]]:
    """Read `episode_index`, `Serial_number`, `grade`, `tags` from every parquet row."""
    out: list[dict[str, Any]] = []
    for fp in _episode_files(dataset_path):
        schema = pq.read_schema(fp)
        cols = ["episode_index", "Serial_number"]
        if "grade" in schema.names:
            cols.append("grade")
        if "tags" in schema.names:
            cols.append("tags")
        table = pq.read_table(fp, columns=cols)
        rows = table.to_pylist()
        for row in rows:
            row["__file__"] = str(fp)
            out.append(row)
    return out


def _plan_new_serials(
    rows: list[dict[str, Any]],
) -> dict[int, str]:
    """Map episode_index -> new unique Serial_number with _N suffix."""
    groups: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        s = row.get("Serial_number")
        if s in (None, ""):
            continue
        groups[str(s)].append(int(row["episode_index"]))

    duplicate_serials = {serial for serial, episodes in groups.items() if len(episodes) > 1}
    used_serials = set(groups) - duplicate_serials
    new_serial: dict[int, str] = {}
    for old_serial, episodes in groups.items():
        episodes_sorted = sorted(episodes)
        # Always suffix when the group has duplicates; preserve original when unique.
        if len(episodes_sorted) == 1:
            new_serial[episodes_sorted[0]] = old_serial
            continue
        for pos, ep_idx in enumerate(episodes_sorted, start=1):
            suffix = pos
            candidate = f"{old_serial}_{suffix}"
            while candidate in used_serials:
                suffix += 1
                candidate = f"{old_serial}_{suffix}"
            new_serial[ep_idx] = candidate
            used_serials.add(candidate)
    return new_serial


def _summarise(rows: list[dict[str, Any]], new_serial: dict[int, str]) -> dict[str, int]:
    groups: dict[str, list[int]] = defaultdict(list)
    grade_count = 0
    for row in rows:
        s = row.get("Serial_number")
        if s not in (None, ""):
            groups[str(s)].append(int(row["episode_index"]))
        if row.get("grade") not in (None, ""):
            grade_count += 1
    dup_groups = {k: v for k, v in groups.items() if len(v) > 1}
    return {
        "rows": len(rows),
        "unique_old_serials": len(groups),
        "duplicate_groups": len(dup_groups),
        "duplicate_episodes": sum(len(v) for v in dup_groups.values()),
        "graded_rows_in_parquet": grade_count,
        "new_unique_serials": len(set(new_serial.values())),
    }


async def _fetch_db_state(conn: asyncpg.Connection, dataset_id: int) -> dict[str, Any]:
    serials_rows = await conn.fetch(
        "SELECT episode_index, serial_number FROM episode_serials WHERE dataset_id = $1",
        dataset_id,
    )
    annotations_rows = await conn.fetch(
        """SELECT a.serial_number, a.grade, a.tags, a.reason
           FROM episode_serials es
           JOIN annotations a ON a.serial_number = es.serial_number
           WHERE es.dataset_id = $1""",
        dataset_id,
    )
    return {
        "episode_serials": [(r["episode_index"], r["serial_number"]) for r in serials_rows],
        "annotations": [
            {
                "serial_number": r["serial_number"],
                "grade": r["grade"],
                "tags": json.loads(r["tags"]) if isinstance(r["tags"], str) else (r["tags"] or []),
                "reason": r["reason"],
            }
            for r in annotations_rows
        ],
    }


def _plan_db_migration(
    rows: list[dict[str, Any]],
    new_serial: dict[int, str],
    db_state: dict[str, Any],
) -> dict[str, Any]:
    """Compute new annotations + episode_serials state from parquet + old DB state.

    Strategy:
    - For every parquet episode with a non-null grade, an annotation will be
      written keyed by the new unique serial.
    - For every old DB annotation whose `grade` matches some parquet episode
      in the same group, attach the DB-stored `reason` to the lowest-index
      matching episode (deterministic, matches the previous dedup winner).
    - Plain `tags` come from parquet too (DB tags are equivalent for the
      grouping winner — parquet writes preserve them).
    """
    parquet_by_index: dict[int, dict[str, Any]] = {}
    for row in rows:
        ep = int(row["episode_index"])
        parquet_by_index[ep] = row

    # Group episodes by old serial.
    groups: dict[str, list[int]] = defaultdict(list)
    for ep, row in parquet_by_index.items():
        s = row.get("Serial_number")
        if s not in (None, ""):
            groups[str(s)].append(ep)
    for episodes in groups.values():
        episodes.sort()

    # Old DB annotation lookup keyed by old serial.
    old_ann_by_serial: dict[str, dict[str, Any]] = {
        a["serial_number"]: a for a in db_state["annotations"]
    }

    # Build per-new-serial annotation rows.
    new_annotations: dict[str, dict[str, Any]] = {}
    reason_attached_for_old_serial: set[str] = set()

    for old_serial, episodes in groups.items():
        old_ann = old_ann_by_serial.get(old_serial)
        for ep in episodes:
            row = parquet_by_index[ep]
            grade = row.get("grade") or None
            tags_val = row.get("tags")
            if isinstance(tags_val, list):
                tags = [str(t) for t in tags_val]
            elif tags_val in (None, ""):
                tags = []
            else:
                tags = json.loads(tags_val) if isinstance(tags_val, str) else []
            # Annotations only carry user-set grades. The parquet `tags`
            # column is populated with dataset-creation metadata (robot id,
            # camera serials, etc) for every episode, so it cannot be used
            # as a signal of user intent — only `grade` can.
            if grade is None:
                continue
            new_serial_str = new_serial[ep]
            reason: str | None = None
            if (
                old_ann is not None
                and old_ann.get("grade") == grade
                and old_serial not in reason_attached_for_old_serial
                and old_ann.get("reason")
            ):
                reason = old_ann["reason"]
                reason_attached_for_old_serial.add(old_serial)
            new_annotations[new_serial_str] = {
                "grade": grade,
                "tags": tags,
                "reason": reason,
            }

    # New episode_serials: every episode with a non-empty Serial_number.
    new_episode_serials: list[tuple[int, str]] = []
    for ep, row in sorted(parquet_by_index.items()):
        if row.get("Serial_number") in (None, ""):
            continue
        new_episode_serials.append((ep, new_serial[ep]))

    return {
        "new_annotations": new_annotations,
        "new_episode_serials": new_episode_serials,
        "old_annotation_count": len(db_state["annotations"]),
        "old_serial_count": len(db_state["episode_serials"]),
        "reasons_recovered": len(reason_attached_for_old_serial),
        "reasons_orphaned": sum(
            1 for a in db_state["annotations"]
            if a.get("reason") and a["serial_number"] not in reason_attached_for_old_serial
        ),
    }


def _backup_parquet_files(dataset_path: Path) -> Path:
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    backup_root = dataset_path / "meta" / f"episodes_backup_{ts}"
    src_root = dataset_path / "meta" / "episodes"
    shutil.copytree(src_root, backup_root)
    return backup_root


def _rewrite_parquet(
    dataset_path: Path,
    new_serial: dict[int, str],
) -> int:
    rewritten = 0
    for fp in _episode_files(dataset_path):
        table = pq.read_table(fp)
        if "Serial_number" not in table.schema.names:
            continue
        indices = table.column("episode_index").to_pylist()
        new_serials = [new_serial.get(int(i)) for i in indices]
        # Preserve original value (None/empty) when no mapping exists.
        old_vals = table.column("Serial_number").to_pylist()
        merged = [
            new_serials[i] if new_serials[i] is not None else old_vals[i]
            for i in range(len(indices))
        ]
        table = table.drop(["Serial_number"])
        table = table.append_column(
            "Serial_number", pa.array(merged, type=pa.string()),
        )
        pq.write_table(table, fp)
        rewritten += 1
    return rewritten


async def _apply_db(
    db_url: str,
    dataset_id: int,
    plan: dict[str, Any],
) -> None:
    conn = await asyncpg.connect(db_url)
    try:
        async with conn.transaction():
            # Drop all old annotations for this dataset.
            await conn.execute(
                """DELETE FROM annotations
                   WHERE serial_number IN (
                     SELECT serial_number FROM episode_serials WHERE dataset_id = $1
                   )""",
                dataset_id,
            )
            # Drop old episode_serials rows.
            await conn.execute(
                "DELETE FROM episode_serials WHERE dataset_id = $1", dataset_id,
            )
            # Insert new episode_serials.
            await conn.executemany(
                """INSERT INTO episode_serials (dataset_id, episode_index, serial_number)
                   VALUES ($1, $2, $3)""",
                [(dataset_id, ep, s) for ep, s in plan["new_episode_serials"]],
            )
            # Insert new annotations.
            ann_rows = [
                (
                    s,
                    a["grade"],
                    json.dumps(a["tags"]),
                    a["reason"],
                )
                for s, a in plan["new_annotations"].items()
            ]
            await conn.executemany(
                """INSERT INTO annotations (serial_number, grade, tags, reason)
                   VALUES ($1, $2, $3::jsonb, $4)""",
                ann_rows,
            )
    finally:
        await conn.close()


async def _refresh_dataset_stats(db_url: str, dataset_id: int) -> None:
    """Recompute dataset_stats counters after annotation rewrite."""
    conn = await asyncpg.connect(db_url)
    try:
        async with conn.transaction():
            row = await conn.fetchrow(
                """SELECT
                     COUNT(a.grade) AS graded,
                     SUM(CASE WHEN a.grade='good' THEN 1 ELSE 0 END) AS good_n,
                     SUM(CASE WHEN a.grade='normal' THEN 1 ELSE 0 END) AS normal_n,
                     SUM(CASE WHEN a.grade='bad' THEN 1 ELSE 0 END) AS bad_n
                   FROM episode_serials es
                   LEFT JOIN annotations a ON a.serial_number = es.serial_number
                   WHERE es.dataset_id = $1""",
                dataset_id,
            )
            await conn.execute(
                """INSERT INTO dataset_stats (
                     dataset_id, graded_count, good_count, normal_count, bad_count, updated_at
                   ) VALUES ($1, $2, $3, $4, $5, NOW())
                   ON CONFLICT (dataset_id) DO UPDATE SET
                     graded_count=excluded.graded_count,
                     good_count=excluded.good_count,
                     normal_count=excluded.normal_count,
                     bad_count=excluded.bad_count,
                     updated_at=excluded.updated_at""",
                dataset_id,
                int(row["graded"] or 0),
                int(row["good_n"] or 0),
                int(row["normal_n"] or 0),
                int(row["bad_n"] or 0),
            )
    finally:
        await conn.close()


async def _resolve_dataset_id(db_url: str, dataset_path: Path) -> int | None:
    conn = await asyncpg.connect(db_url)
    try:
        row = await conn.fetchrow(
            "SELECT id FROM datasets WHERE path = $1",
            str(dataset_path.resolve()),
        )
    finally:
        await conn.close()
    return row["id"] if row else None


async def _find_db_serial_collisions(
    db_url: str,
    dataset_id: int,
    replacement_serials: list[str],
) -> list[str]:
    """Find replacement serials already owned outside this dataset."""
    if not replacement_serials:
        return []
    conn = await asyncpg.connect(db_url)
    try:
        rows = await conn.fetch(
            """WITH replacement_serials(serial_number) AS (
                 SELECT DISTINCT unnest($2::text[])
               )
               SELECT serial_number
               FROM replacement_serials rs
               WHERE EXISTS (
                 SELECT 1
                 FROM episode_serials es
                 WHERE es.serial_number = rs.serial_number
                   AND es.dataset_id <> $1
               )
               OR (
                 EXISTS (
                   SELECT 1
                   FROM annotations a
                   WHERE a.serial_number = rs.serial_number
                 )
                 AND NOT EXISTS (
                   SELECT 1
                   FROM episode_serials es
                   WHERE es.serial_number = rs.serial_number
                     AND es.dataset_id = $1
                 )
               )
               ORDER BY serial_number""",
            dataset_id,
            replacement_serials,
        )
    finally:
        await conn.close()
    return [r["serial_number"] for r in rows]


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--apply", action="store_true",
                        help="Without this flag the script only prints the plan.")
    parser.add_argument("--no-backup", action="store_true",
                        help="Skip backing up parquet files (for re-runs).")
    args = parser.parse_args()

    dataset_path = args.dataset.resolve()
    if not dataset_path.is_dir():
        raise SystemExit(f"Dataset path does not exist: {dataset_path}")

    rows = _load_episodes(dataset_path)
    if not rows:
        raise SystemExit("No episode parquet rows found.")
    new_serial = _plan_new_serials(rows)
    summary = _summarise(rows, new_serial)
    print("=== Parquet summary ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")

    dataset_id = await _resolve_dataset_id(args.db_url, dataset_path)
    db_plan: dict[str, Any] | None = None
    if dataset_id is not None:
        conn = await asyncpg.connect(args.db_url)
        try:
            db_state = await _fetch_db_state(conn, dataset_id)
        finally:
            await conn.close()
        db_plan = _plan_db_migration(rows, new_serial, db_state)
        print(f"\n=== DB plan (dataset_id={dataset_id}) ===")
        print(f"  old episode_serials rows  : {db_plan['old_serial_count']}")
        print(f"  old annotations rows      : {db_plan['old_annotation_count']}")
        print(f"  new episode_serials rows  : {len(db_plan['new_episode_serials'])}")
        print(f"  new annotations rows      : {len(db_plan['new_annotations'])}")
        print(f"  reasons recovered         : {db_plan['reasons_recovered']}")
        print(f"  reasons orphaned (lost)   : {db_plan['reasons_orphaned']}")
        collisions = await _find_db_serial_collisions(
            args.db_url,
            dataset_id,
            [s for _, s in db_plan["new_episode_serials"]],
        )
        if collisions:
            print("\nReplacement serials already exist outside this dataset:")
            for serial in collisions[:20]:
                print(f"  {serial}")
            if len(collisions) > 20:
                print(f"  ... {len(collisions) - 20} more")
            if args.apply:
                raise SystemExit(
                    "Refusing to apply: replacement serials would collide with existing DB rows."
                )
    else:
        print(f"\nDataset not registered in DB at {args.db_url} — DB step will be skipped.")

    if not args.apply:
        print("\n[dry-run] re-run with --apply to execute.")
        return

    if not args.no_backup:
        backup = _backup_parquet_files(dataset_path)
        print(f"\nBacked up parquet meta to: {backup}")

    rewritten = _rewrite_parquet(dataset_path, new_serial)
    print(f"Rewrote {rewritten} parquet file(s) with unique Serial_numbers.")

    if dataset_id is not None and db_plan is not None:
        await _apply_db(args.db_url, dataset_id, db_plan)
        await _refresh_dataset_stats(args.db_url, dataset_id)
        print("DB updated: episode_serials + annotations + dataset_stats.")

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
