"""Router for source, cell, and dataset listing endpoints."""

import urllib.parse
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from backend.core.config import settings
from backend.datasets.schemas import CellInfo, DatasetSourceInfo, DatasetSummary
from backend.datasets.services.cell_service import (
    get_datasets_in_cell,
    list_dataset_sources,
    scan_cells,
)

router = APIRouter(prefix="/api/cells", tags=["cells"])


def _resolve_allowed_root(root: str | None) -> list[str]:
    # Cell/dataset listing is scoped to the curation roots (lerobot, lerobot_test),
    # not the full allowed_dataset_roots (which may include the broader base for I/O).
    curation_roots = settings.configured_dataset_roots()
    if root is None:
        return curation_roots

    resolved = Path(root).resolve()
    allowed = [Path(item).resolve() for item in curation_roots]
    if resolved not in allowed:
        raise HTTPException(status_code=403, detail="Access denied: root outside curation roots")
    return [str(resolved)]


@router.get("/sources", response_model=list[DatasetSourceInfo])
async def list_sources():
    """Return configured dataset sources under the shared base path."""
    return list_dataset_sources(
        settings.dataset_root_base,
        settings.dataset_sources,
        pattern=settings.cell_name_pattern,
    )


@router.get("", response_model=list[CellInfo])
async def list_cells(root: str | None = Query(None, description="Optional source root to scan for cells")):
    """Scan allowed dataset roots for cell directories."""
    return scan_cells(_resolve_allowed_root(root), pattern=settings.cell_name_pattern)


@router.get("/{cell_path:path}/datasets", response_model=list[DatasetSummary])
async def list_datasets_in_cell(cell_path: str):
    """List datasets inside a cell directory.

    cell_path: Full absolute path to the cell directory, URL-encoded.
               Must be within an allowed_dataset_root.
    """
    decoded = urllib.parse.unquote(cell_path)
    resolved = Path(decoded).resolve()

    # Listing cells is a curation-scope operation; restrict to curation roots.
    curation_roots = [Path(r).resolve() for r in settings.configured_dataset_roots()]
    if not any(resolved == root or str(resolved).startswith(str(root) + "/") for root in curation_roots):
        raise HTTPException(status_code=403, detail="Access denied: path outside curation roots")

    datasets = await get_datasets_in_cell(decoded)
    if not Path(decoded).exists():
        raise HTTPException(status_code=404, detail=f"Cell path not found: {decoded}")
    return datasets
