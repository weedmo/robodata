"""Regression tests for the unified UI service Docker deployment."""

from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DOCKERFILE = REPO_ROOT / "docker" / "ui" / "Dockerfile.app"
NGINX_DOCKERFILE = REPO_ROOT / "docker" / "ui" / "Dockerfile.nginx"
CURATION_WORKER_DOCKERFILE = REPO_ROOT / "docker" / "curation-worker" / "Dockerfile"
NGINX_CONF = REPO_ROOT / "docker" / "ui" / "nginx.conf"
COMPOSE_FILE = REPO_ROOT / "docker" / "compose.yml"


def test_ui_ops_files_exist():
    assert APP_DOCKERFILE.exists()
    assert NGINX_DOCKERFILE.exists()
    assert CURATION_WORKER_DOCKERFILE.exists()
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


def test_compose_topology_keeps_ui_services_in_unified_stack():
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    services = compose["services"]

    assert "app" in services
    assert "nginx" in services
    assert services["app"]["build"]["dockerfile"] == "docker/ui/Dockerfile.app"
    assert services["nginx"]["build"]["dockerfile"] == "docker/ui/Dockerfile.nginx"
    assert services["nginx"]["ports"] == ['${CURATION_UI_PORT:-18080}:80']
    assert services["app"]["depends_on"]["db"]["condition"] == "service_healthy"
    assert services["nginx"]["depends_on"]["app"]["condition"] == "service_healthy"
    assert services["nginx"]["depends_on"]["rerun"]["condition"] == "service_started"
    assert "volumes" not in services["rerun"]
    assert "/var/run/docker.sock:/var/run/docker.sock:ro" in services["app"]["volumes"]
    assert services["app"]["environment"]["CURATION_DOCKER_PROJECT_NAME"] == "${CURATION_DOCKER_PROJECT_NAME:-curation-tools}"


def test_app_dockerfile_runs_fastapi_on_internal_port():
    dockerfile = APP_DOCKERFILE.read_text(encoding="utf-8")

    assert "FROM python:" in dockerfile
    assert "COPY pyproject.toml uv.lock ./" in dockerfile
    assert "ffmpeg" in dockerfile
    assert 'CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8001"]' in dockerfile


def test_nginx_dockerfile_builds_frontend_bundle():
    dockerfile = NGINX_DOCKERFILE.read_text(encoding="utf-8")

    assert "FROM node:" in dockerfile
    assert "RUN npm ci" in dockerfile
    assert "RUN npm run build" in dockerfile
    assert "FROM nginx:" in dockerfile
    assert "COPY --from=frontend-builder /frontend/dist /usr/share/nginx/html" in dockerfile


def test_curation_worker_dockerfile_installs_runtime_dependencies():
    dockerfile = CURATION_WORKER_DOCKERFILE.read_text(encoding="utf-8")

    assert "pip install --no-cache-dir" in dockerfile
    assert "asyncpg" in dockerfile
    assert "numpy" in dockerfile
    assert "pyarrow" in dockerfile
    assert "pydantic-settings" in dockerfile
