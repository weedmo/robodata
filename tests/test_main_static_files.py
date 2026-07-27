"""Frontend static-route installation contracts."""

from pathlib import Path

from fastapi import FastAPI

from backend.main import _mount_frontend


def _route_paths(app: FastAPI) -> set[str]:
    return {route.path for route in app.routes}


def test_mount_frontend_allows_source_checkout_without_built_assets(
    tmp_path: Path,
) -> None:
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    app = FastAPI()

    _mount_frontend(app, tmp_path)

    assert "/{full_path:path}" in _route_paths(app)
    assert "/assets" not in _route_paths(app)


def test_mount_frontend_serves_assets_when_bundle_exists(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    (tmp_path / "assets").mkdir()
    app = FastAPI()

    _mount_frontend(app, tmp_path)

    assert "/{full_path:path}" in _route_paths(app)
    assert "/assets" in _route_paths(app)
