from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from backend.core.config import settings
from backend.core.db import get_db
from backend.datasets.schemas import DatasetExportRequest, DatasetInfo, DatasetLoadRequest
from backend.datasets.services.dataset_registry import dataset_key_for, dataset_registry
from backend.datasets.services.export_service import export_dataset

router = APIRouter(prefix="/api/datasets", tags=["datasets"])


def _default_scan_root() -> Path:
    configured = Path(settings.dataset_path)
    if configured.exists() and configured.is_dir():
        return configured

    for root in settings.configured_dataset_roots():
        candidate = Path(root)
        if candidate.exists() and candidate.is_dir():
            return candidate

    return configured


@router.get("/list")
async def list_datasets(root: str | None = Query(None, description="Root directory to scan for LeRobot datasets")):
    """Scan the configured dataset root for valid LeRobot datasets (dirs with meta/info.json)."""
    root_path = Path(root) if root else _default_scan_root()
    if not root_path.exists() or not root_path.is_dir():
        return []

    datasets = []
    for child in sorted(root_path.iterdir()):
        if child.is_dir() and (child / "meta" / "info.json").exists():
            datasets.append({"name": child.name, "path": str(child.resolve())})
    return datasets


@router.post("/load", response_model=DatasetInfo)
async def load_dataset(req: DatasetLoadRequest):
    try:
        ctx = dataset_registry.get(req.path)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    info = ctx.get_info()
    return DatasetInfo(
        path=str(ctx.dataset_path),
        dataset_key=dataset_key_for(ctx.dataset_path),
        name=info.get("robot_type", ctx.dataset_path.name),
        fps=info.get("fps", 0),
        total_episodes=info.get("total_episodes", len(ctx.get_episodes())),
        total_tasks=info.get("total_tasks", len(ctx.get_tasks())),
        robot_type=info.get("robot_type"),
        features=info.get("features", {}),
    )


@router.get("/info", response_model=DatasetInfo)
async def get_info(dataset_path: str = Query(...)):
    try:
        ctx = dataset_registry.get(dataset_path)
        info = ctx.get_info()
        return DatasetInfo(
            path=str(ctx.dataset_path),
            dataset_key=dataset_key_for(ctx.dataset_path),
            name=info.get("robot_type", ctx.dataset_path.name),
            fps=info.get("fps", 0),
            total_episodes=info.get("total_episodes", len(ctx.get_episodes())),
            total_tasks=info.get("total_tasks", len(ctx.get_tasks())),
            robot_type=info.get("robot_type"),
            features=info.get("features", {}),
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/export")
async def export_dataset_endpoint(req: DatasetExportRequest):
    """Export the loaded dataset, excluding episodes with specified grades."""
    try:
        result = export_dataset(req.output_path, req.exclude_grades)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return result


@router.get("/search")
async def search_datasets(
    robot_type: str | None = Query(None),
    cell: str | None = Query(None),
    min_episodes: int | None = Query(None),
    min_good_ratio: float | None = Query(None),
):
    """Search across all registered datasets using DB metadata."""
    db = await get_db()
    query = """
        SELECT d.name, d.path, d.cell_name, d.robot_type, d.fps, d.total_episodes,
               COALESCE(s.graded_count, 0) as graded_count,
               COALESCE(s.good_count, 0) as good_count,
               COALESCE(s.normal_count, 0) as normal_count,
               COALESCE(s.bad_count, 0) as bad_count,
               COALESCE(s.total_duration_sec, 0) as total_duration_sec,
               COALESCE(s.good_duration_sec, 0) as good_duration_sec,
               COALESCE(s.normal_duration_sec, 0) as normal_duration_sec,
               COALESCE(s.bad_duration_sec, 0) as bad_duration_sec
        FROM datasets d
        LEFT JOIN dataset_stats s ON d.id = s.dataset_id
        WHERE 1=1
    """
    params: list = []

    if robot_type is not None:
        query += " AND d.robot_type = ?"
        params.append(robot_type)
    if cell is not None:
        query += " AND d.cell_name = ?"
        params.append(cell)
    if min_episodes is not None:
        query += " AND d.total_episodes >= ?"
        params.append(min_episodes)
    if min_good_ratio is not None:
        query += " AND CASE WHEN s.graded_count > 0 THEN CAST(s.good_count AS REAL) / s.graded_count ELSE 0 END >= ?"
        params.append(min_good_ratio)

    query += " ORDER BY d.name"

    async with db.execute(query, params) as cursor:
        rows = await cursor.fetchall()

    return [
        {
            "name": r[0], "path": r[1], "total_episodes": r[5],
            "graded_count": r[6], "good_count": r[7], "normal_count": r[8], "bad_count": r[9],
            "robot_type": r[3], "fps": r[4],
            "total_duration_sec": r[10], "good_duration_sec": r[11],
            "normal_duration_sec": r[12], "bad_duration_sec": r[13],
        }
        for r in rows
    ]
