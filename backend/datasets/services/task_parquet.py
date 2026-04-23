"""Helpers for reading and updating task text across supported parquet schemas."""

from __future__ import annotations

import json
from collections.abc import Sequence

import pyarrow as pa


TASK_TEXT_COLUMNS: tuple[str, ...] = ("task", "__index_level_0__")


def get_task_text_column_name(table_or_schema_or_columns: pa.Table | pa.Schema | Sequence[str]) -> str | None:
    column_names = _column_names(table_or_schema_or_columns)
    for column_name in TASK_TEXT_COLUMNS:
        if column_name in column_names:
            return column_name

    schema = _schema(table_or_schema_or_columns)
    if schema is not None:
        pandas_index_columns = _pandas_index_columns(schema)
        for column_name in pandas_index_columns:
            if column_name in column_names and column_name != "task_index":
                return column_name

        string_candidates = [
            field.name
            for field in schema
            if field.name != "task_index" and _is_string_field(field.type)
        ]
        if len(string_candidates) == 1:
            return string_candidates[0]

    return None


def has_required_task_columns(table_or_schema_or_columns: pa.Table | pa.Schema | Sequence[str]) -> bool:
    column_names = _column_names(table_or_schema_or_columns)
    return "task_index" in column_names and get_task_text_column_name(table_or_schema_or_columns) is not None


def normalize_task_records(
    records: list[dict],
    table_or_schema_or_columns: pa.Table | pa.Schema | Sequence[str],
) -> list[dict]:
    task_text_column = get_task_text_column_name(table_or_schema_or_columns)
    if task_text_column is None or task_text_column == "task":
        return records

    normalized_records: list[dict] = []
    for record in records:
        normalized_record = dict(record)
        normalized_record["task"] = normalized_record.pop(task_text_column, "")
        normalized_records.append(normalized_record)
    return normalized_records


def _column_names(table_or_schema_or_columns: pa.Table | pa.Schema | Sequence[str]) -> Sequence[str]:
    if isinstance(table_or_schema_or_columns, pa.Table):
        return table_or_schema_or_columns.column_names
    if isinstance(table_or_schema_or_columns, pa.Schema):
        return table_or_schema_or_columns.names
    return table_or_schema_or_columns


def _schema(table_or_schema_or_columns: pa.Table | pa.Schema | Sequence[str]) -> pa.Schema | None:
    if isinstance(table_or_schema_or_columns, pa.Table):
        return table_or_schema_or_columns.schema
    if isinstance(table_or_schema_or_columns, pa.Schema):
        return table_or_schema_or_columns
    return None


def _pandas_index_columns(schema: pa.Schema) -> list[str]:
    metadata = schema.metadata or {}
    raw_pandas = metadata.get(b"pandas")
    if raw_pandas is None:
        return []

    try:
        payload = json.loads(raw_pandas.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []

    columns: list[str] = []
    for item in payload.get("index_columns", []):
        if isinstance(item, str):
            columns.append(item)
    return columns


def _is_string_field(data_type: pa.DataType) -> bool:
    return pa.types.is_string(data_type) or pa.types.is_large_string(data_type)
