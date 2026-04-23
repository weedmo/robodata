# UI Service Docker Operations

Date: 2026-04-22
Scope: production-style deployment for the curation UI service
Status: design approved by operator choices in-chat (`nginx + app`)

## 1. Problem

The current startup path is development-oriented:

- `start.sh` runs `uvicorn` directly and also starts `npm run dev`.
- The Vite dev server is appropriate for local iteration, but it is not a stable runtime surface for real use.
- There is no repo-native production deployment bundle for the UI service, so environment drift and manual setup remain likely.

The goal is to add an operationally stable Docker deployment path without disturbing the existing developer workflow.

## 2. Goals

1. Add a production Docker deployment for the UI service using two containers: `nginx` and `app`.
2. Keep `nginx` responsible for static frontend delivery, SPA fallback, and reverse proxying `/api/*`.
3. Keep `app` responsible only for FastAPI application serving.
4. Preserve the existing relative `/api` frontend contract so application code does not need environment-specific API rewrites.
5. Keep the converter runtime out of this scope because it currently shells out to host Docker.

## 3. Non-goals

- Replacing `start.sh` as the default developer entrypoint.
- Containerizing the converter control path in this change.
- Refactoring backend routing or frontend API code beyond what the deployment path strictly needs.
- Introducing orchestration beyond `docker compose`.

## 4. Chosen architecture

### 4.1 Topology

```
browser
  |
  v
nginx:80
  |- serves frontend/dist
  |- SPA fallback -> /index.html
  |- proxies /api/* -> app:8001
  |- proxies websocket upgrades -> app:8001
  |
  v
app:8001
  |- FastAPI
  |- dataset/db volume mounts
```

### 4.2 App image

- Build from the repo root so backend package files are available.
- Install Python dependencies from `pyproject.toml`.
- Run FastAPI with a production process command instead of the development `start.sh` path.
- Expose port `8001` internally only.

### 4.3 Nginx image

- Build frontend assets with Node in a build stage.
- Copy `frontend/dist` into the nginx runtime image.
- Provide an nginx config that:
  - serves `index.html` and static assets
  - applies SPA fallback for non-file requests
  - proxies `/api/` to `app:8001`
  - supports websocket upgrade headers for `/api/converter/logs`

### 4.4 Data and runtime configuration

- `app` receives `CURATION_*` environment variables through compose.
- Dataset root and sqlite DB live on mounted host paths.
- Only `nginx` is published to the host port.
- `app` remains on the internal compose network.

## 5. Why this approach

### Option A: `nginx + app` split deployment

Chosen.

Pros:
- clean operational separation
- standard frontend/backend deployment shape
- static delivery removed from Python app process
- easy to reason about restart policy and health boundaries

Cons:
- two Docker images instead of one
- nginx config must be maintained

### Option B: single app container serving built frontend

Rejected for this task.

Why rejected:
- simpler image count, but weaker separation of concerns
- pushes static hosting responsibility back into FastAPI
- less aligned with the user-selected `nginx + app` topology

### Option C: include converter in the same compose stack

Rejected for this task.

Why rejected:
- backend converter control currently depends on host Docker access
- mixing that concern into this rollout adds scope and operational risk

## 6. Verification criteria

The work is complete only when all of the following are true:

1. A production deployment definition exists in-repo for `nginx + app`.
2. `docker compose config` succeeds for the new deployment file.
3. The frontend production build completes successfully.
4. The Docker definitions are covered by regression tests that assert the expected topology and proxy behavior.
5. README documents how to run the production stack and what remains out of scope.

## 7. Risks and mitigations

- Risk: nginx websocket proxying is incomplete.
  - Mitigation: explicitly configure `Upgrade` and `Connection` headers in nginx and cover them in a regression test.
- Risk: app container accidentally depends on frontend dev tooling.
  - Mitigation: keep app image Python-only; frontend build lives in the nginx image build.
- Risk: converter UI appears broken in production due to Docker daemon expectations.
  - Mitigation: document that converter control remains host-Docker dependent and is not bundled into this stack.

## 8. Files expected to change

- Create `docker/ui/Dockerfile.app`
- Create `docker/ui/Dockerfile.nginx`
- Create `docker/ui/nginx.conf`
- Create `docker/ui/docker-compose.yml`
- Create or update `.dockerignore`
- Create regression tests for the new Docker deployment files
- Update `README.md`
