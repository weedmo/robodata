# Converter DB Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move converter task requests from NAS request files to Postgres `jobs(type='convert')` while keeping existing progress/event files for the first release.

**Architecture:** The FastAPI backend owns enqueue/cancel/status writes through a small converter job repository. The converter worker connects to Postgres directly, claims one queued convert job with `FOR UPDATE SKIP LOCKED`, converts only that `cell_task`, heartbeats while running, and writes terminal status back to `jobs`. Existing `convert_state.json` and `convert_events.jsonl` remain the live progress path until a later event-table migration.

**Tech Stack:** PostgreSQL 16, asyncpg, FastAPI, pytest, Docker Compose, existing `rosbag2lerobot-svt/auto_converter.py`.

---

## Preconditions

- Complete `docs/superpowers/plans/2026-04-22-docker-5-service-split-spec1.md`.
- Confirm `jobs` has the approved schema from `docs/superpowers/specs/2026-04-22-docker-5-service-split-spec1-design.md`.
- Execute this plan in the same isolated worktree used for Spec-1.

## File Structure

- Create `backend/converter/job_repository.py`: backend-only enqueue, status, cancel operations for `jobs(type='convert')`.
- Modify `backend/converter/router.py`: route `/api/converter/start`, `/stop`, `/status`, and new `/jobs/{job_id}` through the repository when DB queue mode is enabled.
- Modify `backend/core/config.py`: add `converter_queue_backend`, `converter_worker_stale_seconds`, and `converter_worker_poll_seconds`.
- Create `tests/test_converter_job_repository.py`: repository tests against the Postgres test DB.
- Modify `tests/test_converter_validation_router.py`: router contract tests for DB queue mode and file fallback mode.
- Create `rosbag2lerobot-svt/nas/job_queue.py`: converter-side asyncpg client for claim, heartbeat, cancel check, complete, fail, and cancel terminal updates.
- Modify `rosbag2lerobot-svt/auto_converter.py`: add DB queue loop that claims jobs and invokes existing task conversion logic for one `cell_task`.
- Modify `rosbag2lerobot-svt/test/test_job_queue.py`: unit tests for SQL transitions using the compose test DB.
- Modify `docker/compose.yml`: pass DB URL and `CONVERTER_QUEUE_BACKEND=postgres` into the converter service.
- Modify `docker/converter/Dockerfile`: install asyncpg for the converter runtime.
- Modify `README.md`: update converter runbook and rollback mode.

## Task 1: Backend Queue Settings

**Files:**
- Modify: `backend/core/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write failing config tests**

Append to `tests/test_config.py`:

```python
def test_converter_queue_backend_defaults_to_files(monkeypatch):
    monkeypatch.delenv("CURATION_CONVERTER_QUEUE_BACKEND", raising=False)
    from backend.core.config import Settings

    settings = Settings()

    assert settings.converter_queue_backend == "files"


def test_converter_queue_backend_accepts_postgres(monkeypatch):
    monkeypatch.setenv("CURATION_CONVERTER_QUEUE_BACKEND", "postgres")
    from backend.core.config import Settings

    settings = Settings()

    assert settings.converter_queue_backend == "postgres"
    assert settings.converter_worker_stale_seconds == 300
    assert settings.converter_worker_poll_seconds == 5
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run:

```bash
.venv/bin/pytest tests/test_config.py -k "converter_queue_backend" -v
```

Expected: FAIL because the three settings fields do not exist.

- [ ] **Step 3: Add settings**

In `backend/core/config.py`, add these fields inside `Settings` near the existing converter/db settings:

```python
    converter_queue_backend: str = "files"
    converter_worker_stale_seconds: int = 300
    converter_worker_poll_seconds: int = 5
```

Add this validator below `_sync_allowed_dataset_roots`:

```python
    @model_validator(mode="after")
    def _validate_converter_queue_backend(self):
        if self.converter_queue_backend not in {"files", "postgres"}:
            raise ValueError("converter_queue_backend must be 'files' or 'postgres'")
        return self
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run:

```bash
.venv/bin/pytest tests/test_config.py -k "converter_queue_backend" -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add backend/core/config.py tests/test_config.py
git commit -m "Select the converter queue backend through settings" \
  -m "Converter requests need a runtime switch so deployments can move to Postgres without deleting the file queue fallback in the same change." \
  -m "Constraint: File queue remains the default until compose enables Postgres mode" \
  -m "Confidence: high" \
  -m "Scope-risk: narrow" \
  -m "Tested: .venv/bin/pytest tests/test_config.py -k converter_queue_backend -v"
```

## Task 2: Backend Converter Job Repository

**Files:**
- Create: `backend/converter/job_repository.py`
- Test: `tests/test_converter_job_repository.py`

- [ ] **Step 1: Write failing repository tests**

Create `tests/test_converter_job_repository.py`:

```python
"""Tests for DB-backed converter job queue operations."""

import pytest

from backend.converter.job_repository import (
    enqueue_convert_job,
    get_converter_job,
    get_latest_active_convert_job,
    request_convert_cancel,
)
from backend.core.db import get_db, init_db, close_db, _reset


@pytest.fixture(autouse=True)
async def clean_jobs():
    _reset()
    await init_db()
    db = await get_db()
    await db.execute("DELETE FROM jobs")
    yield
    await close_db()
    _reset()


async def test_enqueue_convert_job_creates_queued_job():
    job = await enqueue_convert_job("cell001/task_a")

    assert job["type"] == "convert"
    assert job["status"] == "queued"
    assert job["dedupe_key"] == "cell001/task_a"
    assert job["payload"]["cell_task"] == "cell001/task_a"


async def test_enqueue_convert_job_reuses_active_job():
    first = await enqueue_convert_job("cell001/task_a")
    second = await enqueue_convert_job("cell001/task_a")

    assert second["id"] == first["id"]
    assert second["message"] == "already queued"


async def test_cancel_queued_job_marks_cancelled():
    job = await enqueue_convert_job("cell001/task_a")

    cancelled = await request_convert_cancel()

    assert cancelled["id"] == job["id"]
    assert cancelled["status"] == "cancelled"
    saved = await get_converter_job(job["id"])
    assert saved["status"] == "cancelled"


async def test_cancel_running_job_marks_cancel_requested():
    job = await enqueue_convert_job("cell001/task_a")
    db = await get_db()
    await db.execute(
        "UPDATE jobs SET status='running', worker_id='worker-1', heartbeat_at=NOW() WHERE id=?",
        (job["id"],),
    )

    updated = await request_convert_cancel()

    assert updated["id"] == job["id"]
    assert updated["status"] == "cancel_requested"
    assert updated["cancel_requested_at"] is not None


async def test_latest_active_convert_job_ignores_complete_jobs():
    complete = await enqueue_convert_job("cell001/task_a")
    db = await get_db()
    await db.execute("UPDATE jobs SET status='complete', finished_at=NOW() WHERE id=?", (complete["id"],))
    active = await enqueue_convert_job("cell002/task_b")

    latest = await get_latest_active_convert_job()

    assert latest["id"] == active["id"]
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run:

```bash
.venv/bin/pytest tests/test_converter_job_repository.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'backend.converter.job_repository'`.

- [ ] **Step 3: Implement the repository**

Create `backend/converter/job_repository.py`:

```python
"""Postgres-backed converter job queue operations for FastAPI."""

from __future__ import annotations

import json
from typing import Any

from backend.core.db import get_db

ACTIVE_STATUSES = ("queued", "running", "cancel_requested")


def _json_obj(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, str):
        return json.loads(value)
    return value


def _row_to_job(row: Any, message: str | None = None) -> dict[str, Any]:
    job = dict(row)
    payload = _json_obj(job.get("payload"), {})
    progress = _json_obj(job.get("progress"), {})
    result = _json_obj(job.get("result"), None)
    job["payload"] = dict(payload)
    job["progress"] = dict(progress)
    job["result"] = dict(result) if isinstance(result, dict) else result
    if message:
        job["message"] = message
    return job


async def enqueue_convert_job(cell_task: str) -> dict[str, Any]:
    normalized = cell_task.strip()
    if not normalized:
        raise ValueError("cell_task is required")

    db = await get_db()
    async with db.transaction():
        async with db.execute(
            """
            SELECT *
            FROM jobs
            WHERE type='convert'
              AND dedupe_key=?
              AND status IN ('queued', 'running', 'cancel_requested')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (normalized,),
        ) as cur:
            existing = await cur.fetchone()
        if existing is not None:
            return _row_to_job(existing, "already queued")

        async with db.execute(
            """
            INSERT INTO jobs(type, status, payload, dedupe_key)
            VALUES ('convert', 'queued', ?::jsonb, ?)
            RETURNING *
            """,
            (json.dumps({"cell_task": normalized, "requested_by": "ui", "requested_from": "converter_page"}), normalized),
        ) as cur:
            row = await cur.fetchone()
    return _row_to_job(row, "queued")


async def get_converter_job(job_id: int | str) -> dict[str, Any] | None:
    db = await get_db()
    async with db.execute(
        "SELECT * FROM jobs WHERE id=? AND type='convert'",
        (int(job_id),),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_job(row) if row is not None else None


async def get_latest_active_convert_job() -> dict[str, Any] | None:
    db = await get_db()
    async with db.execute(
        """
        SELECT *
        FROM jobs
        WHERE type='convert'
          AND status IN ('queued', 'running', 'cancel_requested')
        ORDER BY created_at DESC
        LIMIT 1
        """,
    ) as cur:
        row = await cur.fetchone()
    return _row_to_job(row) if row is not None else None


async def request_convert_cancel() -> dict[str, Any] | None:
    active = await get_latest_active_convert_job()
    if active is None:
        return None

    db = await get_db()
    if active["status"] == "queued":
        async with db.execute(
            """
            UPDATE jobs
            SET status='cancelled',
                cancel_requested_at=NOW(),
                finished_at=NOW(),
                updated_at=NOW()
            WHERE id=?
            RETURNING *
            """,
            (active["id"],),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_job(row)

    async with db.execute(
        """
        UPDATE jobs
        SET status='cancel_requested',
            cancel_requested_at=NOW(),
            updated_at=NOW()
        WHERE id=?
          AND status IN ('running', 'cancel_requested')
        RETURNING *
        """,
        (active["id"],),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_job(row) if row is not None else active
```

- [ ] **Step 4: Run the repository tests**

Run:

```bash
.venv/bin/pytest tests/test_converter_job_repository.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add backend/converter/job_repository.py tests/test_converter_job_repository.py
git commit -m "Store converter requests in Postgres jobs" \
  -m "The backend needs a durable queue abstraction before router endpoints can stop writing convert_requests.json." \
  -m "Constraint: Active convert jobs are deduped by cell_task" \
  -m "Confidence: high" \
  -m "Scope-risk: narrow" \
  -m "Tested: .venv/bin/pytest tests/test_converter_job_repository.py -v"
```

## Task 3: Route Converter API Through DB Queue Mode

**Files:**
- Modify: `backend/converter/router.py`
- Test: `tests/test_converter_validation_router.py`

- [ ] **Step 1: Add failing router tests**

Append to `tests/test_converter_validation_router.py`:

```python
@pytest.mark.asyncio
async def test_post_start_in_postgres_queue_mode_returns_job(client):
    with patch.dict("os.environ", {"CURATION_CONVERTER_QUEUE_BACKEND": "postgres"}), patch(
        "backend.converter.router.job_repository.enqueue_convert_job",
        new_callable=AsyncMock,
        return_value={"id": 123, "status": "queued", "message": "queued", "payload": {"cell_task": "cell001/task_a"}},
    ) as enqueue:
        resp = await client.post("/api/converter/start", json={"cell_task": "cell001/task_a"})

    assert resp.status_code == 202
    assert resp.json()["job_id"] == 123
    assert resp.json()["status"] == "queued"
    enqueue.assert_awaited_once_with("cell001/task_a")


@pytest.mark.asyncio
async def test_post_stop_in_postgres_queue_mode_requests_cancel(client):
    with patch.dict("os.environ", {"CURATION_CONVERTER_QUEUE_BACKEND": "postgres"}), patch(
        "backend.converter.router.job_repository.request_convert_cancel",
        new_callable=AsyncMock,
        return_value={"id": 123, "status": "cancel_requested"},
    ) as cancel:
        resp = await client.post("/api/converter/stop")

    assert resp.status_code == 202
    assert resp.json()["job_id"] == 123
    assert resp.json()["status"] == "cancel_requested"
    cancel.assert_awaited_once()
```

- [ ] **Step 2: Run tests and confirm they fail**

Run:

```bash
.venv/bin/pytest tests/test_converter_validation_router.py -k "postgres_queue_mode" -v
```

Expected: FAIL because `router.py` does not import or use `job_repository`.

- [ ] **Step 3: Update router imports and helpers**

In `backend/converter/router.py`, add:

```python
from backend.core.config import settings
from backend.converter import job_repository
```

Add this helper near `_queue_host_controlled_task`:

```python
async def _queue_db_controlled_task(cell_task: str | None) -> JSONResponse:
    if not cell_task:
        raise HTTPException(409, "cell_task is required for DB-backed converter queue")
    try:
        job = await job_repository.enqueue_convert_job(cell_task)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return JSONResponse(
        status_code=202,
        content={
            "status": job["status"],
            "message": job.get("message", "queued"),
            "job_id": job["id"],
            "cell_task": job["payload"]["cell_task"],
        },
    )


async def _request_db_controlled_stop() -> JSONResponse:
    job = await job_repository.request_convert_cancel()
    if job is None:
        raise HTTPException(503, "Converter is not running")
    return JSONResponse(
        status_code=202,
        content={
            "status": job["status"],
            "message": "cancel_requested" if job["status"] == "cancel_requested" else "cancelled",
            "job_id": job["id"],
        },
    )
```

- [ ] **Step 4: Wire start and stop branches**

At the start of `start()`, after `cell_task` is computed, insert:

```python
    if settings.converter_queue_backend == "postgres":
        return await _queue_db_controlled_task(cell_task)
```

At the start of `stop()`, insert:

```python
    if settings.converter_queue_backend == "postgres":
        return await _request_db_controlled_stop()
```

- [ ] **Step 5: Add job detail endpoint**

Add near the other converter routes:

```python
@router.get("/jobs/{job_id}")
async def get_converter_job(job_id: int):
    job = await job_repository.get_converter_job(job_id)
    if job is None:
        raise HTTPException(404, f"Converter job not found: {job_id}")
    return {
        "job_id": job["id"],
        "operation": "convert",
        "status": job["status"],
        "created_at": job["created_at"],
        "started_at": job.get("started_at"),
        "completed_at": job.get("finished_at"),
        "error": job.get("error"),
        "progress": job.get("progress") or {},
    }
```

- [ ] **Step 6: Run router tests**

Run:

```bash
.venv/bin/pytest tests/test_converter_validation_router.py -k "postgres_queue_mode or host_controlled_task" -v
```

Expected: PASS. Existing host-control file queue tests still pass.

- [ ] **Step 7: Commit**

Run:

```bash
git add backend/converter/router.py tests/test_converter_validation_router.py
git commit -m "Route converter control through DB queue mode" \
  -m "The API must support Postgres-backed enqueue and cancellation while preserving the file queue fallback for rollback." \
  -m "Constraint: Existing host/file mode tests must remain green" \
  -m "Confidence: high" \
  -m "Scope-risk: moderate" \
  -m "Tested: .venv/bin/pytest tests/test_converter_validation_router.py -k 'postgres_queue_mode or host_controlled_task' -v"
```

## Task 4: Converter-Side Job Queue Client

**Files:**
- Create: `rosbag2lerobot-svt/nas/job_queue.py`
- Test: `rosbag2lerobot-svt/test/test_job_queue.py`
- Modify: `docker/converter/Dockerfile`

- [ ] **Step 1: Add asyncpg to converter image plan**

In `docker/converter/Dockerfile`, add `"asyncpg>=0.30.0"` to the Python package install block that currently installs converter runtime dependencies.

- [ ] **Step 2: Write failing worker queue tests**

Create `rosbag2lerobot-svt/test/test_job_queue.py`:

```python
"""Tests for converter-side Postgres job claiming."""

import os
import json

import pytest

from nas.job_queue import PostgresJobQueue


pytestmark = pytest.mark.asyncio


@pytest.fixture
async def queue():
    db_url = os.environ.get(
        "CURATION_TEST_DB_URL",
        "postgresql://curation:dev-only-change-me@127.0.0.1:5433/curation_test",
    )
    q = PostgresJobQueue(db_url=db_url, worker_id="test-worker")
    await q.connect()
    await q.execute("DELETE FROM jobs")
    yield q
    await q.close()


async def test_claim_returns_oldest_queued_convert_job(queue):
    await queue.execute(
        "INSERT INTO jobs(type, status, payload, dedupe_key) VALUES ('convert', 'queued', $1::jsonb, 'cell001/task_a')",
        json.dumps({"cell_task": "cell001/task_a"}),
    )

    job = await queue.claim_next()

    assert job is not None
    assert job["payload"]["cell_task"] == "cell001/task_a"
    saved = await queue.fetchrow("SELECT status, worker_id FROM jobs WHERE id=$1", job["id"])
    assert saved["status"] == "running"
    assert saved["worker_id"] == "test-worker"


async def test_cancel_requested_is_visible(queue):
    await queue.execute(
        "INSERT INTO jobs(type, status, payload, dedupe_key) VALUES ('convert', 'queued', $1::jsonb, 'cell001/task_a')",
        json.dumps({"cell_task": "cell001/task_a"}),
    )
    job = await queue.claim_next()
    await queue.execute("UPDATE jobs SET status='cancel_requested' WHERE id=$1", job["id"])

    assert await queue.is_cancel_requested(job["id"]) is True


async def test_complete_marks_terminal(queue):
    await queue.execute(
        "INSERT INTO jobs(type, status, payload, dedupe_key) VALUES ('convert', 'queued', $1::jsonb, 'cell001/task_a')",
        json.dumps({"cell_task": "cell001/task_a"}),
    )
    job = await queue.claim_next()

    await queue.mark_complete(job["id"], {"converted": 1, "failed": 0})

    saved = await queue.fetchrow("SELECT status, result FROM jobs WHERE id=$1", job["id"])
    assert saved["status"] == "complete"
    assert json.loads(saved["result"])["converted"] == 1
```

- [ ] **Step 3: Run tests and confirm they fail**

Run:

```bash
docker run --rm -v "$PWD/rosbag2lerobot-svt":/app -w /app convert-server-convert-server:latest python3 -m pytest test/test_job_queue.py -v
```

Expected: FAIL because `nas.job_queue` does not exist or asyncpg is unavailable.

- [ ] **Step 4: Implement queue client**

Create `rosbag2lerobot-svt/nas/job_queue.py`:

```python
"""Postgres-backed converter job queue client."""

from __future__ import annotations

import json
from typing import Any

import asyncpg


class PostgresJobQueue:
    def __init__(self, *, db_url: str, worker_id: str) -> None:
        self.db_url = db_url
        self.worker_id = worker_id
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(dsn=self.db_url, min_size=1, max_size=2)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def execute(self, sql: str, *params: Any) -> str:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            return await conn.execute(sql, *params)

    async def fetchrow(self, sql: str, *params: Any):
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(sql, *params)

    def _job_from_row(self, row) -> dict[str, Any]:
        job = dict(row)
        payload = job.get("payload")
        if isinstance(payload, str):
            job["payload"] = json.loads(payload)
        return job

    async def claim_next(self) -> dict[str, Any] | None:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    WITH next_job AS (
                        SELECT id
                        FROM jobs
                        WHERE type='convert' AND status='queued'
                        ORDER BY created_at
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE jobs
                    SET status='running',
                        worker_id=$1,
                        attempts=attempts + 1,
                        started_at=COALESCE(started_at, NOW()),
                        heartbeat_at=NOW(),
                        updated_at=NOW()
                    WHERE id=(SELECT id FROM next_job)
                    RETURNING id, payload
                    """,
                    self.worker_id,
                )
        return self._job_from_row(row) if row is not None else None

    async def heartbeat(self, job_id: int, progress: dict[str, Any] | None = None) -> None:
        assert self._pool is not None
        await self.execute(
            """
            UPDATE jobs
            SET heartbeat_at=NOW(),
                updated_at=NOW(),
                progress=COALESCE($3::jsonb, progress)
            WHERE id=$1 AND worker_id=$2 AND status IN ('running', 'cancel_requested')
            """,
            job_id,
            self.worker_id,
            json.dumps(progress) if progress is not None else None,
        )

    async def is_cancel_requested(self, job_id: int) -> bool:
        row = await self.fetchrow("SELECT status FROM jobs WHERE id=$1 AND worker_id=$2", job_id, self.worker_id)
        return row is not None and row["status"] == "cancel_requested"

    async def mark_complete(self, job_id: int, result: dict[str, Any]) -> None:
        await self.execute(
            """
            UPDATE jobs
            SET status='complete',
                result=$3::jsonb,
                finished_at=NOW(),
                updated_at=NOW()
            WHERE id=$1 AND worker_id=$2
            """,
            job_id,
            self.worker_id,
            json.dumps(result),
        )

    async def mark_failed(self, job_id: int, error: str) -> None:
        await self.execute(
            """
            UPDATE jobs
            SET status='failed',
                error=$3,
                finished_at=NOW(),
                updated_at=NOW()
            WHERE id=$1 AND worker_id=$2
            """,
            job_id,
            self.worker_id,
            error,
        )

    async def mark_cancelled(self, job_id: int) -> None:
        await self.execute(
            """
            UPDATE jobs
            SET status='cancelled',
                finished_at=NOW(),
                updated_at=NOW()
            WHERE id=$1 AND worker_id=$2
            """,
            job_id,
            self.worker_id,
        )
```

- [ ] **Step 5: Run worker queue tests**

Run:

```bash
docker compose -f docker/compose.yml build converter
docker run --rm --network host -v "$PWD/rosbag2lerobot-svt":/app -w /app convert-server-converter:latest python3 -m pytest test/test_job_queue.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add docker/converter/Dockerfile rosbag2lerobot-svt/nas/job_queue.py rosbag2lerobot-svt/test/test_job_queue.py
git commit -m "Let converter workers claim Postgres jobs" \
  -m "The converter process needs its own small DB client so work ownership and terminal state live in the shared jobs table." \
  -m "Constraint: auto_converter remains synchronous; DB access is isolated in nas.job_queue" \
  -m "Confidence: high" \
  -m "Scope-risk: moderate" \
  -m "Tested: docker run --rm --network host -v \"$PWD/rosbag2lerobot-svt\":/app -w /app convert-server-converter:latest python3 -m pytest test/test_job_queue.py -v"
```

## Task 5: Auto Converter DB Queue Loop

**Files:**
- Modify: `rosbag2lerobot-svt/auto_converter.py`
- Test: `rosbag2lerobot-svt/test/test_auto_converter_db_queue.py`

- [ ] **Step 1: Write failing auto-converter DB queue tests**

Create `rosbag2lerobot-svt/test/test_auto_converter_db_queue.py`:

```python
"""Tests for auto_converter DB queue mode."""

from types import SimpleNamespace

import auto_converter


class FakeQueue:
    def __init__(self, jobs):
        self.jobs = list(jobs)
        self.completed = []
        self.failed = []
        self.cancelled = []
        self.heartbeats = []
        self.closed = False

    async def connect(self):
        return None

    async def close(self):
        self.closed = True

    async def claim_next(self):
        return self.jobs.pop(0) if self.jobs else None

    async def heartbeat(self, job_id, progress=None):
        self.heartbeats.append((job_id, progress))

    async def is_cancel_requested(self, job_id):
        return False

    async def mark_complete(self, job_id, result):
        self.completed.append((job_id, result))

    async def mark_failed(self, job_id, error):
        self.failed.append((job_id, error))

    async def mark_cancelled(self, job_id):
        self.cancelled.append(job_id)


def test_db_queue_loop_converts_claimed_cell_task(monkeypatch):
    queue = FakeQueue([{"id": 42, "payload": {"cell_task": "cell001/task_a"}}])
    calls = []

    monkeypatch.setattr(auto_converter, "run_single_requested_task", lambda cell_task: calls.append(cell_task) or {"converted": 1, "failed": 0})

    auto_converter.run_db_queue_loop(queue=queue, poll_seconds=0, max_idle_cycles=1)

    assert calls == ["cell001/task_a"]
    assert queue.completed == [(42, {"converted": 1, "failed": 0})]
    assert queue.closed is True


def test_db_queue_loop_marks_failed(monkeypatch):
    queue = FakeQueue([{"id": 43, "payload": {"cell_task": "cell001/task_a"}}])

    def boom(cell_task):
        raise RuntimeError("convert boom")

    monkeypatch.setattr(auto_converter, "run_single_requested_task", boom)

    auto_converter.run_db_queue_loop(queue=queue, poll_seconds=0, max_idle_cycles=1)

    assert queue.failed == [(43, "convert boom")]
```

- [ ] **Step 2: Run tests and confirm they fail**

Run:

```bash
docker run --rm -v "$PWD/rosbag2lerobot-svt":/app -w /app convert-server-convert-server:latest python3 -m pytest test/test_auto_converter_db_queue.py -v
```

Expected: FAIL because `run_db_queue_loop` and `run_single_requested_task` do not exist.

- [ ] **Step 3: Add synchronous wrappers to auto_converter.py**

In `rosbag2lerobot-svt/auto_converter.py`, add imports:

```python
import asyncio
import socket
```

Add these functions above `main()`:

```python
def run_single_requested_task(cell_task: str) -> dict[str, int]:
    """Run one requested cell/task through the existing scanner/converter path."""
    parts = cell_task.split("/", 1)
    if len(parts) != 2:
        raise ValueError(f"invalid cell_task: {cell_task}")
    before = ConvertState(STATE_FILE)
    before.load()
    main_loop_once(only_cell_task=cell_task)
    after = ConvertState(STATE_FILE)
    after.load()
    done = len(after.get_done_serials(cell_task)) - len(before.get_done_serials(cell_task))
    failed = len(after.get_failed_serials(cell_task)) - len(before.get_failed_serials(cell_task))
    return {"converted": max(done, 0), "failed": max(failed, 0)}


def main_loop_once(*, only_cell_task: str) -> None:
    """Execute one scan/convert cycle for a single requested task."""
    previous_single = os.environ.get("SINGLE_SHOT")
    previous_only = os.environ.get("ONLY_CELL_TASK")
    os.environ["SINGLE_SHOT"] = "1"
    os.environ["ONLY_CELL_TASK"] = only_cell_task
    try:
        main()
    finally:
        if previous_single is None:
            os.environ.pop("SINGLE_SHOT", None)
        else:
            os.environ["SINGLE_SHOT"] = previous_single
        if previous_only is None:
            os.environ.pop("ONLY_CELL_TASK", None)
        else:
            os.environ["ONLY_CELL_TASK"] = previous_only
```

Add the DB queue loop:

```python
def _run_async(coro):
    return asyncio.run(coro)


def run_db_queue_loop(*, queue, poll_seconds: int, max_idle_cycles: int | None = None) -> None:
    """Claim and run convert jobs forever, or until max_idle_cycles in tests."""
    _run_async(queue.connect())
    idle_cycles = 0
    try:
        while not shutdown_event.is_set():
            job = _run_async(queue.claim_next())
            if job is None:
                idle_cycles += 1
                if max_idle_cycles is not None and idle_cycles >= max_idle_cycles:
                    break
                shutdown_event.wait(timeout=poll_seconds)
                continue
            idle_cycles = 0
            job_id = int(job["id"])
            payload = job["payload"]
            cell_task = payload["cell_task"]
            try:
                _run_async(queue.heartbeat(job_id, {"phase": "converting", "active_cell_task": cell_task}))
                if _run_async(queue.is_cancel_requested(job_id)):
                    _run_async(queue.mark_cancelled(job_id))
                    continue
                result = run_single_requested_task(cell_task)
                if _run_async(queue.is_cancel_requested(job_id)):
                    _run_async(queue.mark_cancelled(job_id))
                else:
                    _run_async(queue.mark_complete(job_id, result))
            except Exception as exc:
                _run_async(queue.mark_failed(job_id, str(exc)))
    finally:
        _run_async(queue.close())
```

- [ ] **Step 4: Select DB queue mode in main entrypoint**

At the bottom of `auto_converter.py`, replace:

```python
if __name__ == "__main__":
    main()
```

with:

```python
if __name__ == "__main__":
    if os.environ.get("CONVERTER_QUEUE_BACKEND", "files") == "postgres":
        from nas.job_queue import PostgresJobQueue

        db_url = os.environ["CURATION_DB_URL"]
        worker_id = os.environ.get("CONVERTER_WORKER_ID") or socket.gethostname()
        poll_seconds = int(os.environ.get("CONVERTER_WORKER_POLL_SECONDS", "5"))
        run_db_queue_loop(
            queue=PostgresJobQueue(db_url=db_url, worker_id=worker_id),
            poll_seconds=poll_seconds,
        )
    else:
        main()
```

- [ ] **Step 5: Run auto-converter DB queue tests**

Run:

```bash
docker run --rm -v "$PWD/rosbag2lerobot-svt":/app -w /app convert-server-convert-server:latest python3 -m pytest test/test_auto_converter_db_queue.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add rosbag2lerobot-svt/auto_converter.py rosbag2lerobot-svt/test/test_auto_converter_db_queue.py
git commit -m "Run converter tasks from claimed DB jobs" \
  -m "The worker now has a Postgres queue mode that claims one convert job, runs the existing single-task conversion path, and records terminal state." \
  -m "Constraint: Progress and activity files remain active for the first DB queue release" \
  -m "Confidence: medium" \
  -m "Scope-risk: broad" \
  -m "Tested: docker run --rm -v \"$PWD/rosbag2lerobot-svt\":/app -w /app convert-server-convert-server:latest python3 -m pytest test/test_auto_converter_db_queue.py -v"
```

## Task 6: Compose Wiring and Runbook

**Files:**
- Modify: `docker/compose.yml`
- Modify: `README.md`
- Test: `tests/test_ui_service_docker_ops.py`

- [ ] **Step 1: Add failing compose tests**

Append to `tests/test_ui_service_docker_ops.py`:

```python
def test_converter_uses_postgres_queue_in_unified_compose():
    compose = (ROOT / "docker" / "compose.yml").read_text(encoding="utf-8")

    assert "CONVERTER_QUEUE_BACKEND: postgres" in compose
    assert "CURATION_DB_URL:" in compose
    assert "CONVERTER_WORKER_POLL_SECONDS:" in compose
```

- [ ] **Step 2: Run test and confirm it fails**

Run:

```bash
.venv/bin/pytest tests/test_ui_service_docker_ops.py -k postgres_queue -v
```

Expected: FAIL until compose env is wired.

- [ ] **Step 3: Update converter service env**

In `docker/compose.yml`, add these environment entries under the `converter` service:

```yaml
      CURATION_DB_URL: postgresql://${POSTGRES_USER:-curation}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB:-curation}
      CONVERTER_QUEUE_BACKEND: postgres
      CONVERTER_WORKER_POLL_SECONDS: ${CONVERTER_WORKER_POLL_SECONDS:-5}
```

Keep existing `RAW_BASE`, `LEROBOT_BASE`, memory, and NAS mount settings unchanged.

- [ ] **Step 4: Update README runbook**

In `README.md`, replace the production converter note with:

```markdown
- Converter lifecycle is separated from the UI process, but converter requests are queued in Postgres when using `docker/compose.yml`.
- Start the converter with `./main.sh --up-convert`; clicking Convert inserts a `jobs(type='convert')` row and the converter worker claims it.
- Rollback mode: set `CONVERTER_QUEUE_BACKEND=files` to use the legacy NAS request-file path.
```

- [ ] **Step 5: Run compose tests**

Run:

```bash
.venv/bin/pytest tests/test_ui_service_docker_ops.py -k postgres_queue -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add docker/compose.yml README.md tests/test_ui_service_docker_ops.py
git commit -m "Run converter compose service in Postgres queue mode" \
  -m "The unified compose stack should make DB queue mode the normal converter path while preserving the file backend as an explicit rollback switch." \
  -m "Constraint: Existing NAS mounts and converter memory settings stay unchanged" \
  -m "Confidence: high" \
  -m "Scope-risk: narrow" \
  -m "Tested: .venv/bin/pytest tests/test_ui_service_docker_ops.py -k postgres_queue -v"
```

## Task 7: End-to-End Verification

**Files:**
- Modify only if verification exposes a defect.

- [ ] **Step 1: Start the stack**

Run:

```bash
./main.sh --up-convert
```

Expected: `db`, `app`, `nginx`, `rerun`, and `converter` are healthy or running.

- [ ] **Step 2: Queue a converter job through the API**

Run with a real pending task:

```bash
curl -s -X POST http://localhost:18080/api/converter/start \
  -H 'Content-Type: application/json' \
  -d '{"cell_task":"cell001/task_a"}' | jq .
```

Expected:

```json
{
  "status": "queued",
  "message": "queued",
  "job_id": 1,
  "cell_task": "cell001/task_a"
}
```

- [ ] **Step 3: Confirm the job is in Postgres**

Run:

```bash
docker compose -f docker/compose.yml exec -T db psql -U curation -d curation \
  -c "SELECT id, type, status, dedupe_key FROM jobs ORDER BY id DESC LIMIT 1;"
```

Expected: newest row has `type=convert`, `dedupe_key=cell001/task_a`, and status moves from `queued` to `running` when the worker claims it.

- [ ] **Step 4: Confirm legacy request file is not written**

Run:

```bash
docker compose -f docker/compose.yml exec -T converter bash -lc 'test ! -f /data/lerobot/convert_requests.json'
```

Expected: exit 0 in Postgres queue mode.

- [ ] **Step 5: Run targeted tests**

Run:

```bash
.venv/bin/pytest \
  tests/test_converter_job_repository.py \
  tests/test_converter_validation_router.py \
  tests/test_ui_service_docker_ops.py \
  -v
docker run --rm --network host -v "$PWD/rosbag2lerobot-svt":/app -w /app convert-server-converter:latest \
  python3 -m pytest test/test_job_queue.py test/test_auto_converter_db_queue.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Run full regression suite**

Run:

```bash
.venv/bin/pytest tests/ -v
docker run --rm -v "$PWD/rosbag2lerobot-svt":/app -w /app convert-server-converter:latest python3 -m pytest test/ -v
```

Expected: all tests pass. If failures are unrelated to converter queue mode, record them in the final report with exact failing tests.

- [ ] **Step 7: Commit verification fixes if any**

If Step 1-6 required code changes, commit them:

```bash
git add <changed-files>
git commit -m "Stabilize converter DB queue integration" \
  -m "Verification surfaced integration drift after enabling Postgres queue mode, so this commit keeps the implementation aligned with the approved queue contract." \
  -m "Confidence: medium" \
  -m "Scope-risk: narrow" \
  -m "Tested: targeted converter queue tests and full regression commands from Task 7"
```

No commit is needed if verification passes without changes.

## Self-Review Notes

**Spec coverage check:**

- Spec §2 included/excluded scope: Tasks 2-6 implement enqueue/cancel/claim/status while keeping progress/event files.
- Spec §3 decisions: Tasks 2, 4, 5, and 6 implement Postgres queue, direct worker DB access, dedupe, claim query, file fallback, and request-file removal from normal mode.
- Spec §4 data model: Task 2 and Spec-1 plan schema cover payload, progress, result, statuses, dedupe, heartbeat.
- Spec §5 backend API: Tasks 2 and 3 cover start, stop, status addition path, and job detail endpoint.
- Spec §6 worker protocol: Tasks 4 and 5 cover claim, heartbeat, execution, cancellation, and terminal updates.
- Spec §7 migration plan: Tasks 1-6 map to phases 1-4.
- Spec §8 error handling: Tasks 2, 3, 4, and 5 cover invalid input, duplicate job, DB unavailable behavior through raised HTTP errors or worker retry, crash/stale handling baseline, and terminal failure writes.
- Spec §9 testing: Tasks 1-7 include backend, worker, and integration commands.
- Spec §10 rollback: Tasks 1, 3, and 6 keep `files` fallback available.

**Placeholder scan:** no placeholder markers; every implementation task names exact files, commands, and expected outcomes.

**Type consistency:** Job statuses are `queued/running/complete/failed/cancel_requested/cancelled`, matching Spec-1 schema. Convert jobs use `dedupe_key=cell_task` and `payload.cell_task`.
