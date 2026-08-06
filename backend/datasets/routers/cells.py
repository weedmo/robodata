"""Router for source, cell, and dataset listing endpoints."""

import asyncio
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
from backend.datasets.services.path_policy import (
    PathOutsideAllowedRoots,
    require_contained_in_roots,
)

router = APIRouter(prefix="/api/cells", tags=["cells"])


def _resolve_allowed_root(root: str | None) -> list[str]:
    # Cell/dataset listing is scoped to the curation roots (lerobot, lerobot_test),
    # not the full allowed_dataset_roots (which may include the broader base for I/O).
    curation_roots = settings.configured_dataset_roots()
    if root is None:
        return curation_roots

    try:
        contained = require_contained_in_roots(root, curation_roots)
    except PathOutsideAllowedRoots as exc:
        raise HTTPException(status_code=403, detail="Access denied: root outside curation roots") from exc
    if contained.path != contained.root:
        raise HTTPException(status_code=403, detail="Access denied: root outside curation roots")
    return [str(contained.path)]


@router.get("/sources", response_model=list[DatasetSourceInfo])
async def list_sources():
    """Return configured dataset sources under the shared base path."""
    return await asyncio.to_thread(
        list_dataset_sources,
        settings.dataset_root_base,
        settings.dataset_sources,
        settings.cell_name_pattern,
    )


@router.get("", response_model=list[CellInfo])
async def list_cells(root: str | None = Query(None, description="Optional source root to scan for cells")):
    """Scan allowed dataset roots for cell directories."""
    return await asyncio.to_thread(
        scan_cells,
        _resolve_allowed_root(root),
        settings.cell_name_pattern,
    )


@router.get("/{cell_path:path}/datasets", response_model=list[DatasetSummary])
async def list_datasets_in_cell(cell_path: str):
    """List datasets inside a cell directory.

    cell_path: Full absolute path to the cell directory, URL-encoded.
               Must be within an allowed_dataset_root.
    """
    decoded = urllib.parse.unquote(cell_path)

    # Listing cells is a curation-scope operation; restrict to curation roots.
    try:
        require_contained_in_roots(decoded, settings.configured_dataset_roots())
    except PathOutsideAllowedRoots as exc:
        raise HTTPException(status_code=403, detail="Access denied: path outside curation roots") from exc

    datasets = await get_datasets_in_cell(decoded)
    if not Path(decoded).exists():
        raise HTTPException(status_code=404, detail=f"Cell path not found: {decoded}")
    return datasets
