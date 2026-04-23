from __future__ import annotations


def resolve_episode_rows(
    df: dict[str, list],
    from_idx: int,
    to_idx: int,
    all_columns: list[str],
) -> list[int]:
    """Return file-local row positions for an episode slice.

    Split datasets can keep `dataset_from_index` / `dataset_to_index` as
    dataset-global offsets while each parquet file only stores a subset of rows.
    When an `index` column is present, use it to recover the file-local rows
    that belong to the requested global range.
    """
    if not all_columns:
        return []

    row_count = len(df.get(all_columns[0], []))
    if row_count == 0:
        return []

    if "index" in df:
        positions = [
            position
            for position, value in enumerate(df["index"])
            if value is not None and from_idx <= int(value) < to_idx
        ]
        if positions:
            if "frame_index" in df:
                positions.sort(key=lambda pos: int(df["frame_index"][pos]))
            return positions

    start = max(0, min(from_idx, row_count))
    stop = max(start, min(to_idx, row_count))
    return list(range(start, stop))
