"""Regression tests for the UI service production Docker deployment."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DOCKERFILE = REPO_ROOT / "docker" / "ui" / "Dockerfile.app"
NGINX_DOCKERFILE = REPO_ROOT / "docker" / "ui" / "Dockerfile.nginx"
NGINX_CONF = REPO_ROOT / "docker" / "ui" / "nginx.conf"
COMPOSE_FILE = REPO_ROOT / "docker" / "ui" / "docker-compose.yml"


def test_ui_ops_files_exist():
    assert APP_DOCKERFILE.exists()
    assert NGINX_DOCKERFILE.exists()
    assert NGINX_CONF.exists()
    assert COMPOSE_FILE.exists()


def test_nginx_conf_proxies_api_and_websockets():
    config = NGINX_CONF.read_text(encoding="utf-8")

    assert "location /api/" in config
    assert "proxy_pass http://app:8001;" in config
    assert "proxy_http_version 1.1;" in config
    assert "proxy_set_header Upgrade $http_upgrade;" in config
    assert 'proxy_set_header Connection "upgrade";' in config
    assert "try_files $uri $uri/ /index.html;" in config


def test_compose_topology_is_nginx_plus_app():
    compose = COMPOSE_FILE.read_text(encoding="utf-8")

    assert "app:" in compose
    assert "nginx:" in compose
    assert "depends_on:" in compose
    assert "dockerfile: docker/ui/Dockerfile.app" in compose
    assert "dockerfile: docker/ui/Dockerfile.nginx" in compose
    assert 'CURATION_UI_PORT:-18080' in compose


def test_app_dockerfile_runs_fastapi_on_internal_port():
    dockerfile = APP_DOCKERFILE.read_text(encoding="utf-8")

    assert "FROM python:" in dockerfile
    assert "COPY pyproject.toml uv.lock ./" in dockerfile
    assert 'CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8001"]' in dockerfile


def test_nginx_dockerfile_builds_frontend_bundle():
    dockerfile = NGINX_DOCKERFILE.read_text(encoding="utf-8")

    assert "FROM node:" in dockerfile
    assert "RUN npm ci" in dockerfile
    assert "RUN npm run build" in dockerfile
    assert "FROM nginx:" in dockerfile
    assert "COPY --from=frontend-builder /frontend/dist /usr/share/nginx/html" in dockerfile
