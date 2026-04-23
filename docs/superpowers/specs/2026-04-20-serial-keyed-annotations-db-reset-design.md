# Serial-keyed annotations + DB reset — design

Date: 2026-04-20
Status: approved (brainstorming)

## Problem

The metadata SQLite DB (`~/.local/share/curation-tools/metadata.db`) has
diverged from the actual data on disk: after deleting and re-converting
lerobot datasets, rows in the `datasets` / `episode_annotations` tables
still reference paths that no longer exist or whose contents have been
regenerated from different rosbags. User-entered grades (good / normal /
bad) are now attached to the wrong episodes.

The current schema uses the dataset's filesystem path as the identity
key and `(dataset_id, episode_index)` as the annotation key. Both are
brittle: path reuse across re-conversions silently reattaches old grades
to new recordings.

## Goal

1. Back up and reinitialize the DB so the immediate inconsistency is
   cleared.
2. Make annotation identity robust to re-conversion by keying grades on
   each episode's `Serial_number` (a timestamp-based recording ID that
   the converter already writes into every `meta/episodes/*.parquet`).
3. Keep the sync cost low for normal browsing: a full parquet rescan
   fires only when `meta/info.json` changes.

## Non-goals

- Preserving annotations that already exist in the current DB. The user
  has agreed to wipe them. Sidecar-JSON migration is still supported for
  deployments whose legacy annotations live on disk outside the DB.
- Changing the converter (`rosbag2lerobot-svt`). `Serial_number` is
  already written during conversion; we only consume it here.
- Restoring from the backup programmatically. The reset script only
  copies; a human restores manually if needed.

## Data model (schema v4)

### Retained
`datasets` — same as v3 plus one column.

```sql
ALTER TABLE datasets ADD COLUMN info_json_mtime REAL;
```

`info_json_mtime` is the POSIX mtime of `<dataset>/meta/info.json` at
the time `episode_serials` was last rebuilt for that dataset. It drives
the lazy-sync decision.

### Replaced

```sql
DROP TABLE IF EXISTS episode_annotations;
```

### New

```sql
CREATE TABLE episode_serials (
    dataset_id      INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    episode_index   INTEGER NOT NULL,
    serial_number   TEXT NOT NULL,
    PRIMARY KEY (dataset_id, episode_index)
);
CREATE INDEX idx_episode_serials_serial ON episode_serials(serial_number);

CREATE TABLE annotations (
    serial_number   TEXT PRIMARY KEY,
    grade           TEXT CHECK(grade IN ('good','normal','bad')),
    tags            TEXT DEFAULT '[]',
    reason          TEXT,
    updated_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
```

`episode_serials` is a cache rebuilt from parquet; it cascades on
dataset removal. `annotations` is the source of truth for user-entered
grades and has no FK — it survives dataset re-registration so grades
follow recordings via their `serial_number`.

`dataset_stats` is unchanged (aggregation cache).

### Migration safety guard

The v4 migration is **destructive** (drops `episode_annotations`). To
prevent silent data loss on a deployment that pulls the code but skips
`scripts/reset_db.py`, `init_db()` aborts with a clear error when
moving from v3 to v4 if `episode_annotations` still holds any rows:

```python
if version < 4:
    async with db.execute("SELECT COUNT(*) FROM episode_annotations") as cur:
        n = (await cur.fetchone())[0]
    if n > 0:
        raise RuntimeError(
            "Schema v4 drops episode_annotations but found %d rows. "
            "Run `python -m scripts.reset_db` first (it backs up and wipes "
            "the DB). Existing grades are not automatically preserved; the "
            "intended flow is annotate-fresh after reset." % n
        )
    await db.executescript(SCHEMA_V4)
    await db.execute("PRAGMA user_version = 4")
    await db.commit()
```

After `reset_db` runs, the DB file is empty and v1 → v4 run against no
existing annotations, so the guard short-circuits. The guard runs only
on the v3 → v4 transition — brand-new installations skip it.

## Backup and reset

New script `scripts/reset_db.py`:

1. Resolve `settings.db_path` (or the default
   `~/.local/share/curation-tools/metadata.db`).
2. For each of `metadata.db`, `metadata.db-wal`, `metadata.db-shm` that
   exists: copy to `<name>.bak-<UTC-ISO8601>`; if the backup name is
   taken, append `.1`, `.2`, … until free.
3. Delete the three originals.
4. Call `asyncio.run(init_db())`; the sequence
   `v1 → v2 → v3 → v4` runs against the empty file and the final
   on-disk structure is v4.
5. Print the backup paths and the new DB path to stdout.

Safety:
- Refuses without `--yes` (interactive confirmation otherwise).
- `--dry-run` prints planned actions and exits.
- Logs a warning if the FastAPI server appears to be running (detected
  via default bind port); does not attempt to stop it.

Rollback: user stops the server, runs
`cp metadata.db.bak-<ts> metadata.db`, restarts the server. No script
for this path — manual by design.

## Lazy sync (mtime-gated)

Extended `_upsert_datasets_to_db(cell_name, datasets)` in
`backend/datasets/services/cell_service.py`:

```python
async def _upsert_datasets_to_db(cell_name, datasets):
    db = await get_db()
    live_paths = {ds.path for ds in datasets}

    # (a) remove datasets that disappeared from this cell
    placeholders = ",".join("?" * len(live_paths)) or "NULL"
    await db.execute(
        f"DELETE FROM datasets WHERE cell_name = ? AND path NOT IN ({placeholders})",
        (cell_name, *sorted(live_paths)),
    )

    # (b) per-dataset upsert + conditional serial rebuild
    for ds in datasets:
        info_mtime = (Path(ds.path) / "meta" / "info.json").stat().st_mtime
        cached_mtime = await _get_cached_info_mtime(db, ds.path)

        await _upsert_dataset_row(db, cell_name, ds, info_mtime)
        dataset_id = await _fetch_dataset_id(db, ds.path)

        if cached_mtime is None or cached_mtime != info_mtime:
            await _rebuild_episode_serials(db, dataset_id, Path(ds.path))

        await _upsert_dataset_stats(db, dataset_id, ds)

    await db.commit()
```

`_rebuild_episode_serials(db, dataset_id, dataset_dir)`:

- Reads every `meta/episodes/chunk-*/file-*.parquet`, projecting only
  `episode_index` and `Serial_number`.
- Rows with `Serial_number` absent, `None`, or empty are skipped with a
  warning log; the rest of the scan continues.
- Within one transaction: `DELETE FROM episode_serials WHERE dataset_id
  = ?` then `executemany(INSERT …)`. This cleans up stale episode_index
  rows when re-conversion reduces episode count.

`annotations` is never cascaded away — orphaned serials (no matching
`episode_serials` row) are harmless and reattach automatically when the
same recording reappears.

### Interaction with `_count_grades`
`_count_grades` already has a DB-first path (via `dataset_stats`) with
a parquet fallback. After the serial rebuild runs, subsequent calls
read the cached stats. Only the first rebuild during a given browsing
session pays the parquet cost.

## Annotation CRUD path

All reads and writes go through `serial_number`.

### Helper

```python
async def _get_serial(db, dataset_id: int, episode_index: int) -> str | None:
    async with db.execute(
        "SELECT serial_number FROM episode_serials WHERE dataset_id = ? AND episode_index = ?",
        (dataset_id, episode_index),
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row else None
```

### Read — `_load_annotations_from_db(dataset_id)`
```sql
SELECT es.episode_index, a.grade, a.tags, a.reason
FROM episode_serials es
LEFT JOIN annotations a ON a.serial_number = es.serial_number
WHERE es.dataset_id = ?
```
Return shape unchanged: `{episode_index: {grade, tags, reason}}`.

### Write — `_upsert_annotation(dataset_id, episode_index, grade, tags, reason)`
Resolve the serial; if unresolved, raise `ValueError`. Then:

```sql
INSERT INTO annotations (serial_number, grade, tags, reason, updated_at)
VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
ON CONFLICT(serial_number) DO UPDATE SET
  grade=excluded.grade, tags=excluded.tags, reason=excluded.reason,
  updated_at=excluded.updated_at
```

### Stats — `_refresh_dataset_stats(dataset_id)`
Same aggregation as today, but from
`episode_serials LEFT JOIN annotations`.

### Auto-grade (`auto_grade_service.py`)

- Replace the "already graded?" query with the same JOIN, filtering
  `a.grade IS NOT NULL`.
- Replace the `INSERT … ON CONFLICT` write with a serial-resolved UPSERT
  that keeps the existing invariant: **user-entered grades are not
  overwritten by auto.** Episodes whose serial cannot be resolved are
  skipped with a warning, not errored, so auto-grade remains best-effort.

### Sidecar JSON migration — `_ensure_migrated`

- Skip when the dataset already has at least one annotation reachable
  via its `episode_serials` rows.
- Otherwise, for each `(episode_index_str, ann)` in the sidecar, resolve
  the current serial for that episode and `INSERT OR IGNORE` into
  `annotations`. `OR IGNORE` means an existing annotation (possibly
  written earlier for the same recording via another dataset path)
  wins over the sidecar — prevents stale sidecars from clobbering
  fresh DB data.
- Episodes whose serial cannot be resolved are skipped with a warning.

### Call-order guarantee
Annotation operations require `episode_serials` to be populated first.
The two entry points:
- UI browsing path: `get_datasets_in_cell → _upsert_datasets_to_db`
  runs the lazy sync before any annotation query is issued.
- Dataset-service load path (used by auto-grade): add an explicit
  `_rebuild_episode_serials(dataset_id, dataset_path)` hook at load
  time so auto-grade never runs against an empty mapping.

## Testing

### Existing tests to update
- `tests/test_db.py` — add a v3→v4 migration assertion.
- `tests/test_episode_annotations_db.py` — rewrite against the new
  JOIN-based helpers and serial fixtures.
- `tests/test_auto_grade_service.py` — rebuild fixtures on
  `episode_serials + annotations`.
- `tests/test_grade_reason.py` — column location moves to
  `annotations.reason`.
- `tests/test_mockup.py` — verify sidecar interface still loads.

### New tests
- `tests/test_episode_serials_sync.py` — rebuild behavior,
  stale-index cleanup, missing `Serial_number` handling.
- `tests/test_reconversion_scenario.py` — register dataset,
  annotate `S1=good`, delete `datasets` row, re-register with same
  serial, assert grade is inherited.
- `tests/test_lazy_sync_mtime.py` — no parquet read when mtime
  matches, rebuild triggered when mtime changes.
- `tests/test_sidecar_migration_v4.py` — sidecar JSON → `annotations`
  via serial resolution; `OR IGNORE` precedence.
- `tests/test_reset_db_script.py` — dry-run prints, live run produces
  backup files and a fresh v4 DB.

## Rollout

Sequential phases within one working session:

1. **Schema + backup infra**
   - `backend/core/db.py`: add `SCHEMA_V4` and version branch.
   - `scripts/reset_db.py`.
   - `tests/test_db.py` v4 migration test green.
2. **Sync logic**
   - `cell_service._upsert_datasets_to_db` + `_rebuild_episode_serials`.
   - `tests/test_episode_serials_sync.py` green.
3. **Annotation CRUD**
   - `episode_service` helpers and queries.
   - `auto_grade_service` two query updates.
   - Rewritten existing tests green.
4. **Integration + execution**
   - `tests/test_reconversion_scenario.py`, `test_lazy_sync_mtime.py`
     green.
   - Staging steps:
     a. Stop FastAPI server.
     b. `python -m scripts.reset_db --yes`; verify backup path printed.
     c. Restart server.
     d. Browse `cell002`; inspect `episode_serials` directly to
        confirm population.
     e. Grade an episode in the UI; inspect `annotations` for the
        expected row.
     f. (Optional) Delete and re-convert a dataset; verify grade
        inheritance.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| DB reset while server is live | Phase 4 explicitly stops the server first; script warns if the bind port looks busy. |
| Deployment pulls v4 code without running `reset_db` | `init_db()` refuses the v3 → v4 transition when `episode_annotations` has rows, pointing at the reset script. See "Migration safety guard". |
| Legacy dataset without `Serial_number` in parquet | Sync skips with a warning; annotation UI silently returns empty grade for those episodes. Users can re-convert to get the column. |
| Large datasets slow to rescan | Only two columns are projected; mtime gating means this runs only when `info.json` changes. If ever too slow, incremental scan is a follow-up. |
| Old sidecar clobbering newer DB data during migration | `_ensure_migrated` uses `INSERT OR IGNORE` and runs only when no existing annotation is reachable for that dataset. |
| Duplicate `Serial_number` across datasets | `episode_serials` composite PK allows it; `annotations.serial_number` is unique so the two datasets intentionally share a grade (same recording). |

## Commit layout

- `feat(db): add schema v4 with serial-keyed annotations`
- `feat(scripts): add reset_db backup-and-init script`
- `refactor(cell_service): populate episode_serials via lazy mtime-based sync`
- `refactor(episode_service): route annotation CRUD through serial_number`
- `refactor(auto_grade): use serial-keyed annotations`
- `test: cover reconversion scenario and lazy sync`
