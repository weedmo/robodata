"""Regression coverage for the removed Rerun integration boundary."""

from pathlib import Path
import tomllib

from backend.converter.service import COMPOSE_SERVICES
from backend.main import app


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_viewer_routes_and_modules_are_absent():
    route_paths = {route.path for route in app.routes}

    assert not any(path.startswith("/api/rerun") for path in route_paths)
    assert not (REPO_ROOT / "backend" / "datasets" / "routers" / "rerun.py").exists()
    assert not (REPO_ROOT / "backend" / "datasets" / "services" / "rerun_service.py").exists()


def test_runtime_service_inventory_excludes_rerun():
    assert "rerun" not in COMPOSE_SERVICES


def test_project_has_no_direct_rerun_dependency():
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    dependencies = project["project"]["dependencies"]
    optional_dependencies = project["project"].get("optional-dependencies", {})
    assert not any(dependency.startswith("rerun-sdk") for dependency in dependencies)
    assert "rerun" not in optional_dependencies
