"""Service for scanning mount roots and discovering cell/dataset structure.

A "cell" is a subdirectory of an allowed_dataset_root whose name matches
the configured pattern (default: "cell*"). A dataset inside a cell is any
subdirectory that contains meta/info.json.
"""

from __future__ import annotations

import fnmatch
import json
import logging
from pathlib import Path

from backend.datasets.schemas import CellInfo, DatasetSourceInfo, DatasetSummary
from backend.datasets.services.raw_dataset_adapter import (
    is_raw_dataset_path,
    raw_annotation_counts_for_task,
    raw_dataset_summaries_for_cell,
)

logger = logging.getLogger(__name__)


def _find_dataset_roots(cell_dir: Path) -> list[tuple[Path, str]]:
    """Return dataset roots under a cell with names relative to the cell root.

    A dataset root is any directory containing meta/info.json. Once a dataset
    root is found, recursion stops for that subtree to avoid double-counting
    nested content inside an already-valid dataset.
    """
    dataset_roots: list[tuple[Path, str]] = []

    def _walk(current_dir: Path) -> None:
        try:
            children = sorted(current_dir.iterdir())
        except OSError as e:
            logger.warning("Cannot scan %s: %s", current_dir, e)
            return

        for child in children:
            if not child.is_dir():
                continue
            try:
                is_dataset_root = (child / "meta" / "info.json").exists()
            except OSError as e:
                logger.warning("Cannot inspect %s: %s", child, e)
                continue
            if is_dataset_root:
                dataset_roots.append((child, child.relative_to(cell_dir).as_posix()))
                continue
            _walk(child)

    _walk(cell_dir)
    return dataset_roots


def _count_raw_datasets(cell_dir: Path) -> int:
    return len(raw_dataset_summaries_for_cell(cell_dir))


def scan_cells(roots: list[str], pattern: str = "cell*") -> list[CellInfo]:
    """Scan all roots for cell directories matching pattern.

    Args:
        roots: List of mount root paths (from allowed_dataset_roots).
        pattern: Glob pattern for cell directory names (default "cell*").

    Returns:
        List of CellInfo sorted by (root, name).
    """
    cells: list[CellInfo] = []
    for root_str in roots:
        root = Path(root_str)
        if not root.exists() or not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            if not fnmatch.fnmatch(child.name, pattern):
                continue
            dataset_count = _count_raw_datasets(child) if is_raw_dataset_path(child) else _count_datasets(child)
            cells.append(CellInfo(
                name=child.name,
                path=str(child.resolve()),
                mount_root=str(root.resolve()),
                dataset_count=dataset_count,
                active=True,
            ))
    return cells


def list_dataset_sources(base_root: str, source_names: list[str], pattern: str = "cell*") -> list[DatasetSourceInfo]:
    """Return configured source roots under a shared base path."""
    base = Path(base_root)
    sources: list[DatasetSourceInfo] = []
    for source_name in source_names:
        source_path = (base / source_name).resolve()
        active = source_path.exists() and source_path.is_dir()
        cell_count = len(scan_cells([str(source_path)], pattern=pattern)) if active else 0
        sources.append(
            DatasetSourceInfo(
                name=source_name,
                path=str(source_path),
                cell_count=cell_count,
                active=active,
            )
        )
    return sources


async def get_datasets_in_cell(cell_path: str) -> list[DatasetSummary]:
    """Return all datasets inside a cell directory.

    A dataset is a subdirectory containing meta/info.json.
    graded_count is 0 for now — computed from sidecar in a future step.
    """
    root = Path(cell_path)
    if not root.exists() or not root.is_dir():
        return []

    if is_raw_dataset_path(root):
        raw_summaries = []
        for item in raw_dataset_summaries_for_cell(root):
            item.update(await raw_annotation_counts_for_task(item["path"]))
            raw_summaries.append(DatasetSummary(**item))
        return raw_summaries

    datasets: list[DatasetSummary] = []
    for dataset_dir, dataset_name in _find_dataset_roots(root):
        info_path = dataset_dir / "meta" / "info.json"
        try:
            info = json.loads(info_path.read_text(encoding="utf-8").rstrip("\x00"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Cannot read %s: %s", info_path, e)
            continue
        fps = info.get("fps", 0)
        try:
            grade_stats = await _count_grades(dataset_dir, fps)
        except OSError as e:
            logger.warning("Cannot read grade metadata for %s: %s", dataset_dir, e)
            grade_stats = _empty_grade_stats()
        graded = int(grade_stats["good"]) + int(grade_stats["normal"]) + int(grade_stats["bad"])
        datasets.append(DatasetSummary(
            name=dataset_name,
            path=str(dataset_dir.resolve()),
            total_episodes=info.get("total_episodes", 0),
            graded_count=graded,
            good_count=int(grade_stats["good"]),
            normal_count=int(grade_stats["normal"]),
            bad_count=int(grade_stats["bad"]),
            robot_type=info.get("robot_type"),
            fps=fps,
            total_duration_sec=float(grade_stats["total_duration_sec"]),
            good_duration_sec=float(grade_stats["good_duration_sec"]),
            normal_duration_sec=float(grade_stats["normal_duration_sec"]),
            bad_duration_sec=float(grade_stats["bad_duration_sec"]),
        ))
    await _upsert_datasets_to_db(root.name, datasets)
    return datasets


def _empty_grade_stats() -> dict[str, int | float]:
    return {
        "good": 0,
        "normal": 0,
        "bad": 0,
        "total_duration_sec": 0.0,
        "good_duration_sec": 0.0,
        "normal_duration_sec": 0.0,
        "bad_duration_sec": 0.0,
    }


async def _upsert_datasets_to_db(cell_name: str, datasets: list[DatasetSummary]) -> None:
    """Upsert dataset summaries, prune vanished datasets, and rebuild
    episode_serials lazily (only when meta/info.json mtime changes).
    """
    from backend.core.db import get_db

    db = await get_db()
    live_paths = sorted({ds.path for ds in datasets})
    dataset_columns = await _get_table_columns(db, "datasets")
    supports_info_json_mtime = "info_json_mtime" in dataset_columns
    supports_episode_serials = await _table_exists(db, "episode_serials")

    # (a) Remove datasets in this cell that no longer exist on disk.
    if live_paths:
        placeholders = ",".join("?" * len(live_paths))
        await db.execute(
            f"DELETE FROM datasets WHERE cell_name = ? AND path NOT IN ({placeholders})",
            (cell_name, *live_paths),
        )
    else:
        await db.execute("DELETE FROM datasets WHERE cell_name = ?", (cell_name,))

    for ds in datasets:
        info_json = Path(ds.path) / "meta" / "info.json"
        try:
            info_mtime = info_json.stat().st_mtime
        except OSError:
            logger.warning("cannot stat %s; skipping dataset", info_json)
            continue

        cached_mtime = None
        if supports_info_json_mtime:
            async with db.execute(
                "SELECT id, info_json_mtime FROM datasets WHERE path = ?", (ds.path,)
            ) as cursor:
                row = await cursor.fetchone()
            cached_mtime = row[1] if row else None
            await db.execute(
                """
                INSERT INTO datasets (
                    path, name, cell_name, fps, total_episodes, robot_type,
                    info_json_mtime, synced_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, NOW())
                ON CONFLICT (path) DO UPDATE SET
                  name=excluded.name, cell_name=excluded.cell_name,
                  fps=excluded.fps, total_episodes=excluded.total_episodes,
                  robot_type=excluded.robot_type,
                  info_json_mtime=excluded.info_json_mtime,
                  synced_at=excluded.synced_at
                """,
                (
                    ds.path,
                    ds.name,
                    cell_name,
                    ds.fps,
                    ds.total_episodes,
                    ds.robot_type,
                    info_mtime,
                ),
            )
        else:
            await db.execute(
                """
                INSERT INTO datasets (
                    path, name, cell_name, fps, total_episodes, robot_type,
                    synced_at
                )
                VALUES (?, ?, ?, ?, ?, ?, NOW())
                ON CONFLICT (path) DO UPDATE SET
                  name=excluded.name, cell_name=excluded.cell_name,
                  fps=excluded.fps, total_episodes=excluded.total_episodes,
                  robot_type=excluded.robot_type,
                  synced_at=excluded.synced_at
                """,
                (ds.path, ds.name, cell_name, ds.fps, ds.total_episodes, ds.robot_type),
            )

        async with db.execute("SELECT id FROM datasets WHERE path = ?", (ds.path,)) as cursor:
            dataset_id = (await cursor.fetchone())[0]

        if supports_info_json_mtime and supports_episode_serials and (
            cached_mtime is None or cached_mtime != info_mtime
        ):
            await _rebuild_episode_serials(db, dataset_id, Path(ds.path))

        await db.execute(
            """
            INSERT INTO dataset_stats (
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
              updated_at=excluded.updated_at
            """,
            (
                dataset_id, ds.graded_count, ds.good_count, ds.normal_count, ds.bad_count,
                ds.total_duration_sec, ds.good_duration_sec, ds.normal_duration_sec,
                ds.bad_duration_sec,
            ),
        )
    await db.commit()


async def _get_table_columns(db, table_name: str) -> set[str]:
    """Return column names for table_name, or an empty set when the table does not exist."""
    async with db.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = ?
        ORDER BY ordinal_position
        """,
        (table_name,),
    ) as cursor:
        rows = await cursor.fetchall()
    return {row["column_name"] for row in rows}


async def _table_exists(db, table_name: str) -> bool:
    """Return True when table_name exists in the connected database."""
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


async def _rebuild_episode_serials(db, dataset_id: int, dataset_dir: Path) -> None:
    """Replace all rows in episode_serials for dataset_id using current parquet.

    Only reads the ``episode_index`` and ``Serial_number`` columns. Episodes with
    missing or empty Serial_number are skipped with a warning; if the
    Serial_number column is absent from a parquet file the whole file is
    skipped with a warning.

    Within a single dataset_id, the same serial_number must not appear more than
    once (one task = one folder, one serial = one source recording). When a
    parquet legitimately repeats the same Serial_number across multiple
    episode_index rows (e.g. a "split" dataset that chunks one recording into
    several episodes), only the first episode_index is registered and the rest
    are dropped with a WARNING log. Downstream annotation lookup keys on
    serial_number, so keeping multiple mappings would create ambiguous joins.
    """
    from glob import glob

    import pyarrow.parquet as pq

    pattern = str(dataset_dir / "meta" / "episodes" / "chunk-*" / "file-*.parquet")
    collected: list[tuple[int, int, str]] = []
    seen_serials: dict[str, int] = {}
    dropped: dict[str, list[int]] = {}
    for parquet_path in sorted(glob(pattern)):
        schema = pq.read_schema(parquet_path)
        if "Serial_number" not in schema.names:
            logger.warning("parquet %s missing Serial_number; skipping", parquet_path)
            continue
        table = pq.read_table(parquet_path, columns=["episode_index", "Serial_number"])
        indices = table.column("episode_index").to_pylist()
        serials = table.column("Serial_number").to_pylist()
        for idx, serial in zip(indices, serials):
            if serial is None or serial == "":
                logger.warning(
                    "episode %s in %s has empty Serial_number; skipping",
                    idx, dataset_dir,
                )
                continue
            serial_str = str(serial)
            if serial_str in seen_serials:
                dropped.setdefault(serial_str, []).append(int(idx))
                continue
            seen_serials[serial_str] = int(idx)
            collected.append((dataset_id, int(idx), serial_str))

    if dropped:
        for serial_str, dup_indices in dropped.items():
            kept = seen_serials[serial_str]
            logger.warning(
                "duplicate Serial_number %s in dataset_id=%s (%s): kept episode_index=%s, "
                "dropped episode_index=%s",
                serial_str, dataset_id, dataset_dir, kept, dup_indices,
            )

    await db.execute("DELETE FROM episode_serials WHERE dataset_id = ?", (dataset_id,))
    if collected:
        await db.executemany(
            "INSERT INTO episode_serials (dataset_id, episode_index, serial_number) "
            "VALUES (?, ?, ?)",
            collected,
        )


async def _count_grades(dataset_dir: Path, fps: int = 0) -> dict[str, int | float]:
    """Count grades and durations from parquet, sidecar, and current DB annotations."""
    from glob import glob
    import pyarrow.parquet as pq
    from backend.datasets.services.episode_service import _load_sidecar_json

    counts: dict[str, int | float] = {"good": 0, "normal": 0, "bad": 0}

    # 1. Read grades, lengths, and serials from parquet.
    episode_grades: dict[int, str | None] = {}
    episode_lengths: dict[int, int] = {}
    episode_serials: dict[int, str] = {}
    seen_serials: set[str] = set()
    parquet_files = sorted(glob(str(dataset_dir / "meta" / "episodes" / "chunk-*" / "file-*.parquet")))
    for f in parquet_files:
        schema = pq.read_schema(f)
        cols = ["episode_index"]
        has_grade = "grade" in schema.names
        has_length = "length" in schema.names
        has_range = "dataset_from_index" in schema.names and "dataset_to_index" in schema.names
        has_serial = "Serial_number" in schema.names
        if has_grade:
            cols.append("grade")
        if has_length:
            cols.append("length")
        elif has_range:
            cols.extend(["dataset_from_index", "dataset_to_index"])
        if has_serial:
            cols.append("Serial_number")
        table = pq.read_table(f, columns=cols)
        indices = table.column("episode_index").to_pylist()
        grades = table.column("grade").to_pylist() if has_grade else [None] * len(indices)
        if has_length:
            lengths = table.column("length").to_pylist()
        elif has_range:
            starts = table.column("dataset_from_index").to_pylist()
            ends = table.column("dataset_to_index").to_pylist()
            lengths = [
                max(int(end or 0) - int(start or 0), 0)
                for start, end in zip(starts, ends)
            ]
        else:
            lengths = [0] * len(indices)
        serials = table.column("Serial_number").to_pylist() if has_serial else [None] * len(indices)
        for idx, g, length, serial in zip(indices, grades, lengths, serials):
            ep_idx = int(idx)
            episode_grades[ep_idx] = g
            episode_lengths[ep_idx] = int(length or 0)
            if serial is None or serial == "":
                continue
            serial_str = str(serial)
            if serial_str in seen_serials:
                continue
            seen_serials.add(serial_str)
            episode_serials[ep_idx] = serial_str

    # 2. Overlay legacy sidecar data.
    sidecar = _load_sidecar_json(dataset_dir)
    for ep_idx_str, ann in sidecar.items():
        grade = ann.get("grade")
        if grade is not None:
            episode_grades[int(ep_idx_str)] = grade

    # 3. Overlay serial-keyed DB annotations. This is the current source of truth;
    # dataset_stats is only a cache and may be stale after migrations/reconversion.
    db_grades = await _load_annotation_grades_by_serial(list(episode_serials.values()))
    if db_grades:
        for ep_idx, serial in episode_serials.items():
            if serial in db_grades:
                episode_grades[ep_idx] = db_grades[serial]

    # 4. Count grades and compute durations.
    total_frames = 0
    grade_frames: dict[str, int] = {"good": 0, "normal": 0, "bad": 0}
    for ep_idx, grade in episode_grades.items():
        length = episode_lengths.get(ep_idx, 0)
        total_frames += length
        if grade:
            normalized = grade.strip().lower()
            if normalized in counts:
                counts[normalized] += 1
            if normalized in grade_frames:
                grade_frames[normalized] += length

    divisor = fps if fps > 0 else 1
    counts["total_duration_sec"] = total_frames / divisor
    counts["good_duration_sec"] = grade_frames["good"] / divisor
    counts["normal_duration_sec"] = grade_frames["normal"] / divisor
    counts["bad_duration_sec"] = grade_frames["bad"] / divisor

    return counts


async def _load_annotation_grades_by_serial(serials: list[str]) -> dict[str, str | None]:
    """Return current annotation grades keyed by serial number."""
    if not serials:
        return {}

    try:
        from backend.core.db import get_db

        db = await get_db()
        if not await _table_exists(db, "annotations"):
            return {}

        result: dict[str, str | None] = {}
        unique_serials = sorted(set(serials))
        for i in range(0, len(unique_serials), 500):
            chunk = unique_serials[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            async with db.execute(
                f"SELECT serial_number, grade FROM annotations WHERE serial_number IN ({placeholders})",
                tuple(chunk),
            ) as cursor:
                for row in await cursor.fetchall():
                    result[row[0]] = row[1]
        return result
    except Exception:
        logger.exception("Cannot read annotation grades for %s", serials[:3])
        return {}


def _count_datasets(cell_dir: Path) -> int:
    """Count dataset roots recursively under cell_dir."""
    return len(_find_dataset_roots(cell_dir))
