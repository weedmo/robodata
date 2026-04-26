import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

from backend.core.config import settings
from backend.core.db import init_db, close_db
from backend.datasets.routers import (
    datasets, episodes, tasks, rerun, videos, scalars,
    dataset_ops, cells, distribution, fields,
)
from backend.converter import router as converter_mod
from backend.jobs.router import router as jobs_router
from backend.workers.router import router as workers_router
from backend.datasets.services import rerun_service

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()

    if settings.enable_rerun:
        try:
            rerun_service.init_rerun(
                grpc_port=settings.rerun_grpc_port,
                web_port=settings.rerun_web_port,
            )
            logger.info(
                "Rerun SDK configured to stream to %s",
                rerun_service.get_rerun_sink_url(),
            )
        except Exception as e:
            logger.warning("Rerun init failed: %s (video player still works)", e)
    else:
        logger.info("Rerun disabled — using native video player")

    yield

    await close_db()


app = FastAPI(
    title="robodata-studio",
    description="Internal curation and analytics tool for LeRobot datasets",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type"],
)

app.include_router(datasets.router)
app.include_router(episodes.router)
app.include_router(tasks.router)
app.include_router(rerun.router)
app.include_router(videos.router)
app.include_router(scalars.router)
app.include_router(dataset_ops.router)
app.include_router(cells.router)
app.include_router(distribution.router)
app.include_router(fields.router)
app.include_router(converter_mod.router)
app.include_router(jobs_router)
app.include_router(workers_router)


@app.exception_handler(HTTPException)
async def _flatten_dict_detail(_: Request, exc: HTTPException) -> JSONResponse:
    # When routers raise HTTPException(detail={"error": ..., ...}), surface the
    # dict as the top-level JSON body so clients can read fields like
    # `existing_job_id` directly. For string detail we preserve FastAPI's
    # default `{"detail": "..."}` shape so existing routers stay compatible.
    if isinstance(exc.detail, dict):
        return JSONResponse(exc.detail, status_code=exc.status_code)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


# Serve frontend static files (production build)
_frontend_dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _frontend_dist.is_dir():
    app.mount("/assets", StaticFiles(directory=_frontend_dist / "assets"), name="static")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        """Serve index.html for all non-API routes (SPA routing)."""
        # Unknown /api/* routes must 404, not silently return the SPA shell
        if full_path.startswith("api/") or full_path == "api":
            raise HTTPException(status_code=404, detail="Not Found")
        file = _frontend_dist / full_path
        if file.is_file():
            return FileResponse(file)
        return FileResponse(_frontend_dist / "index.html")


def start():
    uvicorn.run(
        "backend.main:app",
        host=settings.host,
        port=settings.fastapi_port,
        reload=settings.debug,
    )


if __name__ == "__main__":
    start()
