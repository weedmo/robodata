"""Regression tests for the hybrid host-dev launcher."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
START_SH = REPO_ROOT / "start.sh"


def test_start_sh_bootstraps_compose_db_and_exports_host_backend_env():
    script = START_SH.read_text(encoding="utf-8")

    assert 'PROJECT_NAME="${PROJECT_NAME:-curation-tools}"' in script
    assert 'ENV_FILE="$(resolve_path "${ENV_FILE:-docker/.env}")"' in script
    assert 'docker compose --env-file "$ENV_FILE" -p "$PROJECT_NAME" -f "$COMPOSE_FILE"' in script
    assert 'up -d db rerun' in script
    assert 'PG_BIND="${CURATION_PG_HOST_PORT:-127.0.0.1:5433}"' in script
    assert 'if [[ "$PG_BIND" == *:* ]]; then' in script
    assert 'PG_HOST="127.0.0.1"' in script
    assert 'export CURATION_DB_URL=' in script
    assert 'export CURATION_DATASET_ROOT_BASE=' in script
    assert 'export CURATION_DATASET_PATH=' in script
    assert 'uvicorn backend.main:app' in script
    assert "npm run dev" in script


def test_start_sh_only_cleans_up_host_processes():
    script = START_SH.read_text(encoding="utf-8")

    assert "kill $BACKEND_PID" in script
    assert "kill $FRONTEND_PID" in script
    assert "docker compose" not in script.split("cleanup()", 1)[1]
