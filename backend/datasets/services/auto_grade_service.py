"""Auto-grade service — runs once per dataset on first registration.

Detects severe divergence between paired observation.state[N] and action[N]
scalar columns and writes `grade='normal'` + a machine-written reason on
ungraded episodes. Idempotency guard is `datasets.auto_graded_at`.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Iterable, TypedDict

import pyarrow.parquet as pq

from backend.core.db import get_db

logger = logging.getLogger(__name__)


# Tuned against labelled good/normal/bad episodes — see spec for evidence.
MODERATE_RATIO = 0.15
SEVERE_RATIO = 0.30
MIN_SEVERE_RUN = 5


class Band(TypedDict):
    start: int
    end: int
    level: str  # 'moderate' | 'severe'


_IDX_RE = re.compile(r"\[(\d+)\]$")


def unify_key(key: str) -> str:
    """Reduce a scalar key to its pair-matching identifier.

    observation.state[0] <-> action[0]      -> '[0]'
    observation.state.joint1 <-> action.joint1 -> 'joint1'
    """
    m = _IDX_RE.search(key)
    if m:
        return m.group(0)
    return (
        key.replace("observation.state.", "")
        .replace("observation.state", "")
        .replace("observation.", "")
        .replace("action.", "")
        .replace("action", "")
    )


def _classify(ratio: float) -> str | None:
    if ratio > SEVERE_RATIO:
        return "severe"
    if ratio > MODERATE_RATIO:
        return "moderate"
    return None


def _range_of(series: list[float]) -> float:
    if not series:
        return 0.0
    lo = hi = series[0]
    for v in series[1:]:
        if v < lo:
            lo = v
        if v > hi:
            hi = v
    return hi - lo


def compute_bands(obs: list[float], act: list[float]) -> list[Band]:
    """Pairwise divergence bands for one joint. Returns merged runs."""
    n = min(len(obs), len(act))
    if n == 0:
        return []
    rng = max(_range_of(obs[:n]), _range_of(act[:n]))
    if rng == 0:
        return []

    # Per-frame level
    levels: list[str | None] = []
    for i in range(n):
        r = abs(act[i] - obs[i]) / rng
        levels.append(_classify(r))

    # Merge runs
    bands: list[Band] = []
    cur: str | None = None
    start = 0
    for i, lv in enumerate(levels):
        if lv != cur:
            if cur is not None:
                bands.append({"start": start, "end": i - 1, "level": cur})
            cur = lv
            start = i
    if cur is not None:
        bands.append({"start": start, "end": n - 1, "level": cur})

    # Downgrade short severe runs to moderate (gripper transients are NOT
    # data-quality problems; only sustained tracking error is).
    for b in bands:
        if b["level"] == "severe" and (b["end"] - b["start"] + 1) < MIN_SEVERE_RUN:
            b["level"] = "moderate"

    # Re-merge adjacent same-level runs after downgrade
    merged: list[Band] = []
    for b in bands:
        if merged and merged[-1]["level"] == b["level"] and merged[-1]["end"] + 1 == b["start"]:
            merged[-1] = {"start": merged[-1]["start"], "end": b["end"], "level": b["level"]}
        else:
            merged.append(dict(b))  # type: ignore[arg-type]
    return merged


# ---------------------------------------------------------------------------
# Per-episode severity summary
# ---------------------------------------------------------------------------


class JointSeverity(TypedDict):
    joint: str
    severe_ratio: float  # severe_frames / episode_length


def _episode_severity(
    observations: dict[str, list[float]],
    actions: dict[str, list[float]],
) -> list[JointSeverity]:
    """Return per-joint severe-frame ratios for joints with any severe band."""
    # Pair by unified key
    act_by_name: dict[str, list[float]] = {}
    for k, v in actions.items():
        act_by_name[unify_key(k)] = v

    out: list[JointSeverity] = []
    for k, obs in observations.items():
        name = unify_key(k)
        act = act_by_name.get(name)
        if act is None:
            continue
        bands = compute_bands(obs, act)
        severe_frames = 0
        for b in bands:
            if b["level"] == "severe":
                severe_frames += b["end"] - b["start"] + 1
        if severe_frames > 0:
            n = min(len(obs), len(act))
            if n > 0:
                out.append({"joint": name, "severe_ratio": severe_frames / n})
    # Sort descending by severe_ratio for stable reason string
    out.sort(key=lambda x: x["severe_ratio"], reverse=True)
    return out


def _format_reason(sev: list[JointSeverity], top: int = 3) -> str:
    """'[auto] severe divergence: [13] 33.3%, [5] 19.6%, [7] 6.4%'"""
    parts = [f"{s['joint']} {s['severe_ratio'] * 100:.1f}%" for s in sev[:top]]
    return "[auto] severe divergence: " + ", ".join(parts)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def _load_scalars_for_episode(
    dataset_path: Path,
    episode_row: dict,
    features: dict,
) -> tuple[dict[str, list[float]], dict[str, list[float]]] | None:
    """Return (observations, actions) dicts for one episode, or None on error.

    Mirrors `backend/datasets/routers/scalars.py` but skipped terminal-frame
    bookkeeping and filters to scalar (non-image, non-video) columns only.
    """
    import asyncio
    import numpy as np

    from_idx = episode_row["dataset_from_index"]
    to_idx = episode_row["dataset_to_index"]
    chunk_idx = episode_row["data/chunk_index"]
    file_idx = episode_row["data/file_index"]
    data_path = dataset_path / f"data/chunk-{chunk_idx:03d}/file-{file_idx:03d}.parquet"
    if not data_path.exists():
        return None

    try:
        schema = await asyncio.to_thread(pq.read_schema, data_path)
    except Exception as exc:
        logger.warning("auto_grade: schema read failed for %s: %s", data_path, exc)
        return None
    all_columns = set(schema.names)

    state_columns: list[str] = []
    action_columns: list[str] = []
    for col, feature in features.items():
        dtype = feature.get("dtype", "")
        if dtype in ("image", "video"):
            continue
        if col.startswith("observation.") and col in all_columns:
            state_columns.append(col)
        elif col.startswith("action") and col in all_columns:
            action_columns.append(col)
    needed_columns = state_columns + action_columns
    if not needed_columns:
        return {}, {}

    try:
        table = await asyncio.to_thread(pq.read_table, data_path, columns=needed_columns)
    except Exception as exc:
        logger.warning("auto_grade: data read failed for %s: %s", data_path, exc)
        return None
    table = table.slice(from_idx, to_idx - from_idx)
    df = table.to_pydict()

    def _extract(columns: list[str]) -> dict[str, list[float]]:
        result: dict[str, list[float]] = {}
        for col in columns:
            values = df.get(col, [])
            scalar_series: list[float] = []
            for v in values:
                arr = np.asarray(v, dtype=float).ravel()
                if arr.size == 1:
                    scalar_series.append(float(arr[0]))
                elif arr.size > 1:
                    for dim in range(arr.size):
                        key = f"{col}[{dim}]"
                        result.setdefault(key, []).append(float(arr[dim]))
                    continue
            if scalar_series:
                result[col] = scalar_series
        return result

    return _extract(state_columns), _extract(action_columns)


async def ensure_auto_graded(dataset_id: int, dataset_path: Path) -> None:
    """Run the auto-grade pass once per dataset. Safe to call repeatedly.

    Does nothing if `datasets.auto_graded_at` is already set. Writes
    `grade='normal'` + a machine reason on every ungraded episode with at
    least one severe divergence band, then stamps `auto_graded_at` with now().
    """
    db = await get_db()
    async with db.execute(
        "SELECT auto_graded_at FROM datasets WHERE id = ?",
        (dataset_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return
    if row[0] is not None:
        return

    # Auto-grade can run from load paths that bypass cell browsing. If
    # episode_serials is still empty, annotation writes would silently skip
    # and auto_graded_at would get stamped — permanently disabling the pass.
    # Rebuild from parquet now so every ep_idx has a serial to write against.
    async with db.execute(
        "SELECT COUNT(*) FROM episode_serials WHERE dataset_id = ?", (dataset_id,)
    ) as cursor:
        serial_count = (await cursor.fetchone())[0]
    if serial_count == 0:
        from backend.datasets.services.cell_service import _rebuild_episode_serials
        await _rebuild_episode_serials(db, dataset_id, dataset_path)
        await db.commit()

    from backend.datasets.services.dataset_service import dataset_service

    try:
        features = dataset_service.get_features()
    except RuntimeError:
        # dataset_service not loaded in this call path (e.g. update_episode
        # round-trip before a load, or tests that stub services). Skip — no
        # stamp, so a later load can still run the pass.
        logger.info("auto_grade: dataset not loaded; skipping for dataset_id=%s", dataset_id)
        return
    if not features:
        logger.info("auto_grade: no features loaded; skipping for dataset_id=%s", dataset_id)
        return  # Do NOT stamp — retry on next load when dataset is fully loaded.

    async with db.execute(
        """SELECT es.episode_index
           FROM episode_serials es
           JOIN annotations a ON a.serial_number = es.serial_number
           WHERE es.dataset_id = ? AND a.grade IS NOT NULL""",
        (dataset_id,),
    ) as cursor:
        graded_rows = await cursor.fetchall()
    already_graded: set[int] = {r[0] for r in graded_rows}

    import asyncio

    auto_updates: list[tuple[int, str]] = []  # (episode_index, reason)
    for file_path in dataset_service.iter_episode_parquet_files():
        try:
            ep_table = await asyncio.to_thread(pq.read_table, file_path)
        except Exception as exc:
            logger.warning("auto_grade: episode parquet read failed for %s: %s", file_path, exc)
            return  # Abort without stamping so we retry on next load.
        rows = ep_table.to_pylist()
        for row in rows:
            ep_idx = row.get("episode_index")
            if ep_idx is None or ep_idx in already_graded:
                continue
            scalars = await _load_scalars_for_episode(dataset_path, row, features)
            if scalars is None:
                continue
            observations, actions = scalars
            if not observations or not actions:
                continue
            sev = _episode_severity(observations, actions)
            if not sev:
                continue
            auto_updates.append((ep_idx, _format_reason(sev)))

    for ep_idx, reason in auto_updates:
        async with db.execute(
            "SELECT serial_number FROM episode_serials WHERE dataset_id = ? AND episode_index = ?",
            (dataset_id, ep_idx),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            logger.warning(
                "auto_grade: skip ep %d in dataset_id=%s (no serial_number)",
                ep_idx, dataset_id,
            )
            continue
        serial = row[0]
        await db.execute(
            """INSERT INTO annotations (serial_number, grade, tags, reason, updated_at)
               VALUES (?, 'normal', '[]', ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
               ON CONFLICT(serial_number) DO UPDATE SET
                   grade  = CASE WHEN annotations.grade IS NULL
                                THEN excluded.grade ELSE annotations.grade END,
                   reason = CASE WHEN annotations.grade IS NULL
                                THEN excluded.reason ELSE annotations.reason END,
                   updated_at = excluded.updated_at""",
            (serial, reason),
        )
    await db.execute(
        "UPDATE datasets SET auto_graded_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
        (dataset_id,),
    )
    await db.commit()

    if auto_updates:
        logger.info(
            "auto_grade: dataset_id=%s marked %d episodes as normal",
            dataset_id,
            len(auto_updates),
        )
    else:
        logger.info(
            "auto_grade: dataset_id=%s no severe episodes detected; stamped auto_graded_at",
            dataset_id,
        )

    try:
        from backend.datasets.services.episode_service import _refresh_dataset_stats
        await _refresh_dataset_stats(dataset_id)
    except Exception as exc:
        logger.warning("auto_grade: stats refresh failed: %s", exc)
    dataset_service.distribution_cache.pop("grade:auto", None)
    dataset_service.distribution_cache.pop("grade:bar", None)
