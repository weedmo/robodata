"""Endpoint to return per-frame scalar data (observations, actions) for charts."""

import asyncio
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
from fastapi import APIRouter, HTTPException, Query

from backend.datasets.services.episode_rows import resolve_episode_rows
from backend.datasets.services.dataset_registry import dataset_registry

router = APIRouter(prefix="/api/scalars", tags=["scalars"])


@router.get("/{episode_index}")
async def get_scalars(episode_index: int, dataset_path: str = Query(...)):
    """Return observation and action scalar arrays for an episode."""
    try:
        ctx = dataset_registry.get(dataset_path)
        loc = ctx.get_episode_file_location(episode_index)
    except (KeyError, RuntimeError) as e:
        raise HTTPException(status_code=404, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    root_path = Path(ctx.get_dataset_path())
    features = ctx.get_features()

    from_idx = loc["dataset_from_index"]
    to_idx = loc["dataset_to_index"]
    chunk_idx = loc["data_chunk_index"]
    file_idx = loc["data_file_index"]

    data_path = root_path / f"data/chunk-{chunk_idx:03d}/file-{file_idx:03d}.parquet"
    if not data_path.exists():
        raise HTTPException(status_code=404, detail=f"Data file not found: {data_path}")

    # Read only the schema to discover available columns without loading data
    schema = await asyncio.to_thread(pq.read_schema, data_path)
    all_columns = set(schema.names)

    # Classify columns using features metadata (before reading any data)
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

    # Find the first available terminal-frame flag column
    _TERMINAL_FLAG_COLS = ["is_terminal", "is_last"]
    flag_col = next((c for c in _TERMINAL_FLAG_COLS if c in all_columns), None)
    ts_col = "timestamp" if "timestamp" in all_columns else None

    if not needed_columns and not flag_col:
        return {
            "episode_index": episode_index,
            "num_frames": to_idx - from_idx,
            "observations": {},
            "actions": {},
            "terminal_frames": [],
            "terminal_timestamps": [],
        }

    extra_cols = [c for c in [flag_col, ts_col] if c]
    position_cols = [c for c in ("index", "frame_index") if c in all_columns]
    read_columns = list(dict.fromkeys(needed_columns + extra_cols + position_cols))

    # Read only the scalar columns we need, then slice to the episode's frame range
    table = await asyncio.to_thread(pq.read_table, data_path, columns=read_columns)
    df = table.to_pydict()
    row_positions = resolve_episode_rows(df, from_idx, to_idx, table.schema.names)
    df = {
        col: [values[pos] for pos in row_positions]
        for col, values in df.items()
    }

    # Extract 0-based frame indices within the episode where the terminal flag is True
    terminal_frames: list[int] = []
    if flag_col and flag_col in df:
        terminal_frames = [i for i, v in enumerate(df[flag_col]) if v]

    # Map terminal frames to their actual timestamps
    terminal_timestamps: list[float] = []
    if terminal_frames and ts_col and ts_col in df:
        timestamps = df[ts_col]
        terminal_timestamps = [float(timestamps[i]) for i in terminal_frames]

    def extract_series(columns: list[str]) -> dict[str, list[float]]:
        result: dict[str, list[float]] = {}
        for col in columns:
            values = df.get(col, [])
            series: list[float] = []
            for v in values:
                arr = np.asarray(v, dtype=float).ravel()
                if arr.size == 1:
                    series.append(float(arr[0]))
                elif arr.size > 1:
                    # Multi-dim: split into separate series per dimension
                    for dim in range(arr.size):
                        dim_key = f"{col}[{dim}]"
                        if dim_key not in result:
                            result[dim_key] = []
                        result[dim_key].append(float(arr[dim]))
                    continue
            if series:
                result[col] = series
        return result

    observations = extract_series(state_columns)
    actions = extract_series(action_columns)

    return {
        "episode_index": episode_index,
        "num_frames": to_idx - from_idx,
        "observations": observations,
        "actions": actions,
        "terminal_frames": terminal_frames,
        "terminal_timestamps": terminal_timestamps,
    }
