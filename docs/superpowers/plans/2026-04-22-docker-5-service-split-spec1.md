# Docker 5-Service Split (Spec-1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `docs/superpowers/specs/2026-04-22-docker-5-service-split-spec1-design.md`

**Goal:** Reorganize curation-tools into five Docker services (app, db, rerun, converter, curation-worker) with PostgreSQL replacing SQLite, while preserving all existing feature behavior.

**Architecture:** Single `docker/compose.yml` with compose profiles (`convert`, `curator`). Backend migrates from `aiosqlite` to `asyncpg` with a thin wrapper preserving the existing call-site shape. Rerun becomes a standalone `rerun serve` container reached by the frontend through an nginx `/rerun/` reverse-proxy. All work happens in an isolated git worktree (`../curation-tools-docker-split/`) branched from `main`.

**Tech Stack:** Docker Compose v2, PostgreSQL 16, asyncpg, FastAPI, React 19, Rerun SDK 0.22+, nginx, bash.

**Conventions for this plan:**
- Every Task below assumes the working directory is the isolated worktree created in Task 0 (`../curation-tools-docker-split/`).
- Tests for backend DB code run against the live `db` compose service (spun up in Task 1). No testcontainers.
- Commits use the imperative-mood style of the existing repo (`Add X`, `Fix Y`, not `feat:`/`fix:` prefixes).

---

## Task 0: Create isolated worktree + feature branch

**Purpose:** Physically isolate all subsequent work so the current active branch (`feat/rosbag2lerobot-svt-converter`) and its uncommitted changes are untouched.

**Files:** none modified; filesystem operations only.

- [ ] **Step 1: Verify origin state from the current repo**

Run (from the original repo `/home/tommoro/jm_ws/local_data_pipline/curation-tools`):
```bash
git branch --show-current
git rev-parse --short HEAD
```
Expected: current branch printed (likely `feat/rosbag2lerobot-svt-converter`), and a short hash.

- [ ] **Step 2: Create worktree on a new branch off main**

Run:
```bash
git worktree add -b feat/docker-5-service-split \
  /home/tommoro/jm_ws/local_data_pipline/curation-tools-docker-split \
  main
```
Expected: `Preparing worktree ... HEAD is now at <hash> <message>`.

- [ ] **Step 3: Sync submodules inside the worktree**

Run:
```bash
cd /home/tommoro/jm_ws/local_data_pipline/curation-tools-docker-split
git submodule update --init --recursive
```
Expected: `rosbag2lerobot-svt` populated.

- [ ] **Step 4: Verify clean state**

Run:
```bash
git status
git branch --show-current
```
Expected: `On branch feat/docker-5-service-split`, `nothing to commit, working tree clean`.

- [ ] **Step 5: Cherry-pick the spec + plan commits across**

The spec and plan files were committed on the original branch (`feat/rosbag2lerobot-svt-converter`) while this worktree was still branching from `main`. Bring both commits over so the files live on the new branch too.

Run (from inside the worktree):
```bash
SPEC_COMMIT=$(git log --all --format=%H -n 1 -- docs/superpowers/specs/2026-04-22-docker-5-service-split-spec1-design.md)
PLAN_COMMIT=$(git log --all --format=%H -n 1 -- docs/superpowers/plans/2026-04-22-docker-5-service-split-spec1.md)
echo "spec=$SPEC_COMMIT plan=$PLAN_COMMIT"
git cherry-pick "$SPEC_COMMIT"
# If the plan was committed separately (common case):
if [[ -n "$PLAN_COMMIT" && "$PLAN_COMMIT" != "$SPEC_COMMIT" ]]; then
    git cherry-pick "$PLAN_COMMIT"
fi
ls docs/superpowers/specs/2026-04-22-docker-5-service-split-spec1-design.md \
   docs/superpowers/plans/2026-04-22-docker-5-service-split-spec1.md
```
Expected: both files present in the worktree, clean commits applied with no conflicts.

If a cherry-pick hits an unrelated conflict (should not happen for doc-only files), abort and report — the rest of the plan assumes Task 0 completes cleanly.

- [ ] **Step 6: (No commit; worktree setup is itself the deliverable.)**

---

## Task 1: docker/compose.yml skeleton (network + volume + db service)

**Purpose:** Create the single compose file and bring up Postgres alone as the first verifiable service.

**Files:**
- Create: `docker/compose.yml`
- Create: `docker/.env` (ignored; populated from example in Task 3)

- [ ] **Step 1: Create compose.yml with db service only**

Write exactly this to `docker/compose.yml`:
```yaml
name: curation-tools

networks:
  curation_net:
    driver: bridge

volumes:
  curation_pg_data:

services:
  db:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_DB: ${POSTGRES_DB:-curation}
      POSTGRES_USER: ${POSTGRES_USER:-curation}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set in docker/.env}
    volumes:
      - curation_pg_data:/var/lib/postgresql/data
      - ./db/init.sql:/docker-entrypoint-initdb.d/10-init.sql:ro
    ports:
      - "${CURATION_PG_HOST_PORT:-127.0.0.1:5433}:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-curation} -d ${POSTGRES_DB:-curation}"]
      interval: 5s
      timeout: 3s
      retries: 10
    networks: [curation_net]
```

Note the host port defaults to `127.0.0.1:5433` (not 5432) so it never collides with a host-installed Postgres.

- [ ] **Step 2: Create a temporary docker/.env for verification**

Write `docker/.env`:
```env
POSTGRES_DB=curation
POSTGRES_USER=curation
POSTGRES_PASSWORD=dev-only-change-me
```

This file is local-only; Task 3 adds it to `.gitignore` and creates the tracked `.env.example`.

- [ ] **Step 3: Verify docker compose config parses (init.sql missing for now)**

Run:
```bash
docker compose -f docker/compose.yml config >/dev/null
```
Expected: no output, exit 0. (This only parses the YAML — it will NOT fail on the missing init.sql file.)

- [ ] **Step 4: Commit**

```bash
git add docker/compose.yml
git commit -m "Add compose skeleton with Postgres db service"
```

---

## Task 2: docker/db/init.sql — schema + jobs table + test database

**Purpose:** First-boot schema for Postgres, including both application tables and the Spec-2/3 `jobs` queue table. Also creates a second database `curation_test` used by pytest.

**Files:**
- Create: `docker/db/init.sql`

- [ ] **Step 1: Write init.sql with complete schema**

Write exactly this to `docker/db/init.sql`:
```sql
-- init.sql — runs on first boot of the Postgres container only.
-- (It does NOT run on subsequent restarts; schema migrations are applied
--  at app startup by backend/core/db.py::init_db().)

-- ---- Create a dedicated test database ---------------------------------
-- The compose-provided database is already created by POSTGRES_DB.
-- We additionally create curation_test for pytest fixtures.
CREATE DATABASE curation_test OWNER curation;

-- ---- schema_versions (replaces SQLite PRAGMA user_version) ------------
\connect curation

CREATE TABLE IF NOT EXISTS schema_versions (
    version    INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---- Application tables (Postgres dialect) ----------------------------
CREATE TABLE IF NOT EXISTS datasets (
    id              SERIAL PRIMARY KEY,
    path            TEXT UNIQUE NOT NULL,
    name            TEXT NOT NULL,
    cell_name       TEXT,
    fps             INTEGER NOT NULL DEFAULT 0,
    total_episodes  INTEGER NOT NULL DEFAULT 0,
    robot_type      TEXT,
    features        JSONB,
    registered_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    synced_at       TIMESTAMPTZ,
    auto_graded_at  TIMESTAMPTZ,
    info_json_mtime DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS dataset_stats (
    dataset_id          INTEGER PRIMARY KEY REFERENCES datasets(id) ON DELETE CASCADE,
    graded_count        INTEGER NOT NULL DEFAULT 0,
    good_count          INTEGER NOT NULL DEFAULT 0,
    normal_count        INTEGER NOT NULL DEFAULT 0,
    bad_count           INTEGER NOT NULL DEFAULT 0,
    total_duration_sec  DOUBLE PRECISION NOT NULL DEFAULT 0,
    good_duration_sec   DOUBLE PRECISION NOT NULL DEFAULT 0,
    normal_duration_sec DOUBLE PRECISION NOT NULL DEFAULT 0,
    bad_duration_sec    DOUBLE PRECISION NOT NULL DEFAULT 0,
    updated_at          TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS episode_serials (
    dataset_id      INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    episode_index   INTEGER NOT NULL,
    serial_number   TEXT NOT NULL,
    PRIMARY KEY (dataset_id, episode_index)
);
CREATE INDEX IF NOT EXISTS idx_episode_serials_serial
    ON episode_serials(serial_number);

CREATE TABLE IF NOT EXISTS annotations (
    serial_number   TEXT PRIMARY KEY,
    grade           TEXT CHECK(grade IN ('good','normal','bad')),
    tags            JSONB NOT NULL DEFAULT '[]'::jsonb,
    reason          TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---- jobs queue (used by Spec-2 converter and Spec-3 curation-worker) -
DO $$ BEGIN
    CREATE TYPE job_type AS ENUM ('convert', 'split', 'merge', 'delete');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE job_status AS ENUM ('pending', 'running', 'done', 'error', 'cancelled');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS jobs (
    id           BIGSERIAL PRIMARY KEY,
    type         job_type   NOT NULL,
    status       job_status NOT NULL DEFAULT 'pending',
    payload      JSONB      NOT NULL DEFAULT '{}'::jsonb,
    result       JSONB,
    error        TEXT,
    attempts     INTEGER    NOT NULL DEFAULT 0,
    worker_id    TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_jobs_pending
    ON jobs(type, created_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_jobs_running
    ON jobs(type, worker_id) WHERE status = 'running';

-- Record initial schema version.
INSERT INTO schema_versions(version) VALUES (1)
    ON CONFLICT (version) DO NOTHING;

-- ---- Mirror the schema into curation_test too -------------------------
\connect curation_test

CREATE TABLE IF NOT EXISTS schema_versions (
    version    INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Test DB intentionally starts empty of application tables;
-- pytest fixtures run init_db() to create them per-session.
```

- [ ] **Step 2: Spin up db and confirm both databases exist**

Run:
```bash
docker compose -f docker/compose.yml up -d db
docker compose -f docker/compose.yml exec -T db pg_isready -U curation
docker compose -f docker/compose.yml exec -T db psql -U curation -d curation \
  -c "SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name;"
docker compose -f docker/compose.yml exec -T db psql -U curation -d curation_test \
  -c "SELECT 1;"
```
Expected:
- `pg_isready` reports `accepting connections`.
- Tables listed: `annotations`, `dataset_stats`, `datasets`, `episode_serials`, `jobs`, `schema_versions`.
- `curation_test` responds `1 row`.

- [ ] **Step 3: Commit**

```bash
git add docker/db/init.sql
git commit -m "Initialize Postgres schema and jobs queue"
```

---

## Task 3: .env.example, .gitignore entries, docs/db-backup scaffold

**Purpose:** Track a template env file, untrack the local `.env` and backup directory contents.

**Files:**
- Create: `docker/.env.example`
- Modify: `.gitignore`
- Create: `docs/db-backup/.gitkeep`
- Create: `docs/db-backup/README.md`

- [ ] **Step 1: Create docker/.env.example**

Write to `docker/.env.example`:
```env
# Host paths
CURATION_DATA_ROOT=/mnt/synology/data/data_div/2026_1
CURATION_UI_PORT=18080

# Postgres (change the password before committing to real environments!)
POSTGRES_DB=curation
POSTGRES_USER=curation
POSTGRES_PASSWORD=change-me-in-env

# Optional: expose Postgres on host for debugging tools.
# Default (127.0.0.1:5433) keeps it local-only; set to e.g. 0.0.0.0:5433 for remote psql.
# CURATION_PG_HOST_PORT=127.0.0.1:5433
```

- [ ] **Step 2: Update .gitignore**

Read the current `.gitignore`, then append:
```
# Local compose env (copy of docker/.env.example with real secrets)
docker/.env

# SQLite metadata backups (created by scripts/backup_sqlite_metadata.sh)
docs/db-backup/**
!docs/db-backup/.gitkeep
!docs/db-backup/README.md
```

- [ ] **Step 3: Create backup dir placeholders**

Create `docs/db-backup/.gitkeep` (empty file).

Create `docs/db-backup/README.md`:
```markdown
# SQLite metadata backups

Generated by `scripts/backup_sqlite_metadata.sh` as part of the Postgres
migration. Each subdirectory is named `YYYYMMDDTHHMMSS/` and contains:

- `host-metadata.db` — copy of `$HOME/.local/share/curation-tools/metadata.db`
- `ui_service_db.tar.gz` — archive of the legacy `ui_service_db` Docker volume
- `MANIFEST.txt` — mtime, size, sha256 of every captured artifact

Contents are git-ignored (see `.gitignore`); only this README is tracked.
```

- [ ] **Step 4: Verify .gitignore works**

Run:
```bash
git status --short
git check-ignore docker/.env docs/db-backup/foo.db
```
Expected: `docker/.env` and `docs/db-backup/foo.db` are both reported ignored; `docker/.env.example`, `docs/db-backup/.gitkeep`, `docs/db-backup/README.md`, `.gitignore` appear as staged-or-untracked for commit.

- [ ] **Step 5: Commit**

```bash
git add docker/.env.example .gitignore docs/db-backup/.gitkeep docs/db-backup/README.md
git commit -m "Add compose env template and SQLite backup scaffolding"
```

---

## Task 4: Add app + nginx services to compose.yml

**Purpose:** Port the existing UI stack (FastAPI `app` + `nginx`) into the single compose file, pointing the app at Postgres via `CURATION_DB_URL`.

**Files:**
- Modify: `docker/compose.yml`

- [ ] **Step 1: Append app and nginx services**

Append to `docker/compose.yml` (under `services:`, after `db:`):
```yaml
  app:
    build:
      context: ..
      dockerfile: docker/ui/Dockerfile.app
    restart: unless-stopped
    depends_on:
      db: { condition: service_healthy }
    environment:
      CURATION_HOST: 0.0.0.0
      CURATION_FASTAPI_PORT: 8001
      CURATION_DB_URL: postgresql://${POSTGRES_USER:-curation}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB:-curation}
      CURATION_DATASET_ROOT_BASE: ${CURATION_DATA_ROOT:-/mnt/synology/data/data_div/2026_1}
      CURATION_RERUN_GRPC_URL: rerun+grpc://rerun:9876
    volumes:
      - ${CURATION_DATA_ROOT:-/mnt/synology/data/data_div/2026_1}:${CURATION_DATA_ROOT:-/mnt/synology/data/data_div/2026_1}
    expose:
      - "8001"
    healthcheck:
      test:
        [
          "CMD",
          "python",
          "-c",
          "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/api/health').read()",
        ]
      interval: 5s
      timeout: 5s
      retries: 10
      start_period: 15s
    networks: [curation_net]

  nginx:
    build:
      context: ..
      dockerfile: docker/ui/Dockerfile.nginx
    depends_on:
      app: { condition: service_healthy }
    restart: unless-stopped
    ports:
      - "${CURATION_UI_PORT:-18080}:80"
    healthcheck:
      test: ["CMD-SHELL", "wget -q -O - http://127.0.0.1/ >/dev/null || exit 1"]
      interval: 5s
      timeout: 5s
      retries: 3
      start_period: 5s
    networks: [curation_net]
```

Note: `CURATION_DB_URL` uses the plain `postgresql://` prefix (not `postgresql+asyncpg://`). The spec previously hinted at the SQLAlchemy style; since we use asyncpg directly with no SQLAlchemy, the driver prefix is unnecessary and would confuse asyncpg's parser. Tasks 10/11 confirm this.

- [ ] **Step 2: Verify compose still parses**

Run:
```bash
docker compose -f docker/compose.yml config >/dev/null
```
Expected: no output, exit 0. (Build is not attempted yet; the current backend still uses SQLite, so do NOT run `up` for `app` until Task 11 completes.)

- [ ] **Step 3: Commit**

```bash
git add docker/compose.yml
git commit -m "Move app and nginx services into unified compose file"
```

---

## Task 5: docker/rerun/Dockerfile + rerun service

**Purpose:** Add the standalone Rerun viewer container. Defer code wiring to Tasks 15–16.

**Files:**
- Create: `docker/rerun/Dockerfile`
- Modify: `docker/compose.yml`

- [ ] **Step 1: Write the Dockerfile**

Write to `docker/rerun/Dockerfile`:
```dockerfile
FROM python:3.13-slim

RUN pip install --no-cache-dir "rerun-sdk>=0.22.0,<0.23.0"

# The `rerun` console script is installed by rerun-sdk; expose both ports.
EXPOSE 9876 9090

# Command is overridden from docker/compose.yml so flags stay visible there.
CMD ["rerun", "--help"]
```

- [ ] **Step 2: Append rerun service to compose.yml**

Append under `services:`:
```yaml
  rerun:
    build:
      context: ..
      dockerfile: docker/rerun/Dockerfile
    restart: unless-stopped
    command:
      [
        "rerun",
        "--serve-web",
        "--web-viewer-port", "9090",
        "--port", "9876",
        "--bind", "0.0.0.0"
      ]
    expose:
      - "9876"
      - "9090"
    volumes:
      - ${CURATION_DATA_ROOT:-/mnt/synology/data/data_div/2026_1}:${CURATION_DATA_ROOT:-/mnt/synology/data/data_div/2026_1}:ro
    networks: [curation_net]
```

- [ ] **Step 3: Build and smoke-test the rerun image**

Run:
```bash
docker compose -f docker/compose.yml build rerun
docker compose -f docker/compose.yml up -d rerun
# wait up to 10s for the listener
for i in 1 2 3 4 5 6 7 8 9 10; do
  docker compose -f docker/compose.yml exec -T rerun sh -c 'nc -z 127.0.0.1 9090 && echo WEB_OK' && break
  sleep 1
done
docker compose -f docker/compose.yml exec -T rerun sh -c 'nc -z 127.0.0.1 9876 && echo GRPC_OK'
```
Expected: `WEB_OK` and `GRPC_OK` printed.

If the `rerun` CLI flag names have changed in the installed version, consult:
```bash
docker compose -f docker/compose.yml run --rm rerun rerun --help
```
and update the `command:` block accordingly.

- [ ] **Step 4: Tear down before commit**

```bash
docker compose -f docker/compose.yml down
```

- [ ] **Step 5: Commit**

```bash
git add docker/rerun/Dockerfile docker/compose.yml
git commit -m "Add standalone rerun viewer container"
```

---

## Task 6: nginx /rerun/ reverse-proxy block

**Purpose:** Proxy the rerun web viewer through nginx so the frontend embeds it at the same origin (no CORS; no extra port exposed on the host).

**Files:**
- Modify: `docker/ui/nginx.conf`

- [ ] **Step 1: Inspect current nginx.conf**

Run:
```bash
cat docker/ui/nginx.conf
```
Identify the existing `server { ... }` block. The `/rerun/` location must live inside that same block.

- [ ] **Step 2: Add /rerun/ location inside the server block**

Insert this block immediately before the existing `location / { ... }` block inside `server { ... }`:
```
    location /rerun/ {
        proxy_pass http://rerun:9090/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_buffering off;
        proxy_read_timeout 1h;
    }
```

- [ ] **Step 3: Validate the resulting nginx config by image build**

Run:
```bash
docker compose -f docker/compose.yml build nginx
```
Expected: build succeeds. The nginx image runs `nginx -t` via its entrypoint on startup — failures surface at boot, not build, so also:
```bash
docker compose -f docker/compose.yml up -d nginx rerun app db
docker compose -f docker/compose.yml logs --tail=50 nginx | grep -iE "error|emerg" || echo "no nginx errors"
```
Expected: `no nginx errors`.

(If `app` fails to start at this point because backend still targets SQLite, that is expected and acceptable for this task — the goal here is only to verify nginx config syntax. `docker compose down` after inspection.)

- [ ] **Step 4: Commit**

```bash
git add docker/ui/nginx.conf
git commit -m "Proxy rerun viewer through nginx /rerun/"
```

---

## Task 7: curation-worker placeholder service

**Purpose:** Stand up the fifth container so Spec-3 has an inhabitable slot. Placeholder just logs every 60 s.

**Files:**
- Create: `docker/curation-worker/Dockerfile`
- Create: `docker/curation-worker/placeholder.py`
- Modify: `docker/compose.yml`

- [ ] **Step 1: Write placeholder.py**

Write to `docker/curation-worker/placeholder.py`:
```python
"""Spec-1 placeholder. Spec-3 replaces this with a real DB-queue consumer."""

import logging
import time


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("curation-worker")
    while True:
        log.info("placeholder: queue consumer not yet implemented (Spec-3)")
        time.sleep(60)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Write the Dockerfile**

Write to `docker/curation-worker/Dockerfile`:
```dockerfile
FROM python:3.13-slim
WORKDIR /app
COPY docker/curation-worker/placeholder.py /app/placeholder.py
CMD ["python", "/app/placeholder.py"]
```

- [ ] **Step 3: Append curation-worker service to compose.yml**

Append under `services:`:
```yaml
  curation-worker:
    build:
      context: ..
      dockerfile: docker/curation-worker/Dockerfile
    profiles: ["curator"]
    restart: unless-stopped
    depends_on:
      db: { condition: service_healthy }
    environment:
      CURATION_DB_URL: postgresql://${POSTGRES_USER:-curation}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB:-curation}
    volumes:
      - ${CURATION_DATA_ROOT:-/mnt/synology/data/data_div/2026_1}:${CURATION_DATA_ROOT:-/mnt/synology/data/data_div/2026_1}
    networks: [curation_net]
```

- [ ] **Step 4: Verify profile gating**

Run:
```bash
docker compose -f docker/compose.yml config --services | sort
docker compose -f docker/compose.yml --profile curator config --services | sort
```
Expected:
- First command lists: `app db nginx rerun` (NO `curation-worker`).
- Second command lists: `app curation-worker db nginx rerun`.

- [ ] **Step 5: Smoke-test placeholder**

Run:
```bash
docker compose -f docker/compose.yml --profile curator up -d db curation-worker
sleep 5
docker compose -f docker/compose.yml logs --tail=5 curation-worker
docker compose -f docker/compose.yml --profile curator down
```
Expected: at least one line containing `queue consumer not yet implemented`.

- [ ] **Step 6: Commit**

```bash
git add docker/curation-worker/ docker/compose.yml
git commit -m "Add curation-worker placeholder behind curator profile"
```

---

## Task 8: Move converter service into unified compose under convert profile

**Purpose:** Consolidate the converter service definition into `docker/compose.yml` but preserve its existing behavior (auto_converter scanning). Spec-2 will later switch it to queue polling.

**Files:**
- Modify: `docker/compose.yml`

- [ ] **Step 1: Read existing converter compose for reference**

Run:
```bash
cat docker/converter/docker-compose.yml
```
Confirm build context, env vars, `mem_limit`, volumes.

- [ ] **Step 2: Append converter service**

Append under `services:`:
```yaml
  converter:
    build:
      context: ..
      dockerfile: docker/converter/Dockerfile
    profiles: ["convert"]
    container_name: convert-server
    restart: unless-stopped
    depends_on:
      db: { condition: service_healthy }
    environment:
      PYTHONUNBUFFERED: "1"
      RAW_BASE: ${CURATION_DATA_ROOT:-/mnt/synology/data/data_div/2026_1}/raw
      LEROBOT_BASE: ${CURATION_DATA_ROOT:-/mnt/synology/data/data_div/2026_1}/lerobot
      SCAN_INTERVAL: "60"
      HZ_MIN_RATIO: "0.7"
      MEMORY_THRESHOLD_PCT: "80"
      VIDEO_ENCODE_WORKERS: "1"
      CURATION_DB_URL: postgresql://${POSTGRES_USER:-curation}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB:-curation}
    volumes:
      - ${CURATION_DATA_ROOT:-/mnt/synology/data/data_div/2026_1}:${CURATION_DATA_ROOT:-/mnt/synology/data/data_div/2026_1}
    mem_limit: 24g
    memswap_limit: 24g
    working_dir: /app
    healthcheck:
      test: ["CMD-SHELL", "test -f /tmp/healthy && find /tmp/healthy -mmin -5 | grep -q ."]
      interval: 60s
      timeout: 10s
      retries: 3
    logging:
      driver: json-file
      options:
        max-size: "50m"
        max-file: "5"
    networks: [curation_net]
```

- [ ] **Step 3: Verify profile gating**

Run:
```bash
docker compose -f docker/compose.yml --profile convert config --services | sort
docker compose -f docker/compose.yml --profile convert --profile curator config --services | sort
```
Expected:
- First lists: `app converter db nginx rerun`.
- Second lists: `app converter curation-worker db nginx rerun`.

- [ ] **Step 4: Commit**

```bash
git add docker/compose.yml
git commit -m "Move converter into unified compose under convert profile"
```

---

## Task 9: Replace aiosqlite with asyncpg in dependencies

**Purpose:** Update the Python package metadata before rewriting `backend/core/db.py`.

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`

- [ ] **Step 1: Edit pyproject.toml dependency list**

In `pyproject.toml`, inside `[project]` → `dependencies`, replace:
```
    "aiosqlite>=0.20.0",
```
with:
```
    "asyncpg>=0.30.0",
```

- [ ] **Step 2: Regenerate the lockfile and sync the local venv**

Run:
```bash
uv lock
uv sync --extra rerun
```
Expected: `uv.lock` updates, `asyncpg` installed into `.venv`, `aiosqlite` removed.

- [ ] **Step 3: Verify import**

Run:
```bash
.venv/bin/python -c "import asyncpg; print(asyncpg.__version__)"
```
Expected: a version like `0.30.x`.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "Swap aiosqlite for asyncpg in backend dependencies"
```

---

## Task 10: Extend Settings with db_url and rerun grpc url

**Purpose:** Add the env-driven fields the rewritten DB layer and rerun wiring will consume.

**Files:**
- Modify: `backend/core/config.py`
- Modify: `tests/test_config.py` (TDD)

- [ ] **Step 1: Write the failing test first**

Append to `tests/test_config.py`:
```python
def test_settings_db_url_defaults_to_local_compose():
    from backend.core.config import Settings

    s = Settings()
    assert s.db_url.startswith("postgresql://"), s.db_url


def test_settings_db_url_overrides_from_env(monkeypatch):
    monkeypatch.setenv("CURATION_DB_URL", "postgresql://u:p@h:5432/d")
    # Re-instantiate because pydantic-settings reads env at construction time.
    from backend.core.config import Settings

    s = Settings()
    assert s.db_url == "postgresql://u:p@h:5432/d"


def test_settings_rerun_grpc_url_defaults():
    from backend.core.config import Settings

    s = Settings()
    assert s.rerun_grpc_url.startswith("rerun+grpc://"), s.rerun_grpc_url
```

- [ ] **Step 2: Run the test and confirm it fails**

Run:
```bash
.venv/bin/pytest tests/test_config.py -k "db_url or rerun_grpc_url" -v
```
Expected: all three tests fail with `AttributeError` (fields not defined).

- [ ] **Step 3: Add the fields to Settings**

In `backend/core/config.py`, inside `class Settings(BaseSettings):`, add (below the existing `rerun_web_port: int = 9090` line):
```python
    db_url: str = "postgresql://curation:change-me-in-env@localhost:5433/curation"
    rerun_grpc_url: str = "rerun+grpc://127.0.0.1:9876"
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run:
```bash
.venv/bin/pytest tests/test_config.py -k "db_url or rerun_grpc_url" -v
```
Expected: all three tests pass.

- [ ] **Step 5: Commit**

```bash
git add backend/core/config.py tests/test_config.py
git commit -m "Add db_url and rerun_grpc_url to Settings"
```

---

## Task 11: Rewrite backend/core/db.py with asyncpg + thin wrapper

**Purpose:** Replace the SQLite singleton with a Postgres connection pool and a compatibility layer so existing call sites can migrate mechanically.

**Files:**
- Rewrite: `backend/core/db.py`
- Create: `tests/conftest.py` (a new session-scoped pool pointing at `curation_test`)
- Modify: `tests/test_db.py`

### The wrapper contract (matches what existing code already uses)

Existing call sites do things like:
```python
db = await get_db()
async with db.execute("SELECT ... WHERE x = ?", (x,)) as cur:
    row = await cur.fetchone()
await db.execute("INSERT ... VALUES (?, ?)", (a, b))
await db.commit()
```

The new wrapper preserves that exact shape: `get_db()` returns a `_DB` object with `.execute(sql, params)` returning an awaitable async-context-manager yielding an object with `.fetchone()` / `.fetchall()`, and a no-op `.commit()` (asyncpg auto-commits outside explicit transactions). Inside a `transaction()` block, `.commit()` still no-ops but the block controls the transaction boundary via asyncpg.

- [ ] **Step 1: Write the failing test**

Replace the contents of `tests/test_db.py` with:
```python
"""Smoke tests for the Postgres metadata layer."""

import pytest

from backend.core.db import close_db, get_db, init_db, _reset


@pytest.fixture(autouse=True)
async def reset_db():
    _reset()
    await init_db()
    yield
    await close_db()


async def test_schema_tables_exist():
    db = await get_db()
    async with db.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' ORDER BY table_name"
    ) as cur:
        names = [row["table_name"] for row in await cur.fetchall()]
    for expected in ("annotations", "dataset_stats", "datasets",
                     "episode_serials", "jobs", "schema_versions"):
        assert expected in names, names


async def test_placeholder_substitution():
    db = await get_db()
    async with db.execute("SELECT $1::int + $2::int AS s", (2, 3)) as cur:
        row = await cur.fetchone()
    assert row["s"] == 5


async def test_question_mark_placeholders_translate():
    db = await get_db()
    async with db.execute("SELECT ?::int AS n", (7,)) as cur:
        row = await cur.fetchone()
    assert row["n"] == 7


async def test_insert_and_read_dataset():
    db = await get_db()
    await db.execute(
        "INSERT INTO datasets(path, name) VALUES (?, ?)",
        ("/tmp/x", "x"),
    )
    async with db.execute("SELECT name FROM datasets WHERE path=?", ("/tmp/x",)) as cur:
        row = await cur.fetchone()
    assert row["name"] == "x"
```

- [ ] **Step 2: Write the session-scoped test fixture**

Write to `tests/conftest.py`:
```python
"""Shared pytest fixtures — point the DB layer at curation_test."""

import os

import pytest


@pytest.fixture(autouse=True, scope="session")
def _point_settings_at_test_db():
    url = os.environ.get(
        "CURATION_TEST_DB_URL",
        "postgresql://curation:dev-only-change-me@127.0.0.1:5433/curation_test",
    )
    os.environ["CURATION_DB_URL"] = url
    # pydantic-settings reads env at construction, not import, so we must also
    # rebuild the module-level `settings` instance used by backend.core.db.
    from backend.core import config as _cfg
    _cfg.settings = _cfg.Settings()
    yield
```

- [ ] **Step 3: Run the test and confirm it fails**

Ensure db is up:
```bash
docker compose -f docker/compose.yml up -d db
```
Then:
```bash
.venv/bin/pytest tests/test_db.py -v
```
Expected: all tests fail — import of `aiosqlite.Connection` or similar breaks, or `get_db` still returns an aiosqlite connection.

- [ ] **Step 4: Rewrite backend/core/db.py**

Replace the entire file with:
```python
"""Postgres metadata layer — asyncpg pool and a thin wrapper that preserves
the original aiosqlite-style call sites (?-placeholders, execute-as-ctxmgr).
"""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Iterable, Sequence

import asyncpg

from backend.core.config import settings

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None
_db_url_override: str | None = None  # for tests


# ---- Placeholder translation ----------------------------------------------
# Existing code uses SQLite-style "?" placeholders. asyncpg requires "$1", "$2".
# This regex walks the query and rewrites ? to $N, ignoring ? inside literals.

_QMARK_RE = re.compile(r"\?")


def _translate(sql: str) -> str:
    # Quick path: nothing to do.
    if "?" not in sql:
        return sql
    out = []
    i = 0
    n = 1
    in_single = False
    in_double = False
    while i < len(sql):
        ch = sql[i]
        if ch == "'" and not in_double:
            in_single = not in_single
            out.append(ch)
        elif ch == '"' and not in_single:
            in_double = not in_double
            out.append(ch)
        elif ch == "?" and not in_single and not in_double:
            out.append(f"${n}")
            n += 1
        else:
            out.append(ch)
        i += 1
    return "".join(out)


# ---- Cursor-like wrapper --------------------------------------------------


class _Cursor:
    """Emulates aiosqlite's async cursor for existing call sites."""

    def __init__(self, rows: Sequence[asyncpg.Record]):
        self._rows = rows

    async def fetchone(self) -> asyncpg.Record | None:
        return self._rows[0] if self._rows else None

    async def fetchall(self) -> list[asyncpg.Record]:
        return list(self._rows)

    async def __aenter__(self) -> "_Cursor":
        return self

    async def __aexit__(self, *exc) -> None:
        return None


class _ExecuteAwaitable:
    """Returned by _DB.execute — awaitable for fire-and-forget, async-ctx for fetch."""

    def __init__(self, conn: asyncpg.Connection, sql: str, params: tuple[Any, ...]):
        self._conn = conn
        self._sql = _translate(sql)
        self._params = params
        self._rows: list[asyncpg.Record] | None = None

    def __await__(self):
        return self._run().__await__()

    async def _run(self) -> _Cursor:
        # Detect SELECT / RETURNING by the translated SQL.
        stripped = self._sql.lstrip().lower()
        if stripped.startswith("select") or " returning " in f" {stripped} ":
            rows = await self._conn.fetch(self._sql, *self._params)
        else:
            await self._conn.execute(self._sql, *self._params)
            rows = []
        return _Cursor(rows)

    async def __aenter__(self) -> _Cursor:
        cur = await self._run()
        return cur

    async def __aexit__(self, *exc) -> None:
        return None


# ---- Connection facade ----------------------------------------------------


class _DB:
    """Acquires a pool connection on demand and mimics aiosqlite.Connection."""

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool
        self._txn_conn: asyncpg.Connection | None = None

    def execute(self, sql: str, params: Iterable[Any] = ()) -> _ExecuteAwaitable:
        if self._txn_conn is not None:
            return _ExecuteAwaitable(self._txn_conn, sql, tuple(params))
        # Off-transaction: acquire a dedicated conn per statement via a helper.
        return _ExecuteAwaitable(_PooledConn(self._pool), sql, tuple(params))

    async def executescript(self, sql: str) -> None:
        # Used by legacy migrations. asyncpg executes multi-statements via
        # connection.execute when they are separated by `;`.
        async with self._pool.acquire() as conn:
            await conn.execute(sql)

    async def commit(self) -> None:
        # asyncpg auto-commits outside transactions; this is a no-op kept for API compat.
        return None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator["_DB"]:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                self._txn_conn = conn
                try:
                    yield self
                finally:
                    self._txn_conn = None


class _PooledConn:
    """Transient wrapper giving asyncpg.fetch/execute a connection per call.
    Only used via _ExecuteAwaitable when no transaction is active.
    """

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def fetch(self, sql: str, *params: Any) -> list[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            return await conn.fetch(sql, *params)

    async def execute(self, sql: str, *params: Any) -> str:
        async with self._pool.acquire() as conn:
            return await conn.execute(sql, *params)


# ---- Lifecycle ------------------------------------------------------------


def _effective_url() -> str:
    return _db_url_override or settings.db_url


async def get_db() -> _DB:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(dsn=_effective_url(), min_size=1, max_size=10)
    return _DB(_pool)


async def close_db() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def init_db() -> None:
    """Create application tables if missing and record the current schema version.

    Postgres runs `docker/db/init.sql` on first boot for compose deployments.
    This function covers two remaining cases: (a) pytest using curation_test,
    which is empty on creation, and (b) future in-process migrations.
    """
    db = await get_db()
    await db.executescript(_SCHEMA_V1)
    await db.execute(
        "INSERT INTO schema_versions(version) VALUES (1) ON CONFLICT DO NOTHING"
    )


def _reset() -> None:
    """Test helper: forget the cached pool so the next get_db() rebuilds it."""
    global _pool, _db_url_override
    _pool = None
    _db_url_override = None


_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_versions (
    version    INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS datasets (
    id              SERIAL PRIMARY KEY,
    path            TEXT UNIQUE NOT NULL,
    name            TEXT NOT NULL,
    cell_name       TEXT,
    fps             INTEGER NOT NULL DEFAULT 0,
    total_episodes  INTEGER NOT NULL DEFAULT 0,
    robot_type      TEXT,
    features        JSONB,
    registered_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    synced_at       TIMESTAMPTZ,
    auto_graded_at  TIMESTAMPTZ,
    info_json_mtime DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS dataset_stats (
    dataset_id          INTEGER PRIMARY KEY REFERENCES datasets(id) ON DELETE CASCADE,
    graded_count        INTEGER NOT NULL DEFAULT 0,
    good_count          INTEGER NOT NULL DEFAULT 0,
    normal_count        INTEGER NOT NULL DEFAULT 0,
    bad_count           INTEGER NOT NULL DEFAULT 0,
    total_duration_sec  DOUBLE PRECISION NOT NULL DEFAULT 0,
    good_duration_sec   DOUBLE PRECISION NOT NULL DEFAULT 0,
    normal_duration_sec DOUBLE PRECISION NOT NULL DEFAULT 0,
    bad_duration_sec    DOUBLE PRECISION NOT NULL DEFAULT 0,
    updated_at          TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS episode_serials (
    dataset_id      INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    episode_index   INTEGER NOT NULL,
    serial_number   TEXT NOT NULL,
    PRIMARY KEY (dataset_id, episode_index)
);

CREATE INDEX IF NOT EXISTS idx_episode_serials_serial
    ON episode_serials(serial_number);

CREATE TABLE IF NOT EXISTS annotations (
    serial_number   TEXT PRIMARY KEY,
    grade           TEXT CHECK(grade IN ('good','normal','bad')),
    tags            JSONB NOT NULL DEFAULT '[]'::jsonb,
    reason          TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DO $$ BEGIN
    CREATE TYPE job_type AS ENUM ('convert', 'split', 'merge', 'delete');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE job_status AS ENUM ('pending', 'running', 'done', 'error', 'cancelled');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS jobs (
    id           BIGSERIAL PRIMARY KEY,
    type         job_type   NOT NULL,
    status       job_status NOT NULL DEFAULT 'pending',
    payload      JSONB      NOT NULL DEFAULT '{}'::jsonb,
    result       JSONB,
    error        TEXT,
    attempts     INTEGER    NOT NULL DEFAULT 0,
    worker_id    TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_jobs_pending
    ON jobs(type, created_at) WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_jobs_running
    ON jobs(type, worker_id) WHERE status = 'running';
"""
```

- [ ] **Step 5: Run the tests and confirm they pass**

Run:
```bash
.venv/bin/pytest tests/test_db.py tests/test_config.py -v
```
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add backend/core/db.py tests/conftest.py tests/test_db.py
git commit -m "Rewrite metadata layer on asyncpg with ?-placeholder shim"
```

---

## Task 12: Migrate backend call sites (routers + services)

**Purpose:** Every place that previously imported `aiosqlite` or used `.executescript` in an SQLite-specific way needs inspection. Because the wrapper in Task 11 preserves the `?`-placeholder call shape, most sites need no change at all.

**Files to inspect:**
- `backend/main.py`
- `backend/datasets/services/cell_service.py`
- `backend/datasets/services/episode_service.py`
- `backend/datasets/services/auto_grade_service.py`
- `backend/datasets/services/dataset_service.py`
- `backend/datasets/services/task_service.py`
- `backend/datasets/services/episode_rows.py` (if present — untracked in current state)
- `backend/datasets/services/task_parquet.py` (same)
- `backend/datasets/routers/datasets.py`
- `backend/datasets/routers/cells.py`
- `backend/datasets/routers/scalars.py`
- `backend/converter/router.py`
- `backend/converter/validation_service.py`

### Migration pattern

**Pattern A — call sites using `?` placeholders with tuple params:** no change needed.

Example before (stays as-is):
```python
async with db.execute(
    "SELECT id FROM datasets WHERE path = ?",
    (path,),
) as cur:
    row = await cur.fetchone()
```

**Pattern B — sites using `aiosqlite.Row` attribute access:** no change, `asyncpg.Record` supports dict-style (`row["col"]`) and tuple-style (`row[0]`) identically.

**Pattern C — sites using SQLite-only functions:** rewrite. Specifically:
- `strftime(...)` in SQL → `NOW()` (timestamps) or remove (defaults are now server-side).
- `PRAGMA user_version` → replace with `SELECT COALESCE(MAX(version), 0) FROM schema_versions`.
- `PRAGMA table_info(...)` → replace with a catalog query (only used internally by `backend/core/db.py` — already handled in Task 11).
- `INSERT OR REPLACE` → `INSERT ... ON CONFLICT (...) DO UPDATE SET ...`.
- `INSERT OR IGNORE` → `INSERT ... ON CONFLICT DO NOTHING`.
- Any `LAST_INSERT_ROWID()` → use `RETURNING id` on the INSERT.

**Pattern D — explicit `db.commit()` calls:** leave in place; the wrapper's `.commit()` is a no-op.

- [ ] **Step 1: Scan for SQLite-only SQL fragments**

Run:
```bash
grep -RInE "strftime\(|INSERT OR REPLACE|INSERT OR IGNORE|LAST_INSERT_ROWID|PRAGMA " \
  backend/ --include="*.py"
```
Collect the list. Zero hits is the ideal outcome; expect 0–3 hits in practice (the legacy schema migrations inside `backend/core/db.py` already moved out in Task 11).

- [ ] **Step 2: Rewrite any hits using Pattern C**

For each hit, apply the translation above. Keep the original SQL readable.

Example:
```python
# before
await db.execute(
    "INSERT OR REPLACE INTO dataset_stats(dataset_id, graded_count) VALUES (?, ?)",
    (dataset_id, count),
)

# after
await db.execute(
    "INSERT INTO dataset_stats(dataset_id, graded_count) VALUES (?, ?) "
    "ON CONFLICT (dataset_id) DO UPDATE SET graded_count = EXCLUDED.graded_count",
    (dataset_id, count),
)
```

- [ ] **Step 3: Run the full backend test suite**

Ensure db is running, then:
```bash
.venv/bin/pytest tests/ -v --ignore=tests/test_legacy_annotation_migration.py \
  --ignore=tests/test_sidecar_migration_v4.py \
  --ignore=tests/test_cell_service_legacy_db.py
```
(The three ignored files are SQLite-version-specific legacy migrations that Task 13 removes.)

Expected: all remaining tests pass. Where tests fail due to SQLite-only SQL in production code, revisit Step 2 and add any missed rewrites.

- [ ] **Step 4: Commit**

```bash
git add backend/
git commit -m "Rewrite SQLite-only SQL fragments to Postgres dialect"
```

---

## Task 13: Remove obsolete SQLite-migration tests

**Purpose:** Three existing test files specifically exercise SQLite v3→v4 migration paths that no longer apply; remove them rather than port them.

**Files:**
- Delete: `tests/test_legacy_annotation_migration.py`
- Delete: `tests/test_sidecar_migration_v4.py`
- Delete: `tests/test_cell_service_legacy_db.py`

- [ ] **Step 1: Confirm each file's scope before deleting**

Run:
```bash
head -30 tests/test_legacy_annotation_migration.py \
  tests/test_sidecar_migration_v4.py \
  tests/test_cell_service_legacy_db.py
```
Expected: each file's docstring/imports reference v1/v2/v3 SQLite schemas or `SCHEMA_V1`/`SCHEMA_V2`/`SCHEMA_V3` constants no longer defined in the new `db.py`.

- [ ] **Step 2: Delete and confirm the rest of the suite still collects**

Run:
```bash
git rm tests/test_legacy_annotation_migration.py \
       tests/test_sidecar_migration_v4.py \
       tests/test_cell_service_legacy_db.py
.venv/bin/pytest tests/ --collect-only -q | tail -5
```
Expected: collection succeeds, count drops by the removed test cases.

- [ ] **Step 3: Commit**

```bash
git commit -m "Remove SQLite-specific legacy migration tests"
```

---

## Task 14: Migrate remaining DB tests (question-mark style stays; aiosqlite imports removed)

**Purpose:** One test file imports `aiosqlite` directly and must be adjusted. All other DB tests work as-is because they go through the wrapper.

**Files:**
- Modify: `tests/test_grade_reason.py`

- [ ] **Step 1: Inspect the aiosqlite import**

Run:
```bash
grep -n "aiosqlite" tests/test_grade_reason.py
```
Locate any direct `aiosqlite.connect(...)` usage.

- [ ] **Step 2: Replace the import and connection logic**

Two cases:

**Case A — the file only imports `aiosqlite.Row` or the type annotation:** delete the import entirely; no other change needed (the wrapper returns `asyncpg.Record` which supports the same interface).

**Case B — the file opens its own `aiosqlite.connect(...)`:** rewrite to use `get_db()` from `backend.core.db`. Concretely, replace:
```python
import aiosqlite
...
async with aiosqlite.connect(str(db_path)) as conn:
    conn.row_factory = aiosqlite.Row
    async with conn.execute("SELECT ...") as cur:
        row = await cur.fetchone()
```
with:
```python
from backend.core.db import get_db

db = await get_db()
async with db.execute("SELECT ...") as cur:
    row = await cur.fetchone()
```

Paths referenced in the old test (like `db_path`) can be dropped — the wrapper already points at `curation_test` via the fixture.

- [ ] **Step 3: Run the full test suite**

Run:
```bash
.venv/bin/pytest tests/ -v
```
Expected: every test passes. If a test still fails, inspect the failure and apply Pattern C from Task 12 if the offender is SQLite-specific SQL.

- [ ] **Step 4: Commit**

```bash
git add tests/test_grade_reason.py
git commit -m "Route test_grade_reason through the new DB wrapper"
```

---

## Task 15: Point rerun_service at the containerized rerun (gRPC URL)

**Purpose:** The canonical rerun service lives at `backend/datasets/services/rerun_service.py`. Make it read `CURATION_RERUN_GRPC_URL` so the backend logs to the `rerun` container instead of spawning a local viewer.

**Files:**
- Modify: `backend/datasets/services/rerun_service.py`
- Modify: `tests/` (smoke test — if no rerun test exists, skip this step)

- [ ] **Step 1: Find the current connection code**

Run:
```bash
grep -n "spawn\|serve\|connect\|RecordingStream\|rerun.init\|rerun.connect" \
  backend/datasets/services/rerun_service.py | head -20
```

- [ ] **Step 2: Rewire to the env-configured gRPC URL**

Locate the function that establishes the rerun connection (commonly named `init_rerun` or similar). Replace any `rr.spawn()` / `rr.serve()` call with:
```python
import rerun as rr
from backend.core.config import settings

# previously: rr.spawn() or rr.serve()
rr.init("curation-tools")
rr.connect(settings.rerun_grpc_url)
```

If the rerun SDK version 0.22 in use renamed `connect` → `connect_grpc`, prefer `connect_grpc`. Verify with:
```bash
.venv/bin/python -c "import rerun; print([a for a in dir(rerun) if 'connect' in a or 'serve' in a])"
```

- [ ] **Step 3: Verify the import path still works**

Run:
```bash
.venv/bin/python -c "from backend.datasets.services.rerun_service import init_rerun; print('ok')"
```
Expected: `ok`.

- [ ] **Step 4: Commit**

```bash
git add backend/datasets/services/rerun_service.py
git commit -m "Point rerun_service at the containerized gRPC endpoint"
```

---

## Task 16: Point RerunViewer.tsx at /rerun/ proxy

**Purpose:** The frontend iframe must target the same origin as the app, through the nginx proxy added in Task 6.

**Files:**
- Modify: `frontend/src/components/RerunViewer.tsx`

- [ ] **Step 1: Find the current iframe src**

Run:
```bash
grep -n "iframe\|src\|rerun" frontend/src/components/RerunViewer.tsx | head -10
```

- [ ] **Step 2: Change the src to the relative proxy path**

Replace the existing src value with `/rerun/` so the browser hits nginx directly. Example:
```tsx
// before
<iframe src={`http://${host}:9090/?url=${encodeURIComponent(rrdUrl)}`} ... />

// after
<iframe src={`/rerun/?url=${encodeURIComponent(rrdUrl)}`} ... />
```

If the original code had a prop/env override for the host, keep the prop for dev mode but default to the proxy path when unset.

- [ ] **Step 3: Build the frontend to confirm no type errors**

Run:
```bash
(cd frontend && npm run build) >/tmp/fe-build.log 2>&1 && echo "build OK" || { tail -50 /tmp/fe-build.log; exit 1; }
```
Expected: `build OK`.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/RerunViewer.tsx frontend/dist
git commit -m "Embed rerun viewer through the nginx /rerun/ proxy"
```

---

## Task 17: SQLite metadata backup script

**Purpose:** Preserve any existing SQLite content from the prior setup before anyone blows away local state.

**Files:**
- Create: `scripts/backup_sqlite_metadata.sh`

- [ ] **Step 1: Write the script**

Write to `scripts/backup_sqlite_metadata.sh`:
```bash
#!/usr/bin/env bash
set -euo pipefail

# backup_sqlite_metadata.sh — snapshot all known SQLite metadata locations
# into docs/db-backup/<timestamp>/ so they can be revisited after the
# Postgres migration.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

TS="$(date +%Y%m%dT%H%M%S)"
DEST="$REPO_ROOT/docs/db-backup/$TS"
mkdir -p "$DEST"

MANIFEST="$DEST/MANIFEST.txt"
: > "$MANIFEST"

record() {
  local path="$1"
  local size mtime sha
  size=$(stat -c %s "$path" 2>/dev/null || echo "?")
  mtime=$(stat -c %y "$path" 2>/dev/null || echo "?")
  sha=$(sha256sum "$path" 2>/dev/null | awk '{print $1}')
  printf "%s\n  size=%s\n  mtime=%s\n  sha256=%s\n\n" \
    "$path" "$size" "$mtime" "$sha" >> "$MANIFEST"
}

# 1) Host default path
HOST_DB="$HOME/.local/share/curation-tools/metadata.db"
if [[ -f "$HOST_DB" ]]; then
  cp -a "$HOST_DB" "$DEST/host-metadata.db"
  record "$DEST/host-metadata.db"
  echo "Backed up: $HOST_DB"
fi

# 2) $CURATION_DB_PATH override, if set and different from default
if [[ -n "${CURATION_DB_PATH:-}" && -f "$CURATION_DB_PATH" && "$CURATION_DB_PATH" != "$HOST_DB" ]]; then
  cp -a "$CURATION_DB_PATH" "$DEST/env-metadata.db"
  record "$DEST/env-metadata.db"
  echo "Backed up: $CURATION_DB_PATH"
fi

# 3) Legacy docker volume ui_service_db
if docker volume inspect ui_service_db >/dev/null 2>&1; then
  docker run --rm -v ui_service_db:/src:ro -v "$DEST":/out alpine \
    tar czf /out/ui_service_db.tar.gz -C /src .
  record "$DEST/ui_service_db.tar.gz"
  echo "Backed up: docker volume ui_service_db"
fi

if [[ ! -s "$MANIFEST" ]]; then
  echo "No SQLite artifacts found; nothing to back up."
  rmdir "$DEST"
  exit 0
fi

echo ""
echo "Backup complete: $DEST"
echo "Manifest:"
cat "$MANIFEST"
```

- [ ] **Step 2: Make it executable and smoke-test**

Run:
```bash
chmod +x scripts/backup_sqlite_metadata.sh
scripts/backup_sqlite_metadata.sh
ls -la docs/db-backup/ | head -10
```
Expected: either a new `YYYYMMDDTHHMMSS/` directory with a `MANIFEST.txt`, or the "nothing to back up" message with no new directory.

- [ ] **Step 3: Confirm the backup dir stays gitignored**

Run:
```bash
git status --short docs/db-backup/
```
Expected: only `.gitkeep` and `README.md` show up; any newly created timestamped directory is ignored.

- [ ] **Step 4: Commit**

```bash
git add scripts/backup_sqlite_metadata.sh
git commit -m "Add SQLite metadata backup script"
```

---

## Task 18: Rewrite main.sh around the unified compose

**Purpose:** Replace the current two-stack launcher (written earlier this session) with one that uses `docker/compose.yml` and the `convert`/`curator` profiles.

**Files:**
- Overwrite: `main.sh`

Note: in this worktree, `main.sh` at HEAD is the pre-existing version (from `main` branch). The two-stack launcher that was written on `feat/rosbag2lerobot-svt-converter` does NOT exist in this worktree; nothing is lost by overwriting.

- [ ] **Step 1: Write the new main.sh**

Overwrite `main.sh`:
```bash
#!/bin/bash
set -euo pipefail

# =============================================================================
# main.sh — Curation Tools Docker Launcher (unified compose, 5 services)
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/docker/compose.yml"
ENV_FILE="$SCRIPT_DIR/docker/.env"
ENV_EXAMPLE="$SCRIPT_DIR/docker/.env.example"
PROJECT_NAME="curation-tools"

export DOCKER_BUILDKIT=1
export COMPOSE_DOCKER_CLI_BUILD=1

dc() { docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"; }
log() { echo "[$(date '+%H:%M:%S')] $*"; }

ensure_env() {
  if [[ ! -f "$ENV_FILE" ]]; then
    if [[ -f "$ENV_EXAMPLE" ]]; then
      cp "$ENV_EXAMPLE" "$ENV_FILE"
      log "Created $ENV_FILE from .env.example — edit POSTGRES_PASSWORD before real use."
    else
      echo "ERROR: both $ENV_FILE and $ENV_EXAMPLE are missing." >&2
      exit 1
    fi
  fi
}

preflight() {
  command -v docker >/dev/null 2>&1 || { echo "ERROR: docker not found"; return 1; }
  docker compose version >/dev/null 2>&1 || { echo "ERROR: 'docker compose' plugin missing"; return 1; }
  [[ -f "$COMPOSE_FILE" ]] || { echo "ERROR: missing $COMPOSE_FILE"; return 1; }
  ensure_env
}

service_running() {
  dc ps --format '{{.Service}}\t{{.State}}' 2>/dev/null | awk -v s="$1" '$1==s && $2=="running" {found=1} END{exit !found}'
}

status_dot() { service_running "$1" && echo '●' || echo '○'; }

show_menu() {
  local s_app s_ng s_db s_rr s_cv s_cw
  s_app=$(status_dot app); s_ng=$(status_dot nginx); s_db=$(status_dot db)
  s_rr=$(status_dot rerun); s_cv=$(status_dot converter); s_cw=$(status_dot curation-worker)
  local ui_port="${CURATION_UI_PORT:-18080}"
  local data_root="${CURATION_DATA_ROOT:-/mnt/synology/data/data_div/2026_1}"
  echo ""
  echo "============================================"
  echo "  Curation Tools — Docker Launcher"
  echo "  Status: app $s_app nginx $s_ng db $s_db rerun $s_rr | converter $s_cv curator $s_cw"
  echo "  UI: http://localhost:$ui_port/   Data: $data_root"
  echo "============================================"
  echo " Core"
  echo "  1) Build all images"
  echo "  2) Build all (no-cache)"
  echo "  3) Up (default: app + nginx + db + rerun)"
  echo "  4) Up + convert profile"
  echo "  5) Up + curator profile"
  echo "  6) Up everything (all profiles)"
  echo "  7) Down (stop all, keep volumes)"
  echo ""
  echo " Logs & Shell"
  echo "  8) Logs — all (follow)"
  echo "  9) Logs — pick service"
  echo " 10) Shell — app"
  echo " 11) Shell — converter"
  echo " 12) Shell — curation-worker"
  echo " 13) psql — db"
  echo ""
  echo " Maintenance"
  echo " 14) Backup SQLite metadata  (docs/db-backup/)"
  echo " 15) Reset DB  (drop volume, re-init)  [confirm]"
  echo ""
  echo "  0) Exit (or ESC)"
  echo "--------------------------------------------"
  echo -n "  Choice: "
}

# ----- Action handlers --------------------------------------------------------

do_build_all()      { preflight; dc --profile convert --profile curator build; }
do_build_nocache()  { preflight; dc --profile convert --profile curator build --no-cache; }
do_up_default()     { preflight; dc up -d; log "UI: http://localhost:${CURATION_UI_PORT:-18080}/"; }
do_up_convert()     { preflight; dc --profile convert up -d; }
do_up_curator()     { preflight; dc --profile curator up -d; }
do_up_all()         { preflight; dc --profile convert --profile curator up -d; }
do_down()           { preflight; dc --profile convert --profile curator down; }
do_logs_all()       { preflight; dc logs -f --tail=100 || true; }

do_logs_pick() {
  preflight
  echo "Services: app nginx db rerun converter curation-worker"
  read -rp "  Which: " svc
  [[ -n "$svc" ]] && dc logs -f --tail=200 "$svc" || true
}

do_shell() {
  preflight
  local svc="$1"
  if service_running "$svc"; then
    dc exec "$svc" bash || dc exec "$svc" sh
  else
    log "$svc not running — starting a one-off shell..."
    dc --profile convert --profile curator run --rm --entrypoint bash "$svc" \
      || dc --profile convert --profile curator run --rm --entrypoint sh "$svc"
  fi
}

do_psql() {
  preflight
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  if service_running db; then
    dc exec db psql -U "${POSTGRES_USER:-curation}" -d "${POSTGRES_DB:-curation}"
  else
    log "db not running — starting it first..."
    dc up -d db
    dc exec db psql -U "${POSTGRES_USER:-curation}" -d "${POSTGRES_DB:-curation}"
  fi
}

do_backup_sqlite() {
  preflight
  bash "$SCRIPT_DIR/scripts/backup_sqlite_metadata.sh"
}

do_reset_db() {
  preflight
  local auto_yes="${1:-}"
  if [[ "$auto_yes" != "--yes" ]]; then
    read -rp "This DROPS the Postgres volume and reinitializes the schema. Continue? (y/N) " ans
    [[ "$ans" == "y" || "$ans" == "Y" ]] || { log "Aborted."; return; }
  fi
  dc --profile convert --profile curator down
  docker volume rm "${PROJECT_NAME}_curation_pg_data" 2>/dev/null || true
  dc up -d db
  log "DB volume recreated and re-initialized from docker/db/init.sql."
}

# ----- Non-interactive flags --------------------------------------------------

case "${1:-}" in
  --up)             do_up_default;        exit $? ;;
  --up-convert)     do_up_convert;        exit $? ;;
  --up-curator)     do_up_curator;        exit $? ;;
  --up-all)         do_up_all;            exit $? ;;
  --down)           do_down;              exit $? ;;
  --build)          do_build_all;         exit $? ;;
  --build-nocache)  do_build_nocache;     exit $? ;;
  --logs)           preflight; dc logs -f --tail=200 "${2:-}"; exit $? ;;
  --shell)          do_shell "${2:?service required}"; exit $? ;;
  --psql)           do_psql;              exit $? ;;
  --backup-sqlite)  do_backup_sqlite;     exit $? ;;
  --reset-db)       do_reset_db "--yes";  exit $? ;;
  "")               ;;  # interactive
  *)                echo "Unknown flag: $1"; exit 2 ;;
esac

# ----- Interactive menu loop --------------------------------------------------

while true; do
  show_menu
  read -rsn1 c
  [[ "$c" == $'\e' ]] && { echo -e "\n[ESC] Exit"; break; }
  echo ""
  case "$c" in
    1) do_build_all ;;
    2) do_build_nocache ;;
    3) do_up_default ;;
    4) do_up_convert ;;
    5) do_up_curator ;;
    6) do_up_all ;;
    7) do_down ;;
    8) do_logs_all ;;
    9) do_logs_pick ;;
    10) do_shell app ;;
    11) do_shell converter ;;
    12) do_shell curation-worker ;;
    13) do_psql ;;
    14) do_backup_sqlite ;;
    15) do_reset_db ;;
    0) echo "Exit"; break ;;
    *) echo "Invalid choice: '$c'" ;;
  esac
  echo ""
done
```

- [ ] **Step 2: Permission + syntax check**

Run:
```bash
chmod +x main.sh
bash -n main.sh && echo "syntax OK"
```
Expected: `syntax OK`.

- [ ] **Step 3: Smoke the non-interactive paths**

Run:
```bash
./main.sh --up
./main.sh --down
```
Expected: first command brings up `app nginx db rerun`, prints the UI URL; second tears everything down cleanly.

- [ ] **Step 4: Commit**

```bash
git add main.sh
git commit -m "Rewrite main.sh around the unified compose file"
```

---

## Task 19: Retire the legacy split compose files

**Purpose:** Now that `docker/compose.yml` is authoritative, delete the two legacy compose files to prevent drift.

**Files:**
- Delete: `docker/ui/docker-compose.yml`
- Delete: `docker/converter/docker-compose.yml`

- [ ] **Step 1: Ensure no references remain**

Run:
```bash
grep -RIn "docker/ui/docker-compose\|docker/converter/docker-compose" \
  --include="*.md" --include="*.sh" --include="*.py" --include="*.yml" \
  --include="*.yaml" --include="*.ts" --include="*.tsx" \
  --exclude-dir=node_modules --exclude-dir=.git .
```
Expected: zero hits, or only inside `docs/superpowers/specs/` and `docs/superpowers/plans/` (historical design docs — leave untouched).

If any non-doc reference exists, update it to `docker/compose.yml`.

- [ ] **Step 2: Delete**

Run:
```bash
git rm docker/ui/docker-compose.yml docker/converter/docker-compose.yml
```

- [ ] **Step 3: Commit**

```bash
git commit -m "Retire legacy per-stack compose files"
```

---

## Task 20: Update start.sh hybrid mode (native backend + containerized db/rerun)

**Purpose:** Preserve the native-dev workflow by launching Postgres (and optionally Rerun) via compose, while the backend and frontend run on the host as before.

**Files:**
- Modify: `start.sh`

- [ ] **Step 1: Overwrite start.sh**

Write:
```bash
#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 1) Make sure Postgres (and rerun) are running via compose.
if ! docker compose -f docker/compose.yml ps db --format '{{.State}}' 2>/dev/null | grep -q running; then
    echo "Starting Postgres via docker compose..."
    bash main.sh --up >/dev/null
fi

# 2) Native Python env.
if [ ! -d ".venv" ]; then
    echo "Setting up Python environment..."
    if ! command -v uv &>/dev/null; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
    fi
    uv venv .venv
    source .venv/bin/activate
    uv pip install -e .
else
    source .venv/bin/activate
fi

# 3) Frontend deps.
if [ ! -d "frontend/node_modules" ]; then
    (cd frontend && npm install)
fi

# 4) Wire env so the host backend targets the compose-managed Postgres.
#    CURATION_PG_HOST_PORT has the default 127.0.0.1:5433.
PG_BIND="${CURATION_PG_HOST_PORT:-127.0.0.1:5433}"
PG_PORT="${PG_BIND##*:}"
PG_HOST="${PG_BIND%:*}"
source docker/.env
export CURATION_DB_URL="postgresql://${POSTGRES_USER:-curation}:${POSTGRES_PASSWORD}@${PG_HOST}:${PG_PORT}/${POSTGRES_DB:-curation}"
export CURATION_DATASET_PATH="${CURATION_DATASET_PATH:-/mnt/synology/data/data_div/2026_1/lerobot}"

BACKEND_PORT="${BACKEND_PORT:-8001}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
export VITE_PORT="$FRONTEND_PORT"
export VITE_BACKEND_URL="${VITE_BACKEND_URL:-http://localhost:${BACKEND_PORT}}"

echo "Hybrid dev mode:"
echo "  Dataset : $CURATION_DATASET_PATH"
echo "  Backend : http://localhost:${BACKEND_PORT}"
echo "  Frontend: http://localhost:${FRONTEND_PORT}"
echo "  DB      : $CURATION_DB_URL"

uvicorn backend.main:app --host 0.0.0.0 --port "$BACKEND_PORT" &
BACKEND_PID=$!
(cd frontend && npm run dev) &
FRONTEND_PID=$!

cleanup() {
    echo; echo "Shutting down host processes..."
    kill $BACKEND_PID 2>/dev/null || true
    kill $FRONTEND_PID 2>/dev/null || true
    wait
    # By design, leave the db/rerun containers up so restarting start.sh is fast.
}
trap cleanup EXIT INT TERM

wait
```

- [ ] **Step 2: Verify syntax**

Run:
```bash
bash -n start.sh && echo "syntax OK"
```
Expected: `syntax OK`.

- [ ] **Step 3: Smoke-test the path without launching servers**

Run:
```bash
docker compose -f docker/compose.yml up -d db
PG_BIND="${CURATION_PG_HOST_PORT:-127.0.0.1:5433}"
psql "postgresql://curation:dev-only-change-me@${PG_BIND}/curation" -c "SELECT 1;"
```
Expected: `?column? ------ 1`.

- [ ] **Step 4: Commit**

```bash
git add start.sh
git commit -m "Make start.sh a hybrid launcher that borrows compose's db"
```

---

## Task 21: Integration smoke test

**Purpose:** Walk the full stack end-to-end to confirm the Spec-1 acceptance gates are green before opening the PR.

**Files:** no code changes; this task verifies and records.

- [ ] **Step 1: Cold up with default profile**

Run:
```bash
./main.sh --down >/dev/null 2>&1 || true
./main.sh --up
```

- [ ] **Step 2: Wait for health and inspect**

Run:
```bash
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
  docker compose -f docker/compose.yml ps --format '{{.Service}} {{.Health}}' \
    | grep -q 'app healthy' && break
  sleep 5
done
docker compose -f docker/compose.yml ps
```
Expected: `app`, `nginx`, `db`, `rerun` all `running`, `app` health reports `healthy`.

- [ ] **Step 3: Hit the API and UI**

Run:
```bash
curl -fsS http://localhost:${CURATION_UI_PORT:-18080}/api/health && echo
curl -fsS -o /dev/null -w '%{http_code}\n' http://localhost:${CURATION_UI_PORT:-18080}/
curl -fsS -o /dev/null -w 'rerun %{http_code}\n' http://localhost:${CURATION_UI_PORT:-18080}/rerun/
```
Expected: `{"status":"ok"}` (or similar 200 JSON), `200`, and `rerun 200` (or 101/upgrade for websockets — any 2xx/1xx passes).

- [ ] **Step 4: Bring up the two profiled services**

Run:
```bash
./main.sh --up-convert
./main.sh --up-curator
sleep 10
docker compose -f docker/compose.yml ps converter curation-worker
docker compose -f docker/compose.yml logs --tail=5 curation-worker
```
Expected: both containers `running`; `curation-worker` log has at least one `placeholder: queue consumer not yet implemented`.

- [ ] **Step 5: Tear down cleanly**

Run:
```bash
./main.sh --down
docker compose -f docker/compose.yml ps
```
Expected: no running containers for the compose project.

- [ ] **Step 6: Run the full pytest suite once more**

Run:
```bash
./main.sh --up >/dev/null   # brings db back up
.venv/bin/pytest tests/ -v
./main.sh --down
```
Expected: every test green.

- [ ] **Step 7: Prepare the PR commit (no code; only the final checklist update)**

No files to commit in this task unless earlier steps produced fixes. In that case, group them into a single `Fix issues surfaced by Spec-1 integration smoke` commit.

---

## Self-Review Notes

**Spec coverage check (against sections in `docs/superpowers/specs/2026-04-22-docker-5-service-split-spec1-design.md`):**
- Spec §4 topology → Tasks 1, 4, 5, 7, 8.
- Spec §5 DB schema/driver → Tasks 2, 9, 10, 11, 12, 13, 14.
- Spec §6 file layout → Tasks 1–8, 17, 18, 20.
- Spec §6.3 nginx proxy → Task 6.
- Spec §7 main.sh → Task 18.
- Spec §7.4 start.sh hybrid → Task 20.
- Spec §8 implementation order → Tasks map 1:1 (Step 0 → Task 0; Step 1 → 1–8; Step 2 → 9–14; Step 3 → 17; Step 4 → 5, 6, 15, 16; Step 5 → 18; Step 6 → 19; Step 7 → 20).
- Spec §9 verification gates → Task 21.
- Spec §10 rollback → relies on per-task git commits (one per Task) and the worktree itself (Task 0).
- Spec §12 risks:
  - `features` JSONB parsing — addressed by fresh-start assumption (Spec-1 §3 decision B) and by Task 12 Step 1's scan; no runtime data exists yet.
  - rerun CLI flag drift — Task 5 Step 3 verifies via `--help`.
  - `mem_limit` legacy — kept intentionally in Task 8.
  - SQLite hardcoded tests — Tasks 13 and 14 cover.

**Gaps intentionally deferred (consistent with Spec §13):**
- No queue-consumer implementation for converter or curation-worker (Spec-2/3).
- No Alembic.
- No testcontainers.

**Placeholder scan:** no TBD/TODO/similar-to-Task-N markers remain in the plan body.
