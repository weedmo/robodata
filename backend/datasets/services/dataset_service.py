"""Compatibility helpers for legacy parquet shape normalization.

The DatasetService singleton lived here historically. It has been replaced
by backend.datasets.services.dataset_registry.DatasetRegistry and
DatasetContext. Keep only stateless helpers that are shared by registry code.
"""

from __future__ import annotations

import pyarrow as pa


def _table_to_list_of_dicts(table: pa.Table) -> list[dict]:
    column_names = table.schema.names
    columns = [table.column(name).to_pylist() for name in column_names]
    return [
        dict(zip(column_names, row))
        for row in zip(*columns)
    ] if columns else []


def _normalize_compatible_string_widths(tables: list[pa.Table]) -> list[pa.Table]:
    compatible_types: dict[str, pa.DataType] = {}
    field_types: dict[str, list[pa.DataType]] = {}

    for table in tables:
        for field in table.schema:
            field_types.setdefault(field.name, []).append(field.type)

    for field_name, types in field_types.items():
        if _has_only_string_width_mismatch(types):
            compatible_types[field_name] = pa.large_string()

    if not compatible_types:
        return tables

    normalized_tables: list[pa.Table] = []
    for table in tables:
        normalized_table = table
        for index, field in enumerate(normalized_table.schema):
            target_type = compatible_types.get(field.name)
            if target_type is None or field.type.equals(target_type):
                continue
            normalized_table = normalized_table.set_column(
                index,
                pa.field(
                    field.name,
                    target_type,
                    nullable=field.nullable,
                    metadata=field.metadata,
                ),
                normalized_table.column(field.name).cast(target_type),
            )
        normalized_tables.append(normalized_table)

    return normalized_tables


def _has_only_string_width_mismatch(types: list[pa.DataType]) -> bool:
    if len(types) < 2:
        return False

    distinct_types: list[pa.DataType] = []
    for data_type in types:
        if any(existing.equals(data_type) for existing in distinct_types):
            continue
        distinct_types.append(data_type)

    return (
        len(distinct_types) > 1
        and all(
            data_type.equals(pa.string()) or data_type.equals(pa.large_string())
            for data_type in distinct_types
        )
    )
