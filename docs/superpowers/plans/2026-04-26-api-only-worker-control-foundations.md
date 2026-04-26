# API-only Worker Control — Foundations Plan

> **Verified:** 2026-04-26T00:00Z · Codex(gpt-5.5/xhigh) ↔ Claude · 1 Codex pass (partial, edits applied) + 0 Claude pass · NEEDS-WORK · fixes=substantive-but-uncounted
> **Verification note:** Codex Pass 1이 5분 cap을 넘겨 (~7m30s) cancel됐으나, 그 전에 file에 substantive 편집을 적용했다 (Task 0 db facade 신설, Prerequisites 정정, Rollback 섹션 추가, asyncpg.UniqueViolationError race 핸들링, JSONB decode, ASGITransport 패턴 등 +526/-125 줄). 단, Codex가 "## Fixed"/"## Remaining concerns" 공식 출력을 렌더링하지 못해 변경 항목 카운트 정확치 미확보. Claude Pass 2(dissent) 및 Codex Pass 3 미실행. 실행 전 Tasks 1~6에 대한 추가 코드리뷰 권장.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** API-only Worker Control 보강안의 **기반 인프라**를 구축한다 — `worker_controls`/`worker_heartbeats` 스키마, `/api/jobs`·`/api/workers` REST API, 상주 워커 베이스 루프(`runtime.tick`, `run_forever`). 이 plan만 머지하면 jobs 큐를 enqueue·cancel·polling으로 운영할 수 있는 데이터/서버 표면이 완성된다.

**Architecture:**
- Spec-1 plan(`2026-04-22-docker-5-service-split-spec1.md`)이 깔아두는 Postgres + asyncpg 래퍼 + `jobs` 테이블을 전제로 한다. 본 plan은 그 위에 `worker_controls`/`worker_heartbeats`를 더하고, jobs/workers를 다루는 일반 API와 상주 워커 베이스 루프를 신규 추가한다.
- 워커는 `(queued → running → complete|failed|cancelled)` 상태 머신을 안전 지점(safe checkpoint) 사이에서 협력적으로 운영한다. `cancel_requested` 신호를 chunk 사이에 폴링해 부분 산출물을 명시적으로 정리하거나 partial-fail 마커를 남긴다.
- 모든 변경은 TDD로 진행: pytest → docker mockup 확인 → 실 데이터 확인의 CLAUDE.md 순서를 따른다.
- 본 plan은 호출자(컨버터·UI)를 손대지 않는다. 그 부분은 Integration plan(`2026-04-26-api-only-worker-control-integration.md`)에서 다룬다.

**Tech Stack:** Python 3.13 + FastAPI + asyncpg, Postgres 16-alpine, Docker Compose v2, pytest + httpx.AsyncClient + ASGITransport.

---

## Prerequisites

- Spec-1 plan Task 1~14 완료 — `docker/compose.yml`, `docker/db/init.sql`(기본 datasets/jobs 테이블), `backend/core/db.py` asyncpg 래퍼, 백엔드 호출 지점 마이그레이션이 끝난 상태. 이 파일들이 현재 checkout에 없다면 먼저 `docs/superpowers/plans/2026-04-22-docker-5-service-split-spec1.md`를 끝까지 실행한 브랜치/worktree로 전환한다.
- Spec-1 Task 11의 실제 contract는 `backend.core.db.get_db()`가 `_DB` 래퍼를 반환하고, 그 래퍼가 `execute(sql, params)`, `executescript(sql)`, `commit()`, `transaction()`을 제공한다는 것이다. 본 plan의 Task 0이 `fetch_one`/`fetch_all` 편의 facade를 명시적으로 추가하므로, 그 전에는 `from backend.core.db import db`를 사용할 수 없다.
- 작업 워크트리: `superpowers:using-git-worktrees`로 `feat/docker-5-service-split` 기반 격리 worktree를 만든 뒤 실행한다. 사용자가 명시적으로 main worktree 작업을 지시한 경우에만 예외.
- Python 테스트 환경은 Spec-1 Task 9 이후 상태(`asyncpg`, `pytest-asyncio`, `httpx`)여야 한다. 아래 명령은 `pytest`가 활성 venv PATH에 있다고 가정한다. 그렇지 않으면 같은 명령을 `.venv/bin/pytest ...`로 실행한다.
- Postgres test DB가 host port `5433` 또는 compose 내부 `db:5432`로 접근 가능. `CURATION_TEST_DB_URL` 미설정 시 Spec-1 기본값 `postgresql://curation:dev-only-change-me@127.0.0.1:5433/curation_test` 사용.

본 plan을 머지한 뒤 Integration plan으로 이어진다 — 그 plan이 `rosbag2lerobot-svt/auto_converter.py`를 큐 소비자로 전환하고, host control 코드/시그널 파일을 제거하며, 프런트 표면을 정돈한다.

---

## Rollback / Recovery

- 코드 rollback: 각 task는 독립 커밋이다. 해당 task만 되돌릴 때는 `git revert <commit>`을 사용한다.
- 테스트 DB rollback: `docker compose -f docker/compose.yml down -v && docker compose -f docker/compose.yml up -d db`로 test/개발 DB volume을 재생성한다. 운영 데이터가 들어간 volume에서는 `down -v` 금지.
- 스키마 task 실패 시: `git restore docker/db/init.sql backend/core/db.py tests/test_init_sql_workers.py` 후 test DB volume을 재생성한다.

---

## File Structure

**Modify:**
- `docker/db/init.sql` — `worker_controls`, `worker_heartbeats` 테이블 + ENUM 추가, 기본 워커 행 시드.
- `backend/core/db.py` — Spec-1 `_DB` 래퍼 위에 `db.execute/fetch_one/fetch_all/transaction` facade 추가, `_SCHEMA_V1`에 worker schema mirror.
- `backend/main.py` — jobs/workers 라우터 include.

**Create:**
- `tests/test_db_query_facade.py`
- `backend/jobs/__init__.py`
- `backend/jobs/repo.py` — `enqueue`, `fetch`, `list_jobs`, `request_cancel` SQL.
- `backend/jobs/router.py` — `/api/jobs` REST 라우터.
- `backend/workers/__init__.py`
- `backend/workers/repo.py` — `worker_controls`/`worker_heartbeats` 조회·갱신 SQL.
- `backend/workers/service.py` — 상태 전이 규칙(`running ⇄ paused`, stale TTL).
- `backend/workers/router.py` — `/api/workers` REST 라우터.
- `backend/workers/runtime.py` — 상주 워커 베이스 루프(`tick`, `run_forever`).
- `tests/test_init_sql_workers.py`
- `tests/test_jobs_repo.py`
- `tests/test_jobs_router.py`
- `tests/test_workers_repo.py`
- `tests/test_workers_router.py`
- `tests/test_workers_runtime.py`

**Out of scope** (Integration plan으로 이관):
- `rosbag2lerobot-svt/auto_converter.py` 큐 소비자 전환
- `backend/converter/router.py` / `backend/converter/service.py` host control 코드·NAS 시그널 파일 제거
- `docker/compose.yml`에서 docker.sock·`CURATION_CONVERTER_CONTROL_MODE` 제거
- `frontend/src/components/ConverterControls.tsx` Convert 버튼·WorkerControlPill, `README.md`

---

## Task 0: `backend.core.db.db` query facade 추가

**Files:**
- Modify: `backend/core/db.py`
- Create: `tests/test_db_query_facade.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_db_query_facade.py`:

```python
import pytest

from backend.core.db import db, init_db


@pytest.fixture(autouse=True)
async def ensure_schema():
    await init_db()
    await db.execute("DELETE FROM jobs")
    yield
    await db.execute("DELETE FROM jobs")


@pytest.mark.asyncio
async def test_db_facade_fetch_one_and_fetch_all():
    await db.execute(
        "INSERT INTO jobs(type, payload, dedupe_key) VALUES($1, $2::jsonb, $3)",
        "convert",
        "{}",
        "facade-1",
    )
    row = await db.fetch_one("SELECT type, dedupe_key FROM jobs WHERE dedupe_key=$1", "facade-1")
    assert row["type"] == "convert"
    assert row["dedupe_key"] == "facade-1"

    rows = await db.fetch_all("SELECT id FROM jobs WHERE dedupe_key=$1", "facade-1")
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_db_facade_transaction_keeps_statements_on_same_connection():
    async with db.transaction():
        await db.execute(
            "INSERT INTO jobs(type, payload, dedupe_key) VALUES($1, $2::jsonb, $3)",
            "convert",
            "{}",
            "facade-txn",
        )
        row = await db.fetch_one("SELECT dedupe_key FROM jobs WHERE dedupe_key=$1", "facade-txn")

    assert row["dedupe_key"] == "facade-txn"
```

- [ ] **Step 2: 테스트 실패 확인**

```bash
docker compose -f docker/compose.yml up -d db
pytest tests/test_db_query_facade.py -v
```

Expected: ImportError 또는 AttributeError(`backend.core.db.db` 없음).

- [ ] **Step 3: `backend/core/db.py` facade 구현**

상단 import block에 `contextvars`를 추가:

```python
import contextvars
```

Spec-1 Task 11의 `_DB`/`get_db()` 구현 아래, `_PooledConn` 클래스 다음과 `init_db()` 사이에 다음 코드를 추가:

```python
_current_facade_db: contextvars.ContextVar[_DB | None] = contextvars.ContextVar(
    "current_facade_db",
    default=None,
)


def _pack_params(params: tuple[Any, ...]) -> tuple[Any, ...]:
    if len(params) == 1 and isinstance(params[0], (list, tuple)):
        return tuple(params[0])
    return params


class _DBFacade:
    async def _conn(self) -> _DB:
        return _current_facade_db.get() or await get_db()

    async def execute(self, sql: str, *params: Any) -> None:
        conn = await self._conn()
        await conn.execute(sql, _pack_params(params))

    async def fetch_one(self, sql: str, *params: Any) -> asyncpg.Record | None:
        conn = await self._conn()
        async with conn.execute(sql, _pack_params(params)) as cur:
            return await cur.fetchone()

    async def fetch_all(self, sql: str, *params: Any) -> list[asyncpg.Record]:
        conn = await self._conn()
        async with conn.execute(sql, _pack_params(params)) as cur:
            return await cur.fetchall()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator["_DBFacade"]:
        conn = await get_db()
        async with conn.transaction():
            token = _current_facade_db.set(conn)
            try:
                yield self
            finally:
                _current_facade_db.reset(token)


db = _DBFacade()
```

Verify syntax before running tests:
```bash
python -m py_compile backend/core/db.py
```
Expected: no output.

- [ ] **Step 4: 테스트 통과 확인**

```bash
pytest tests/test_db_query_facade.py -v
```

Expected: 2 PASS.

- [ ] **Step 5: 커밋**

```bash
git add backend/core/db.py tests/test_db_query_facade.py
git commit -m "Make queue repositories use an explicit DB facade" -m "Adds a small async facade over the Spec-1 get_db wrapper so queue repositories can use fetch_one/fetch_all without relying on undocumented globals.

Constraint: Spec-1 exposes get_db(), not backend.core.db.db
Confidence: high
Scope-risk: narrow
Tested: pytest tests/test_db_query_facade.py -v
Not-tested: concurrent production transaction load beyond contextvars isolation"
```

---

## Task 1: `init.sql`에 worker control 스키마 추가

**Files:**
- Modify: `docker/db/init.sql`
- Modify: `backend/core/db.py`
- Create: `tests/test_init_sql_workers.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_init_sql_workers.py`:

```python
import pytest

from backend.core.db import db, init_db


@pytest.fixture(autouse=True)
async def ensure_schema():
    await init_db()

@pytest.mark.asyncio
async def test_worker_state_enum_exists():
    rows = await db.fetch_all(
        "SELECT enumlabel FROM pg_enum e "
        "JOIN pg_type t ON e.enumtypid = t.oid "
        "WHERE t.typname = 'worker_state' ORDER BY enumsortorder"
    )
    assert [r["enumlabel"] for r in rows] == [
        "running", "paused", "draining", "stopped",
    ]

@pytest.mark.asyncio
async def test_worker_controls_seeded():
    rows = await db.fetch_all(
        "SELECT worker_id, desired_state FROM worker_controls ORDER BY worker_id"
    )
    assert [(r["worker_id"], r["desired_state"]) for r in rows] == [
        ("converter", "running"),
        ("curation-worker", "running"),
    ]

@pytest.mark.asyncio
async def test_worker_heartbeats_table_exists():
    cols = await db.fetch_all(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'worker_heartbeats' ORDER BY column_name"
    )
    assert {c["column_name"] for c in cols} == {
        "worker_id", "actual_state", "pid", "container_id",
        "last_beat_at", "in_flight_job_id", "detail",
    }
```

- [ ] **Step 2: 테스트 실패 확인**

```bash
docker compose -f docker/compose.yml up -d db
pytest tests/test_init_sql_workers.py -v
```

Expected: 3 fails(`undefined enum`/`relation does not exist`).

- [ ] **Step 3: `docker/db/init.sql`과 `backend/core/db.py` `_SCHEMA_V1`에 같은 schema block 추가**

`docker/db/init.sql`은 primary `curation` DB의 jobs indexes 아래에 추가한다. `backend/core/db.py`의 `_SCHEMA_V1` 문자열에도 같은 block을 jobs indexes 아래에 추가한다. 이유: Spec-1 init.sql은 `curation_test`에 application table을 직접 만들지 않고 pytest가 `init_db()`로 `_SCHEMA_V1`을 적용한다.

```sql
-- Worker control plane (API-only Worker Control 보강안 §4)
DO $$ BEGIN
  CREATE TYPE worker_state AS ENUM ('running', 'paused', 'draining', 'stopped');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS worker_controls (
    worker_id     TEXT PRIMARY KEY,
    desired_state worker_state NOT NULL DEFAULT 'running',
    updated_by    TEXT,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    note          TEXT
);

CREATE TABLE IF NOT EXISTS worker_heartbeats (
    worker_id        TEXT PRIMARY KEY,
    actual_state     worker_state NOT NULL,
    pid              INTEGER,
    container_id     TEXT,
    last_beat_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    in_flight_job_id BIGINT REFERENCES jobs(id),
    detail           JSONB
);
CREATE INDEX IF NOT EXISTS idx_worker_heartbeats_recent
    ON worker_heartbeats(last_beat_at);

INSERT INTO worker_controls (worker_id, desired_state)
VALUES ('converter', 'running'), ('curation-worker', 'running')
ON CONFLICT (worker_id) DO NOTHING;
```

- [ ] **Step 4: DB 재생성 후 테스트 통과 확인**

```bash
docker compose -f docker/compose.yml down -v
docker compose -f docker/compose.yml up -d db
pytest tests/test_init_sql_workers.py -v
```

Expected: 3 PASS.

- [ ] **Step 5: 커밋**

```bash
git add docker/db/init.sql backend/core/db.py tests/test_init_sql_workers.py
git commit -m "Establish API-managed worker control tables" -m "Adds worker_controls and worker_heartbeats to both first-boot SQL and the pytest init schema so production compose and curation_test share the same worker-control contract.

Constraint: Spec-1 creates application tables in curation_test through backend.core.db.init_db()
Confidence: high
Scope-risk: narrow
Tested: pytest tests/test_init_sql_workers.py -v
Not-tested: migration of an existing non-empty production Postgres volume"
```

---

## Task 2: jobs 저장소 (`backend/jobs/repo.py`)

**Files:**
- Create: `backend/jobs/__init__.py`, `backend/jobs/repo.py`
- Create: `tests/test_jobs_repo.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_jobs_repo.py`:

```python
from datetime import datetime, timedelta, timezone

import pytest
from backend.jobs import repo
from backend.core.db import db, init_db

@pytest.fixture(autouse=True)
async def clean_jobs():
    await init_db()
    await db.execute("DELETE FROM worker_heartbeats")
    await db.execute("DELETE FROM jobs")
    yield
    await db.execute("DELETE FROM worker_heartbeats")
    await db.execute("DELETE FROM jobs")

@pytest.mark.asyncio
async def test_enqueue_returns_id_and_default_status():
    row = await repo.enqueue(type_="convert", payload={"cell": "a/b"})
    assert row["id"] > 0
    assert row["status"] == "queued"
    fetched = await repo.fetch(row["id"])
    assert fetched["payload"] == {"cell": "a/b"}

@pytest.mark.asyncio
async def test_enqueue_rejects_duplicate_dedupe_key():
    await repo.enqueue(type_="convert", payload={}, dedupe_key="cell:a/b")
    with pytest.raises(repo.DuplicateDedupe) as ei:
        await repo.enqueue(type_="convert", payload={}, dedupe_key="cell:a/b")
    assert ei.value.existing_job_id > 0

@pytest.mark.asyncio
async def test_request_cancel_marks_running_job():
    enq = await repo.enqueue(type_="convert", payload={})
    await db.execute(
        "UPDATE jobs SET status='running', worker_id='converter' WHERE id=$1",
        enq["id"],
    )
    cancelled = await repo.request_cancel(enq["id"])
    assert cancelled["status"] == "cancel_requested"

@pytest.mark.asyncio
async def test_request_cancel_rejects_terminal_job():
    enq = await repo.enqueue(type_="convert", payload={})
    await db.execute("UPDATE jobs SET status='complete' WHERE id=$1", enq["id"])
    with pytest.raises(repo.AlreadyTerminal):
        await repo.request_cancel(enq["id"])

@pytest.mark.asyncio
async def test_list_jobs_filters_by_status_dataset_and_since():
    old_since = datetime.now(timezone.utc) - timedelta(seconds=1)
    keep = await repo.enqueue(type_="convert", payload={"dataset_id": 7})
    await repo.enqueue(type_="convert", payload={"dataset_id": 8})
    await db.execute("UPDATE jobs SET status='running' WHERE id=$1", keep["id"])

    rows = await repo.list_jobs(status="running", dataset_id=7, since=old_since)
    assert [r["id"] for r in rows] == [keep["id"]]
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_jobs_repo.py -v`
Expected: ImportError(`backend.jobs`).

- [ ] **Step 3: 구현**

`backend/jobs/__init__.py` (빈 파일).

`backend/jobs/repo.py`:

```python
"""SQL access for the unified jobs queue."""
from __future__ import annotations
import json
from datetime import datetime
from typing import Any, Mapping

import asyncpg

from backend.core.db import db


class DuplicateDedupe(Exception):
    def __init__(self, existing_job_id: int) -> None:
        super().__init__(f"duplicate dedupe key for job {existing_job_id}")
        self.existing_job_id = existing_job_id


class AlreadyTerminal(Exception):
    def __init__(self, current_status: str) -> None:
        super().__init__(f"job already terminal: {current_status}")
        self.current_status = current_status


_TERMINAL = {"complete", "failed", "cancelled"}


async def enqueue(
    *,
    type_: str,
    payload: Mapping[str, Any],
    dedupe_key: str | None = None,
    requested_by: str | None = None,
) -> Mapping[str, Any]:
    if dedupe_key is not None:
        existing = await db.fetch_one(
            "SELECT id FROM jobs "
            "WHERE type = $1 AND dedupe_key = $2 "
            "AND status IN ('queued', 'running', 'cancel_requested')",
            type_, dedupe_key,
        )
        if existing is not None:
            raise DuplicateDedupe(existing_job_id=existing["id"])
    try:
        row = await db.fetch_one(
            "INSERT INTO jobs (type, payload, dedupe_key) "
            "VALUES ($1, $2::jsonb, $3) "
            "RETURNING id, type, status, dedupe_key, created_at",
            type_, _to_jsonb(payload), dedupe_key,
        )
    except asyncpg.UniqueViolationError:
        existing = await db.fetch_one(
            "SELECT id FROM jobs "
            "WHERE type = $1 AND dedupe_key = $2 "
            "AND status IN ('queued', 'running', 'cancel_requested')",
            type_, dedupe_key,
        )
        if existing is not None:
            raise DuplicateDedupe(existing_job_id=existing["id"])
        raise
    assert row is not None
    return dict(row)


async def fetch(job_id: int) -> Mapping[str, Any] | None:
    row = await db.fetch_one(
        "SELECT id, type, status, payload, progress, result, error, "
        "       attempts, worker_id, dedupe_key, created_at, updated_at, "
        "       started_at, heartbeat_at, cancel_requested_at, finished_at "
        "FROM jobs WHERE id = $1",
        job_id,
    )
    return _decode_job(row) if row is not None else None


async def list_jobs(
    *,
    type_: str | None = None,
    status: str | None = None,
    dataset_id: int | None = None,
    since: datetime | None = None,
    limit: int = 100,
) -> list[Mapping[str, Any]]:
    clauses: list[str] = []
    args: list[Any] = []
    if type_ is not None:
        args.append(type_); clauses.append(f"type = ${len(args)}")
    if status is not None:
        args.append(status); clauses.append(f"status = ${len(args)}")
    if dataset_id is not None:
        args.append(str(dataset_id)); clauses.append(f"payload->>'dataset_id' = ${len(args)}")
    if since is not None:
        args.append(since); clauses.append(f"updated_at >= ${len(args)}")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    args.append(limit)
    rows = await db.fetch_all(
        f"SELECT id, type, status, payload, created_at, updated_at, "
        f"       started_at, heartbeat_at, cancel_requested_at, finished_at, error "
        f"FROM jobs {where} "
        f"ORDER BY id DESC LIMIT ${len(args)}",
        *args,
    )
    return [_decode_job(row) for row in rows]


async def request_cancel(job_id: int) -> Mapping[str, Any]:
    row = await db.fetch_one("SELECT status FROM jobs WHERE id = $1", job_id)
    if row is None:
        raise LookupError(job_id)
    if row["status"] in _TERMINAL:
        raise AlreadyTerminal(current_status=row["status"])
    updated = await db.fetch_one(
        "UPDATE jobs "
        "SET status = 'cancel_requested', cancel_requested_at = NOW(), updated_at = NOW() "
        "WHERE id = $1 AND status IN ('queued', 'running') "
        "RETURNING id, status",
        job_id,
    )
    return dict(updated) if updated is not None else {"id": job_id, "status": "cancel_requested"}


def _to_jsonb(payload: Mapping[str, Any]) -> str:
    return json.dumps(dict(payload))


def _decode_job(row: Mapping[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    for key in ("payload", "progress", "result"):
        value = decoded.get(key)
        if isinstance(value, str):
            decoded[key] = json.loads(value)
    return decoded
```

Verify syntax before running tests:
```bash
python -m py_compile backend/jobs/repo.py
```
Expected: no output.

- [ ] **Step 4: 테스트 통과 확인**

```bash
pytest tests/test_jobs_repo.py -v
```

Expected: 5 PASS.

- [ ] **Step 5: 커밋**

```bash
git add backend/jobs tests/test_jobs_repo.py
git commit -m "Add a concurrency-safe jobs repository" -m "Creates the reusable jobs queue repository with decoded JSON payloads, duplicate dedupe protection, list filters, and cooperative cancel status updates.

Constraint: jobs schema is owned by Spec-1 and has no requested_by column
Rejected: Store requested_by as a new jobs column | schema expansion belongs in the upstream jobs schema plan
Confidence: high
Scope-risk: narrow
Tested: pytest tests/test_jobs_repo.py -v
Not-tested: multi-process duplicate enqueue race beyond asyncpg unique violation handling"
```

---

## Task 3: jobs 라우터 (`POST/GET /api/jobs`, `POST /api/jobs/:id/cancel`)

**Files:**
- Create: `backend/jobs/router.py`
- Modify: `backend/main.py` (라우터 include)
- Create: `tests/test_jobs_router.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_jobs_router.py`:

```python
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from backend.main import app
from backend.core.db import db, init_db


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

@pytest.fixture(autouse=True)
async def clean():
    await init_db()
    await db.execute("DELETE FROM worker_heartbeats")
    await db.execute("DELETE FROM jobs")
    yield
    await db.execute("DELETE FROM worker_heartbeats")
    await db.execute("DELETE FROM jobs")

@pytest.mark.asyncio
async def test_post_jobs_creates_queued():
    async with _client() as ac:
        r = await ac.post(
            "/api/jobs",
            json={"type": "convert", "payload": {"cell": "a/b"}},
            headers={"X-User-Name": "tester"},
        )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "queued"
    assert body["type"] == "convert"
    assert body["id"] > 0

@pytest.mark.asyncio
async def test_post_jobs_409_on_duplicate_dedupe():
    async with _client() as ac:
        first = await ac.post(
            "/api/jobs",
            json={"type": "convert", "payload": {}, "dedupe_key": "k1"},
        )
        assert first.status_code == 201
        dup = await ac.post(
            "/api/jobs",
            json={"type": "convert", "payload": {}, "dedupe_key": "k1"},
        )
    assert dup.status_code == 409
    assert dup.json()["error"] == "duplicate_dedupe_key"
    assert dup.json()["existing_job_id"] == first.json()["id"]

@pytest.mark.asyncio
async def test_get_job_by_id():
    async with _client() as ac:
        created = (await ac.post("/api/jobs", json={"type": "convert", "payload": {}})).json()
        r = await ac.get(f"/api/jobs/{created['id']}")
    assert r.status_code == 200
    assert r.json()["id"] == created["id"]

@pytest.mark.asyncio
async def test_get_jobs_filters_running_since():
    since = datetime.now(timezone.utc)
    async with _client() as ac:
        created = (await ac.post(
            "/api/jobs",
            json={"type": "convert", "payload": {"dataset_id": 7}},
        )).json()
        await db.execute("UPDATE jobs SET status='running' WHERE id=$1", created["id"])
        r = await ac.get("/api/jobs", params={"status": "running", "dataset_id": 7, "since": since.isoformat()})
    assert r.status_code == 200
    assert [job["id"] for job in r.json()] == [created["id"]]

@pytest.mark.asyncio
async def test_post_cancel_running_job():
    async with _client() as ac:
        created = (await ac.post("/api/jobs", json={"type": "convert", "payload": {}})).json()
        await db.execute("UPDATE jobs SET status='running' WHERE id=$1", created["id"])
        r = await ac.post(f"/api/jobs/{created['id']}/cancel")
    assert r.status_code == 202
    assert r.json()["status"] == "cancel_requested"

@pytest.mark.asyncio
async def test_post_cancel_409_on_terminal():
    async with _client() as ac:
        created = (await ac.post("/api/jobs", json={"type": "convert", "payload": {}})).json()
        await db.execute("UPDATE jobs SET status='complete' WHERE id=$1", created["id"])
        r = await ac.post(f"/api/jobs/{created['id']}/cancel")
    assert r.status_code == 409
    assert r.json()["error"] == "already_terminal"
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_jobs_router.py -v`
Expected: 404 또는 ImportError.

- [ ] **Step 3: `backend/jobs/router.py` 구현**

`backend/jobs/router.py`:

```python
"""REST surface for the unified jobs queue."""
from __future__ import annotations
from datetime import datetime
from typing import Any, Literal
from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field
from backend.jobs import repo


router = APIRouter(prefix="/api/jobs", tags=["jobs"])


class EnqueueBody(BaseModel):
    type: Literal["convert", "split", "merge", "delete", "sync_good_episodes", "stamp_cycles"]
    payload: dict[str, Any] = Field(default_factory=dict)
    dedupe_key: str | None = None


@router.post("", status_code=201)
async def post_job(
    body: EnqueueBody,
    x_user_name: str | None = Header(default=None),
) -> dict[str, Any]:
    try:
        row = await repo.enqueue(
            type_=body.type,
            payload=body.payload,
            dedupe_key=body.dedupe_key,
            requested_by=x_user_name,
        )
    except repo.DuplicateDedupe as e:
        raise HTTPException(
            status_code=409,
            detail={"error": "duplicate_dedupe_key", "existing_job_id": e.existing_job_id},
        )
    return dict(row)


@router.get("/{job_id}")
async def get_job(job_id: int) -> dict[str, Any]:
    row = await repo.fetch(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    return dict(row)


@router.get("")
async def list_jobs(
    type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    dataset_id: int | None = Query(default=None),
    since: datetime | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    rows = await repo.list_jobs(
        type_=type, status=status, dataset_id=dataset_id, since=since, limit=limit,
    )
    return [dict(r) for r in rows]


@router.post("/{job_id}/cancel", status_code=202)
async def cancel_job(job_id: int) -> dict[str, Any]:
    try:
        row = await repo.request_cancel(job_id)
    except LookupError:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    except repo.AlreadyTerminal as e:
        raise HTTPException(
            status_code=409,
            detail={"error": "already_terminal", "current_status": e.current_status},
        )
    return dict(row)
```

Verify syntax:
```bash
python -m py_compile backend/jobs/router.py
```
Expected: no output.

- [ ] **Step 4: `backend/main.py` include + error handler 추가**

`backend/main.py` 에 include 추가(기존 라우터 include들과 SPA fallback 정의 사이):
```python
from backend.jobs.router import router as jobs_router
app.include_router(jobs_router)
```

테스트의 `r.json()["error"]` 가 통과하려면 `HTTPException(detail=dict)` 의 dict이 응답 body로 풀어져야 한다. FastAPI의 기본은 `{"detail": {...}}` 형태로 감싸므로, 다음 핸들러를 `backend/main.py` (또는 신규 `backend/core/errors.py`)에 추가:

```python
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

@app.exception_handler(HTTPException)
async def _flatten_dict_detail(_: Request, exc: HTTPException) -> JSONResponse:
    payload = exc.detail if isinstance(exc.detail, dict) else {"error": str(exc.detail)}
    return JSONResponse(payload, status_code=exc.status_code)
```

(앱 단위 핸들러 1개로 jobs 라우터·workers 라우터 모두 일관되게 펴진다.)

Verify syntax:
```bash
python -m py_compile backend/main.py
```
Expected: no output.

- [ ] **Step 5: 테스트 통과 확인**

Run: `pytest tests/test_jobs_router.py -v`
Expected: 6 PASS.

- [ ] **Step 6: 커밋**

```bash
git add backend/jobs/router.py backend/main.py tests/test_jobs_router.py
git commit -m "Expose the jobs queue over REST" -m "Adds the /api/jobs create/list/get/cancel endpoints with top-level error payloads and httpx ASGITransport-backed route tests.

Constraint: httpx 0.28 removed AsyncClient(app=...)
Confidence: high
Scope-risk: moderate
Tested: pytest tests/test_jobs_router.py -v
Not-tested: browser polling against /api/jobs?since= under live nginx"
```

---

## Task 4: workers 저장소 (`backend/workers/repo.py`)

**Files:**
- Create: `backend/workers/__init__.py`, `backend/workers/repo.py`
- Create: `tests/test_workers_repo.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_workers_repo.py`:

```python
import pytest
from datetime import datetime, timedelta, timezone
from backend.workers import repo
from backend.core.db import db, init_db

@pytest.fixture(autouse=True)
async def clean_heartbeats():
    await init_db()
    await db.execute("DELETE FROM worker_heartbeats")
    await db.execute(
        "UPDATE worker_controls "
        "SET desired_state='running', note=NULL, updated_by=NULL"
    )
    yield
    await db.execute("DELETE FROM worker_heartbeats")

@pytest.mark.asyncio
async def test_list_workers_includes_seeded_rows():
    rows = await repo.list_workers()
    ids = {r["worker_id"] for r in rows}
    assert {"converter", "curation-worker"} <= ids

@pytest.mark.asyncio
async def test_upsert_heartbeat_and_read():
    await repo.upsert_heartbeat(
        worker_id="converter",
        actual_state="running",
        pid=42,
        container_id="abc",
        in_flight_job_id=None,
        detail={"note": "hi"},
    )
    row = await repo.get_worker("converter")
    assert row["actual_state"] == "running"
    assert row["pid"] == 42
    assert row["detail"] == {"note": "hi"}
    assert row["last_beat_at"] is not None

@pytest.mark.asyncio
async def test_set_desired_state_records_actor():
    await repo.set_desired_state(
        worker_id="converter", desired_state="paused",
        updated_by="tester", note="manual pause",
    )
    row = await repo.get_worker("converter")
    assert row["desired_state"] == "paused"
    assert row["updated_by"] == "tester"
    assert row["note"] == "manual pause"

@pytest.mark.asyncio
async def test_is_stale_when_heartbeat_old():
    old = datetime.now(timezone.utc) - timedelta(seconds=60)
    await db.execute(
        "INSERT INTO worker_heartbeats(worker_id, actual_state, last_beat_at) "
        "VALUES('converter','running',$1) "
        "ON CONFLICT(worker_id) DO UPDATE SET last_beat_at=$1", old,
    )
    assert await repo.is_stale("converter", ttl_seconds=30) is True
    fresh = datetime.now(timezone.utc)
    await db.execute(
        "UPDATE worker_heartbeats SET last_beat_at=$1 WHERE worker_id='converter'", fresh,
    )
    assert await repo.is_stale("converter", ttl_seconds=30) is False
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_workers_repo.py -v`
Expected: ImportError.

- [ ] **Step 3: 구현**

`backend/workers/__init__.py` (빈 파일).

`backend/workers/repo.py`:

```python
"""Access layer for worker_controls and worker_heartbeats."""
from __future__ import annotations
import json
from typing import Any, Mapping
from backend.core.db import db


async def list_workers() -> list[Mapping[str, Any]]:
    rows = await db.fetch_all(
        "SELECT c.worker_id, c.desired_state, c.updated_by, c.updated_at, c.note, "
        "       h.actual_state, h.pid, h.container_id, h.last_beat_at, "
        "       h.in_flight_job_id, h.detail "
        "FROM worker_controls c "
        "LEFT JOIN worker_heartbeats h USING (worker_id) "
        "ORDER BY c.worker_id"
    )
    return [_decode_worker(row) for row in rows]


async def get_worker(worker_id: str) -> Mapping[str, Any] | None:
    row = await db.fetch_one(
        "SELECT c.worker_id, c.desired_state, c.updated_by, c.updated_at, c.note, "
        "       h.actual_state, h.pid, h.container_id, h.last_beat_at, "
        "       h.in_flight_job_id, h.detail "
        "FROM worker_controls c "
        "LEFT JOIN worker_heartbeats h USING (worker_id) "
        "WHERE c.worker_id = $1",
        worker_id,
    )
    return _decode_worker(row) if row is not None else None


async def set_desired_state(
    *, worker_id: str, desired_state: str,
    updated_by: str | None, note: str | None,
) -> None:
    await db.execute(
        "UPDATE worker_controls "
        "SET desired_state=$2, updated_by=$3, note=$4, updated_at=NOW() "
        "WHERE worker_id=$1",
        worker_id, desired_state, updated_by, note,
    )


async def upsert_heartbeat(
    *, worker_id: str, actual_state: str, pid: int | None,
    container_id: str | None, in_flight_job_id: int | None,
    detail: Mapping[str, Any] | None,
) -> None:
    await db.execute(
        "INSERT INTO worker_heartbeats "
        "(worker_id, actual_state, pid, container_id, in_flight_job_id, detail) "
        "VALUES($1,$2,$3,$4,$5,$6::jsonb) "
        "ON CONFLICT(worker_id) DO UPDATE "
        "SET actual_state=excluded.actual_state, pid=excluded.pid, "
        "    container_id=excluded.container_id, "
        "    in_flight_job_id=excluded.in_flight_job_id, "
        "    detail=excluded.detail, last_beat_at=NOW()",
        worker_id, actual_state, pid, container_id, in_flight_job_id,
        json.dumps(dict(detail or {})),
    )


async def is_stale(worker_id: str, *, ttl_seconds: int) -> bool:
    row = await db.fetch_one(
        "SELECT last_beat_at < NOW() - ($2 * interval '1 second') AS stale "
        "FROM worker_heartbeats WHERE worker_id = $1",
        worker_id, ttl_seconds,
    )
    if row is None:
        return True
    return bool(row["stale"])


def _decode_worker(row: Mapping[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    if isinstance(decoded.get("detail"), str):
        decoded["detail"] = json.loads(decoded["detail"])
    return decoded
```

Verify syntax before running tests:
```bash
python -m py_compile backend/workers/repo.py
```
Expected: no output.

- [ ] **Step 4: 통과 확인**

Run: `pytest tests/test_workers_repo.py -v`
Expected: 4 PASS.

- [ ] **Step 5: 커밋**

```bash
git add backend/workers/__init__.py backend/workers/repo.py tests/test_workers_repo.py
git commit -m "Add worker control and heartbeat repositories" -m "Creates the worker repository layer with decoded JSON heartbeat detail, actor tracking, and stale heartbeat detection.

Constraint: worker_controls rows are seeded by Task 1 schema initialization
Confidence: high
Scope-risk: narrow
Tested: pytest tests/test_workers_repo.py -v
Not-tested: stale detection under database clock skew"
```

---

## Task 5: workers 라우터 + 상태 전이 규칙

**Files:**
- Create: `backend/workers/service.py`, `backend/workers/router.py`
- Modify: `backend/main.py`
- Create: `tests/test_workers_router.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_workers_router.py`:

```python
import pytest
from datetime import datetime, timedelta, timezone
from httpx import ASGITransport, AsyncClient
from backend.main import app
from backend.core.db import db, init_db


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

@pytest.fixture(autouse=True)
async def reset():
    await init_db()
    await db.execute("DELETE FROM worker_heartbeats")
    await db.execute(
        "UPDATE worker_controls "
        "SET desired_state='running', note=NULL, updated_by=NULL"
    )
    yield
    await db.execute("DELETE FROM worker_heartbeats")

async def _seed_fresh_heartbeat(worker_id: str, state: str = "running") -> None:
    await db.execute(
        "INSERT INTO worker_heartbeats(worker_id, actual_state) VALUES($1,$2) "
        "ON CONFLICT(worker_id) DO UPDATE SET actual_state=$2, last_beat_at=NOW()",
        worker_id, state,
    )

@pytest.mark.asyncio
async def test_get_workers_lists_two_seeded():
    async with _client() as ac:
        r = await ac.get("/api/workers")
    assert r.status_code == 200
    ids = {w["worker_id"] for w in r.json()}
    assert {"converter", "curation-worker"} <= ids

@pytest.mark.asyncio
async def test_patch_worker_running_to_paused():
    await _seed_fresh_heartbeat("converter")
    async with _client() as ac:
        r = await ac.patch(
            "/api/workers/converter",
            json={"desired_state": "paused", "note": "smoke"},
            headers={"X-User-Name": "tester"},
        )
    assert r.status_code == 200
    assert r.json()["desired_state"] == "paused"

@pytest.mark.asyncio
async def test_patch_worker_rejects_stale():
    old = datetime.now(timezone.utc) - timedelta(seconds=60)
    await db.execute(
        "INSERT INTO worker_heartbeats(worker_id, actual_state, last_beat_at) "
        "VALUES('converter','running',$1) "
        "ON CONFLICT(worker_id) DO UPDATE SET last_beat_at=$1", old,
    )
    async with _client() as ac:
        r = await ac.patch("/api/workers/converter", json={"desired_state": "paused"})
    assert r.status_code == 422
    assert r.json()["error"] == "worker_stale"

@pytest.mark.asyncio
async def test_patch_worker_illegal_transition_stopped_to_running_requires_note():
    await _seed_fresh_heartbeat("converter")
    await db.execute("UPDATE worker_controls SET desired_state='stopped' WHERE worker_id='converter'")
    async with _client() as ac:
        r = await ac.patch("/api/workers/converter", json={"desired_state": "running"})
    assert r.status_code == 422
    assert r.json()["error"] == "illegal_transition"
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_workers_router.py -v`
Expected: 404/ImportError.

- [ ] **Step 3: service.py 구현 (전이 규칙)**

`backend/workers/service.py`:

```python
"""Worker control state-machine."""
from __future__ import annotations
from backend.workers import repo

STALE_TTL_SECONDS = 30
_VALID_STATES = {"running", "paused", "draining", "stopped"}


class WorkerNotFound(Exception): ...
class WorkerStale(Exception): ...
class IllegalTransition(Exception):
    def __init__(self, frm: str, to: str) -> None:
        super().__init__(f"{frm} → {to}")
        self.frm, self.to = frm, to


def _allowed(frm: str, to: str, *, has_note: bool) -> bool:
    if to not in _VALID_STATES: return False
    if frm == to: return True
    if frm == "running"  and to in {"paused", "draining", "stopped"}: return to != "stopped" or has_note
    if frm == "paused"   and to in {"running", "draining", "stopped"}: return to != "stopped" or has_note
    if frm == "draining" and to in {"running", "stopped"}:             return to != "stopped" or has_note
    if frm == "stopped"  and to == "running":                          return has_note
    return False


async def patch_desired_state(
    *, worker_id: str, desired_state: str,
    note: str | None, updated_by: str | None,
) -> dict:
    current = await repo.get_worker(worker_id)
    if current is None:
        raise WorkerNotFound(worker_id)
    if await repo.is_stale(worker_id, ttl_seconds=STALE_TTL_SECONDS):
        raise WorkerStale(worker_id)
    frm = current["desired_state"]
    if not _allowed(frm, desired_state, has_note=bool(note)):
        raise IllegalTransition(frm, desired_state)
    # draining → running only when no in-flight job
    if frm == "draining" and desired_state == "running" and current["in_flight_job_id"]:
        raise IllegalTransition(frm, desired_state)
    await repo.set_desired_state(
        worker_id=worker_id, desired_state=desired_state,
        updated_by=updated_by, note=note,
    )
    fresh = await repo.get_worker(worker_id)
    return dict(fresh) if fresh else {}
```

Verify syntax:
```bash
python -m py_compile backend/workers/service.py
```
Expected: no output.

- [ ] **Step 4: `backend/workers/router.py` 구현**

`backend/workers/router.py`:

```python
"""REST surface for worker control plane."""
from __future__ import annotations
from typing import Any, Literal
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from backend.workers import repo, service


router = APIRouter(prefix="/api/workers", tags=["workers"])


class PatchBody(BaseModel):
    desired_state: Literal["running", "paused", "draining", "stopped"]
    note: str | None = None


@router.get("")
async def list_workers() -> list[dict[str, Any]]:
    return [dict(r) for r in await repo.list_workers()]


@router.get("/{worker_id}")
async def get_worker(worker_id: str) -> dict[str, Any]:
    row = await repo.get_worker(worker_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    return dict(row)


@router.patch("/{worker_id}")
async def patch_worker(
    worker_id: str, body: PatchBody,
    x_user_name: str | None = Header(default=None),
) -> dict[str, Any]:
    try:
        return await service.patch_desired_state(
            worker_id=worker_id, desired_state=body.desired_state,
            note=body.note, updated_by=x_user_name,
        )
    except service.WorkerNotFound:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    except service.WorkerStale:
        row = await repo.get_worker(worker_id)
        raise HTTPException(
            status_code=422,
            detail={
                "error": "worker_stale",
                "last_beat_at": row["last_beat_at"].isoformat() if row and row["last_beat_at"] else None,
            },
        )
    except service.IllegalTransition as e:
        raise HTTPException(
            status_code=422,
            detail={"error": "illegal_transition", "from": e.frm, "to": e.to},
        )
```

Verify syntax:
```bash
python -m py_compile backend/workers/router.py
```
Expected: no output.

- [ ] **Step 5: `backend/main.py` workers 라우터 include 추가**

`backend/main.py`:
```python
from backend.workers.router import router as workers_router
app.include_router(workers_router)
```

(Task 3에서 추가한 `_flatten_dict_detail` 핸들러가 dict detail을 최상위로 펴주므로 별도 라우터 핸들러 불필요.)

Verify syntax:
```bash
python -m py_compile backend/main.py
```
Expected: no output.

- [ ] **Step 6: 통과 확인**

```bash
pytest tests/test_workers_router.py -v
```

Expected: 4 PASS.

- [ ] **Step 7: 커밋**

```bash
git add backend/workers/service.py backend/workers/router.py backend/main.py tests/test_workers_router.py
git commit -m "Expose worker desired-state control over REST" -m "Adds /api/workers list/get/patch endpoints and enforces stale-heartbeat and state-transition guards before accepting desired-state changes.

Constraint: heartbeat freshness is the API guardrail for safe worker control
Confidence: high
Scope-risk: moderate
Tested: pytest tests/test_workers_router.py -v
Not-tested: simultaneous PATCH requests for the same worker"
```

---

## Task 6: 워커 베이스 루프 (`backend/workers/runtime.py`)

**Files:**
- Create: `backend/workers/runtime.py`
- Create: `tests/test_workers_runtime.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_workers_runtime.py`:

```python
import pytest
from backend.workers import runtime
from backend.workers import repo as workers_repo
from backend.jobs import repo as jobs_repo
from backend.core.db import db, init_db

@pytest.fixture(autouse=True)
async def clean():
    await init_db()
    await db.execute("DELETE FROM worker_heartbeats")
    await db.execute("DELETE FROM jobs")
    await db.execute("UPDATE worker_controls SET desired_state='running', note=NULL")
    yield
    await db.execute("DELETE FROM worker_heartbeats")
    await db.execute("DELETE FROM jobs")


@pytest.mark.asyncio
async def test_runtime_skips_when_paused():
    enq = await jobs_repo.enqueue(type_="convert", payload={})
    await db.execute("UPDATE worker_controls SET desired_state='paused' WHERE worker_id='converter'")
    handler_calls = []
    async def handler(job):
        handler_calls.append(job["id"])
    await runtime.tick(
        worker_id="converter", supported_types=["convert"], handler=handler,
        idle_sleep=0,
    )
    assert handler_calls == []  # paused → never picked up
    job = await jobs_repo.fetch(enq["id"])
    assert job["status"] == "queued"


@pytest.mark.asyncio
async def test_runtime_runs_handler_and_marks_complete():
    enq = await jobs_repo.enqueue(type_="convert", payload={"x": 1})
    async def handler(job):
        assert job["payload"]["x"] == 1
    await runtime.tick(
        worker_id="converter", supported_types=["convert"], handler=handler,
        idle_sleep=0,
    )
    job = await jobs_repo.fetch(enq["id"])
    assert job["status"] == "complete"
    worker = await workers_repo.get_worker("converter")
    assert worker["in_flight_job_id"] is None


@pytest.mark.asyncio
async def test_runtime_marks_failed_on_handler_exception():
    enq = await jobs_repo.enqueue(type_="convert", payload={})
    async def handler(job):
        raise RuntimeError("boom")
    await runtime.tick(
        worker_id="converter", supported_types=["convert"], handler=handler,
        idle_sleep=0,
    )
    job = await jobs_repo.fetch(enq["id"])
    assert job["status"] == "failed"
    assert "boom" in job["error"]
    worker = await workers_repo.get_worker("converter")
    assert worker["in_flight_job_id"] is None


@pytest.mark.asyncio
async def test_runtime_handler_can_check_cancel_via_callback():
    enq = await jobs_repo.enqueue(type_="convert", payload={})
    chunks_seen = []
    async def handler(job, *, check_cancel):
        chunks_seen.append("a")
        await jobs_repo.request_cancel(job["id"])  # external cancel
        if await check_cancel():
            return runtime.CancelledNormally(cleanup="partial removed")
        chunks_seen.append("b")
    await runtime.tick(
        worker_id="converter", supported_types=["convert"], handler=handler,
        idle_sleep=0,
    )
    job = await jobs_repo.fetch(enq["id"])
    assert chunks_seen == ["a"]
    assert job["status"] == "cancelled"
    assert "partial removed" in (job["error"] or "")
    assert job["heartbeat_at"] is not None
    worker = await workers_repo.get_worker("converter")
    assert worker["in_flight_job_id"] is None
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_workers_runtime.py -v`
Expected: ImportError.

- [ ] **Step 3: 구현**

`backend/workers/runtime.py`:

```python
"""Long-running worker base loop — claim job, heartbeat, observe cancel."""
from __future__ import annotations
import asyncio, inspect, json, logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Sequence
from backend.core.db import db
from backend.workers import repo as workers_repo

log = logging.getLogger("workers.runtime")


@dataclass
class CancelledNormally:
    cleanup: str = ""


JobHandler = Callable[..., Awaitable[None | CancelledNormally]]


async def tick(
    *, worker_id: str, supported_types: Sequence[str],
    handler: JobHandler, idle_sleep: float = 1.0,
) -> None:
    """Single iteration: heartbeat, check desired_state, claim one job, run."""
    await workers_repo.upsert_heartbeat(
        worker_id=worker_id, actual_state="running", pid=None,
        container_id=None, in_flight_job_id=None, detail=None,
    )
    desired = await db.fetch_one(
        "SELECT desired_state FROM worker_controls WHERE worker_id=$1", worker_id,
    )
    if desired is None or desired["desired_state"] != "running":
        await asyncio.sleep(idle_sleep)
        return
    job = await _claim(worker_id, list(supported_types))
    if job is None:
        await asyncio.sleep(idle_sleep)
        return
    await workers_repo.upsert_heartbeat(
        worker_id=worker_id, actual_state="running", pid=None,
        container_id=None, in_flight_job_id=job["id"], detail=None,
    )

    async def check_cancel() -> bool:
        await db.execute(
            "UPDATE jobs SET heartbeat_at=NOW(), updated_at=NOW() WHERE id=$1",
            job["id"],
        )
        row = await db.fetch_one("SELECT status FROM jobs WHERE id=$1", job["id"])
        return bool(row and row["status"] == "cancel_requested")

    try:
        sig = inspect.signature(handler)
        if "check_cancel" in sig.parameters:
            result = await handler(job, check_cancel=check_cancel)
        else:
            result = await handler(job)
    except Exception as exc:
        await db.execute(
            "UPDATE jobs SET status='failed', finished_at=NOW(), updated_at=NOW(), "
            "error=$2 WHERE id=$1", job["id"], str(exc),
        )
        log.exception("job %s failed", job["id"])
    else:
        if isinstance(result, CancelledNormally):
            await db.execute(
                "UPDATE jobs SET status='cancelled', finished_at=NOW(), updated_at=NOW(), "
                "error=$2 WHERE id=$1", job["id"], result.cleanup or "",
            )
        else:
            await db.execute(
                "UPDATE jobs SET status='complete', finished_at=NOW(), updated_at=NOW() "
                "WHERE id=$1", job["id"],
            )
    finally:
        await workers_repo.upsert_heartbeat(
            worker_id=worker_id, actual_state="running", pid=None,
            container_id=None, in_flight_job_id=None, detail=None,
        )


async def _claim(worker_id: str, types: list[str]) -> dict | None:
    async with db.transaction():
        row = await db.fetch_one(
            "SELECT id, type, payload FROM jobs "
            "WHERE status='queued' AND type = ANY($1::text[]) "
            "ORDER BY created_at "
            "FOR UPDATE SKIP LOCKED LIMIT 1",
            types,
        )
        if row is None:
            return None
        await db.execute(
            "UPDATE jobs SET status='running', worker_id=$2, started_at=NOW(), "
            "heartbeat_at=NOW(), updated_at=NOW() WHERE id=$1", row["id"], worker_id,
        )
        return _decode_claimed_job(row)


def _decode_claimed_job(row: Mapping[str, Any]) -> dict[str, Any]:
    job = dict(row)
    if isinstance(job.get("payload"), str):
        job["payload"] = json.loads(job["payload"])
    return job


async def run_forever(
    *, worker_id: str, supported_types: Sequence[str],
    handler: JobHandler, idle_sleep: float = 1.0,
) -> None:  # pragma: no cover — ops loop
    while True:
        try:
            await tick(
                worker_id=worker_id, supported_types=supported_types,
                handler=handler, idle_sleep=idle_sleep,
            )
        except Exception:
            log.exception("worker %s tick crashed", worker_id)
            await asyncio.sleep(idle_sleep)
```

Verify syntax:
```bash
python -m py_compile backend/workers/runtime.py
```
Expected: no output.

- [ ] **Step 4: 통과 확인**

```bash
pytest tests/test_workers_runtime.py -v
```

Expected: 4 PASS.

- [ ] **Step 5: 커밋**

```bash
git add backend/workers/runtime.py tests/test_workers_runtime.py
git commit -m "Add the cooperative worker runtime foundation" -m "Implements tick/run_forever with pause-aware job claiming, decoded job payloads, heartbeat updates, cooperative cancel checks, terminal status writes, and in-flight heartbeat cleanup.

Constraint: actual converter/curation handlers are wired in the integration plan
Confidence: high
Scope-risk: moderate
Tested: pytest tests/test_workers_runtime.py -v
Not-tested: real converter cleanup behavior after cancel"
```

---

## Final Verification

- [ ] **Step 1: 새 foundation 테스트 전체 실행**

```bash
pytest \
  tests/test_db_query_facade.py \
  tests/test_init_sql_workers.py \
  tests/test_jobs_repo.py \
  tests/test_jobs_router.py \
  tests/test_workers_repo.py \
  tests/test_workers_router.py \
  tests/test_workers_runtime.py \
  -v
```

Expected: 28 PASS.

- [ ] **Step 2: DB 서비스 상태 확인**

```bash
docker compose -f docker/compose.yml ps db
```

Expected: `db` service is running/healthy. If it is unhealthy, inspect `docker compose -f docker/compose.yml logs --tail=100 db` before proceeding to the Integration plan.

---

## Self-Review Notes

- 함수/타입 시그니처 일관성:
  - `backend.core.db.db.execute/fetch_one/fetch_all/transaction` (Task 0) ↔ jobs/workers repositories (Tasks 2/4/6) — 일치.
  - `repo.enqueue(type_=, payload=, dedupe_key=, requested_by=)` (Task 2) ↔ router의 호출 (Task 3) ↔ runtime의 jobs 행 형태 (Task 6) — 일치.
  - `repo.request_cancel(job_id)` (Task 2) ↔ router (Task 3) ↔ runtime의 `cancel_requested` 상태 관찰 (Task 6) — 일치.
  - `service.patch_desired_state(worker_id=, desired_state=, note=, updated_by=)` (Task 5) ↔ router (Task 5) — 일치.
  - `runtime.tick(worker_id=, supported_types=, handler=, idle_sleep=)` 시그니처는 Integration plan Task 1의 `process_one_queued` 가 그대로 호출.
- 검증 지점:
  - Task 0: DB facade fetch/transaction → 2 PASS.
  - Task 1: `init.sql`과 `_SCHEMA_V1`에 enum/테이블/시드가 모두 들어갔는지 → DB 재생성 후 3 PASS.
  - Task 2: enqueue/fetch JSON decode/dedupe/cancel/list filters → 5 PASS.
  - Task 3: REST 6 케이스 (post/dup/get/list/cancel/cancel-409) → 6 PASS.
  - Task 4: list/upsert/set/stale → 4 PASS.
  - Task 5: list/patch/stale-422/illegal-422 → 4 PASS.
  - Task 6: paused-skip/complete/failed/cancel + in-flight cleanup → 4 PASS.
- 명시적 placeholder/TBD 없음. 모든 step은 실행 가능한 코드 또는 명령.
- 다음 plan(Integration)에서 본 plan의 산출을 호출하는 자리:
  - `rosbag2lerobot-svt/auto_converter.py` → `runtime.tick(worker_id="converter", ...)`
  - 프런트 → `POST /api/jobs`, `PATCH /api/workers/:id`, `POST /api/jobs/:id/cancel`
  - host control 코드 제거 후 `_flatten_dict_detail` 핸들러로 응답 통일.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-26-api-only-worker-control-foundations.md`. Two execution options:

1. **Subagent-Driven (recommended)** — task마다 fresh subagent dispatch, task 사이마다 검토.
2. **Inline Execution** — 같은 세션에서 batch 실행, checkpoint마다 일시정지.

본 plan 머지 후 `2026-04-26-api-only-worker-control-integration.md` 로 이어진다.
