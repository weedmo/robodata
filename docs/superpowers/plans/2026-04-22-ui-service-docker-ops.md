# UI Service Docker Operations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a production `nginx + app` Docker deployment path for the UI service without changing the current development workflow.

**Architecture:** Build two images from the repo: a Python `app` image for FastAPI and an nginx image that compiles and serves the frontend bundle while reverse proxying `/api` and websocket traffic to the app container. Keep converter Docker control out of scope and document that limit.

**Tech Stack:** FastAPI, Python packaging via `pyproject.toml`, React/Vite build, nginx, Docker, Docker Compose, pytest

---

### Task 1: Lock deployment expectations with failing regression tests

**Files:**
- Create: `tests/test_ui_service_docker_ops.py`

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ui_service_docker_ops.py -q`
Expected: FAIL because the new Docker deployment files do not exist yet.

- [ ] **Step 3: Expand the failing coverage**

```python
def test_nginx_conf_proxies_api_and_websockets():
    config = NGINX_CONF.read_text(encoding="utf-8")

    assert "location /api/" in config
    assert "proxy_pass http://app:8001;" in config
    assert "proxy_http_version 1.1" in config
    assert "proxy_set_header Upgrade $http_upgrade;" in config
    assert 'proxy_set_header Connection "upgrade";' in config


def test_compose_topology_is_nginx_plus_app():
    compose = COMPOSE_FILE.read_text(encoding="utf-8")

    assert "app:" in compose
    assert "nginx:" in compose
    assert "depends_on:" in compose
    assert "./docker/ui/nginx.conf:/etc/nginx/conf.d/default.conf:ro" in compose
```

- [ ] **Step 4: Run test to verify it still fails for the right reason**

Run: `python -m pytest tests/test_ui_service_docker_ops.py -q`
Expected: FAIL on missing files, not import errors.

- [ ] **Step 5: Commit**

```bash
git add tests/test_ui_service_docker_ops.py
git commit -m "Define regression coverage for UI service Docker topology"
```

### Task 2: Add the Docker deployment files

**Files:**
- Create: `docker/ui/Dockerfile.app`
- Create: `docker/ui/Dockerfile.nginx`
- Create: `docker/ui/nginx.conf`
- Create: `docker/ui/docker-compose.yml`
- Create: `.dockerignore`

- [ ] **Step 1: Write minimal app image**

```dockerfile
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir .

COPY backend /app/backend

EXPOSE 8001

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8001"]
```

- [ ] **Step 2: Write minimal nginx image with frontend build stage**

```dockerfile
FROM node:22-bookworm-slim AS frontend-builder

WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend /frontend
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=frontend-builder /frontend/dist /usr/share/nginx/html
COPY docker/ui/nginx.conf /etc/nginx/conf.d/default.conf
```

- [ ] **Step 3: Write nginx config**

```nginx
server {
    listen 80;
    server_name _;

    root /usr/share/nginx/html;
    index index.html;

    location /api/ {
        proxy_pass http://app:8001;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }

    location / {
        try_files $uri $uri/ /index.html;
    }
}
```

- [ ] **Step 4: Write compose file**

```yaml
services:
  app:
    build:
      context: ../..
      dockerfile: docker/ui/Dockerfile.app
    restart: unless-stopped
    environment:
      CURATION_HOST: 0.0.0.0
      CURATION_FASTAPI_PORT: 8001
    expose:
      - "8001"

  nginx:
    build:
      context: ../..
      dockerfile: docker/ui/Dockerfile.nginx
    depends_on:
      - app
    restart: unless-stopped
    ports:
      - "18080:80"
```

- [ ] **Step 5: Add `.dockerignore` entries**

```dockerignore
.git
.omx
.venv
frontend/node_modules
frontend/dist
__pycache__
*.pyc
```

- [ ] **Step 6: Run the targeted tests**

Run: `python -m pytest tests/test_ui_service_docker_ops.py -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add .dockerignore docker/ui tests/test_ui_service_docker_ops.py
git commit -m "Add production Docker deployment for UI service"
```

### Task 3: Document the production deployment path

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Write the failing doc expectation mentally and then update README**

```markdown
## Production-style Docker run

```bash
docker compose -f docker/ui/docker-compose.yml up --build -d
```

Open `http://localhost:18080`.

Notes:
- `nginx` serves the frontend bundle.
- `app` serves FastAPI behind the reverse proxy.
- Converter control remains outside this stack because it still depends on host Docker access.
```

- [ ] **Step 2: Verify the README includes the new deployment path**

Run: `rg -n "Production-style Docker run|docker/ui/docker-compose.yml|Converter control remains outside this stack" README.md`
Expected: 3 matching lines

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "Document UI service Docker deployment"
```

### Task 4: Verify the whole rollout

**Files:**
- Verify only

- [ ] **Step 1: Run the Docker regression tests**

Run: `python -m pytest tests/test_ui_service_docker_ops.py -q`
Expected: PASS

- [ ] **Step 2: Build the frontend bundle**

Run: `cd frontend && npm run build`
Expected: Vite production build completes successfully

- [ ] **Step 3: Validate compose syntax**

Run: `docker compose -f docker/ui/docker-compose.yml config`
Expected: merged compose output with no errors

- [ ] **Step 4: Build both images**

Run: `docker compose -f docker/ui/docker-compose.yml build`
Expected: `app` and `nginx` images build successfully

- [ ] **Step 5: Optional smoke run**

Run: `docker compose -f docker/ui/docker-compose.yml up -d && curl -fsS http://localhost:18080/api/health`
Expected: `{"status":"ok"}`

- [ ] **Step 6: Commit**

```bash
git status --short
```
