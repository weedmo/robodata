"""Service for computing column distributions from episode parquet files.

Uses pyarrow column projection to read only the selected field,
keeping memory usage low even for large datasets.
"""

from __future__ import annotations

import logging
from glob import glob
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from backend.datasets.schemas import DistributionBin, DistributionResponse, FieldInfo

logger = logging.getLogger(__name__)

SYSTEM_COLUMNS = {
    "episode_index", "length", "task_index", "chunk_index", "file_index",
    "dataset_from_index", "dataset_to_index", "task_instruction",
}

# Only these parquet columns are useful for distribution analysis
ALLOWED_PARQUET_COLUMNS = {
    "length", "task_instruction",
}

# Virtual fields computed from parquet + sidecar
VIRTUAL_FIELDS = {"grade", "tags", "collection_date"}


def get_available_fields(dataset_path: str) -> list[FieldInfo]:
    """Return all columns available in episode parquet files."""
    root = Path(dataset_path)
    parquet_files = sorted(glob(str(root / "meta" / "episodes" / "chunk-*" / "file-*.parquet")))
    if not parquet_files:
        return []

    schema = pq.read_schema(parquet_files[0])
    fields: list[FieldInfo] = []
    for i in range(len(schema)):
        field = schema.field(i)
        if field.name not in ALLOWED_PARQUET_COLUMNS:
            continue
        dtype = _arrow_type_to_str(field.type)
        fields.append(FieldInfo(
            name=field.name,
            dtype=dtype,
            is_system=field.name in SYSTEM_COLUMNS,
        ))
    fields.append(FieldInfo(name="grade", dtype="string", is_system=False))
    fields.append(FieldInfo(name="tags", dtype="list[string]", is_system=False))
    # Add collection_date if Serial_number column exists (capital S to match
    # what rosbag2lerobot-svt writes; matches cell002 and newer cells).
    if any(schema.field(i).name == "Serial_number" for i in range(len(schema))):
        fields.append(FieldInfo(name="collection_date", dtype="date", is_system=False))
    return fields


def compute_distribution(
    dataset_path: str,
    field: str,
    chart_type: str = "auto",
) -> DistributionResponse:
    """Compute value distribution for a single column using column projection.

    Results are cached in dataset_service.distribution_cache and returned
    instantly on subsequent calls until the dataset is reloaded or the
    cache is invalidated (e.g. after a grade/tag update).
    """
    from backend.datasets.services.dataset_service import dataset_service

    cache_key = f"{field}:{chart_type}"
    cached = dataset_service.distribution_cache.get(cache_key)
    if cached is not None:
        return cached

    if field in ("grade", "tags"):
        result = _compute_annotation_distribution(dataset_path, field, chart_type)
    elif field == "collection_date":
        result = _compute_collection_date_distribution(dataset_path)
    else:
        result = _compute_parquet_distribution(dataset_path, field, chart_type)

    dataset_service.distribution_cache[cache_key] = result
    return result


def _compute_parquet_distribution(
    dataset_path: str,
    field: str,
    chart_type: str = "auto",
) -> DistributionResponse:
    """Compute distribution from parquet column data."""

    root = Path(dataset_path)
    parquet_files = sorted(glob(str(root / "meta" / "episodes" / "chunk-*" / "file-*.parquet")))
    if not parquet_files:
        raise ValueError(f"No episode parquet files found in {root}")

    tables: list[pa.Table] = []
    for f in parquet_files:
        schema = pq.read_schema(f)
        if field not in schema.names:
            raise ValueError(f"Field '{field}' not found in parquet schema")
        table = pq.read_table(f, columns=[field])
        tables.append(table)

    combined = pa.concat_tables(tables, promote_options="default")
    column = combined.column(field)
    total = len(column)
    dtype = _arrow_type_to_str(column.type)

    if chart_type == "auto":
        chart_type = "histogram" if _is_numeric(column.type) else "bar"

    if chart_type == "histogram":
        bins = _histogram_bins(column)
    else:
        bins = _categorical_bins(column)

    return DistributionResponse(
        field=field,
        dtype=dtype,
        chart_type=chart_type,
        bins=bins,
        total=total,
    )


def _compute_collection_date_distribution(dataset_path: str) -> DistributionResponse:
    """Parse Serial_number column to extract YYYYMMDD date and count episodes per date."""
    import re

    root = Path(dataset_path)
    parquet_files = sorted(glob(str(root / "meta" / "episodes" / "chunk-*" / "file-*.parquet")))
    if not parquet_files:
        raise ValueError(f"No episode parquet files found in {root}")

    date_counts: dict[str, int] = {}
    total = 0
    date_pattern = re.compile(r"^(\d{4})(\d{2})(\d{2})")

    for f in parquet_files:
        schema = pq.read_schema(f)
        if "Serial_number" not in schema.names:
            continue
        table = pq.read_table(f, columns=["Serial_number"])
        for val in table.column("Serial_number").to_pylist():
            total += 1
            if val is None:
                date_counts["(unknown)"] = date_counts.get("(unknown)", 0) + 1
                continue
            s = str(val).strip().replace("-", "").replace("_", "")
            m = date_pattern.match(s)
            if m:
                date_label = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
                date_counts[date_label] = date_counts.get(date_label, 0) + 1
            else:
                date_counts["(unknown)"] = date_counts.get("(unknown)", 0) + 1

    bins = [
        DistributionBin(label=k, count=v)
        for k, v in sorted(date_counts.items())
    ]
    return DistributionResponse(
        field="collection_date",
        dtype="date",
        chart_type="bar",
        bins=bins,
        total=total,
    )


def _is_numeric(arrow_type: pa.DataType) -> bool:
    return pa.types.is_integer(arrow_type) or pa.types.is_floating(arrow_type)


def _histogram_bins(column: pa.ChunkedArray, num_bins: int = 20) -> list[DistributionBin]:
    arr = column.to_pylist()
    valid = [v for v in arr if v is not None]
    if not valid:
        return []

    min_val = min(valid)
    max_val = max(valid)

    if min_val == max_val:
        return [DistributionBin(label=str(min_val), count=len(valid))]

    bin_width = (max_val - min_val) / num_bins
    bins: list[DistributionBin] = []
    for i in range(num_bins):
        lo = min_val + i * bin_width
        hi = lo + bin_width
        count = sum(1 for v in valid if lo <= v < hi) if i < num_bins - 1 \
            else sum(1 for v in valid if lo <= v <= hi)
        if count > 0:
            label = f"{lo:.1f}-{hi:.1f}" if isinstance(lo, float) else f"{int(lo)}-{int(hi)}"
            bins.append(DistributionBin(label=label, count=count))
    return bins


def _categorical_bins(column: pa.ChunkedArray) -> list[DistributionBin]:
    arr = column.to_pylist()
    counts: dict[str, int] = {}
    for v in arr:
        key = str(v) if v is not None else "(null)"
        counts[key] = counts.get(key, 0) + 1

    return [
        DistributionBin(label=k, count=v)
        for k, v in sorted(counts.items(), key=lambda x: -x[1])
    ]


def _compute_annotation_distribution(
    dataset_path: str,
    field: str,
    chart_type: str,
) -> DistributionResponse:
    """Compute distribution for annotation fields (grade, tags).

    Reads from BOTH parquet files (original data) and the SQLite DB
    (user annotations), with DB taking priority when both exist.
    """
    import asyncio
    import concurrent.futures

    from backend.datasets.services.episode_service import (
        _ensure_dataset_registered, _ensure_migrated, _load_annotations_from_db,
    )

    dataset_path_obj = Path(dataset_path)

    def _fetch_annotations():
        async def _inner():
            ds_id = await _ensure_dataset_registered(dataset_path_obj)
            await _ensure_migrated(ds_id, dataset_path_obj)
            return ds_id, await _load_annotations_from_db(ds_id)
        return asyncio.run(_inner())

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        dataset_id, db_annotations = pool.submit(_fetch_annotations).result()

    # Read base grade/tags from parquet, then overlay DB annotations
    root = Path(dataset_path)
    parquet_files = sorted(glob(str(root / "meta" / "episodes" / "chunk-*" / "file-*.parquet")))

    episodes: dict[int, dict] = {}
    for f in parquet_files:
        cols = ["episode_index"]
        schema = pq.read_schema(f)
        if "grade" in schema.names:
            cols.append("grade")
        if "tags" in schema.names:
            cols.append("tags")
        table = pq.read_table(f, columns=cols)
        for batch in table.to_batches():
            col_arrays = {name: batch.column(name).to_pylist() for name in batch.schema.names}
            for i in range(batch.num_rows):
                ep_idx = col_arrays["episode_index"][i]
                grade = col_arrays.get("grade", [None] * batch.num_rows)[i]
                raw_tags = col_arrays.get("tags", [None] * batch.num_rows)[i]
                tags = raw_tags if isinstance(raw_tags, list) else []
                episodes[ep_idx] = {"grade": grade, "tags": tags}

    # Overlay DB annotations (replaces sidecar overlay)
    for ep_idx, ann in db_annotations.items():
        if ep_idx in episodes:
            if ann.get("grade") is not None:
                episodes[ep_idx]["grade"] = ann["grade"]
            if ann.get("tags") is not None:
                episodes[ep_idx]["tags"] = ann["tags"]
        else:
            episodes[ep_idx] = {
                "grade": ann.get("grade"),
                "tags": ann.get("tags", []),
            }

    total_episodes = len(episodes)

    if field == "grade":
        counts: dict[str, int] = {}
        for ep in episodes.values():
            grade = ep.get("grade")
            if grade:
                normalized = grade.strip().lower()
                counts[normalized] = counts.get(normalized, 0) + 1
            else:
                counts["(ungraded)"] = counts.get("(ungraded)", 0) + 1

        bins = [
            DistributionBin(label=k, count=v)
            for k, v in sorted(counts.items(), key=lambda x: -x[1])
        ]
        return DistributionResponse(
            field="grade",
            dtype="string",
            chart_type="bar",
            bins=bins,
            total=total_episodes,
        )

    elif field == "tags":
        tag_counts: dict[str, int] = {}
        tagged_episodes = 0
        for ep in episodes.values():
            tags = ep.get("tags", [])
            if tags:
                tagged_episodes += 1
                for tag in tags:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1

        untagged = total_episodes - tagged_episodes
        if untagged > 0:
            tag_counts["(no tags)"] = untagged

        bins = [
            DistributionBin(label=k, count=v)
            for k, v in sorted(tag_counts.items(), key=lambda x: -x[1])
        ]
        return DistributionResponse(
            field="tags",
            dtype="list[string]",
            chart_type="bar",
            bins=bins,
            total=total_episodes,
        )

    raise ValueError(f"Unknown annotation field: {field}")


def _arrow_type_to_str(t: pa.DataType) -> str:
    if pa.types.is_int64(t):
        return "int64"
    if pa.types.is_int32(t):
        return "int32"
    if pa.types.is_float64(t):
        return "float64"
    if pa.types.is_float32(t):
        return "float32"
    if pa.types.is_boolean(t):
        return "bool"
    if pa.types.is_string(t) or pa.types.is_large_string(t):
        return "string"
    return str(t)
