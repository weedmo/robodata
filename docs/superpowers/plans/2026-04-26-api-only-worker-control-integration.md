# API-only Worker Control — Integration Plan

> **Verified:** 2026-04-26T00:00Z · Codex(gpt-5.5/xhigh) ↔ Claude · 0 Codex pass + 0 Claude pass · NEEDS-WORK · fixes=0
> **Verification note:** verify-plan skill 미실행. Foundations plan에서 Codex Pass 1이 두 번 모두 5분 cap을 초과하는 패턴이 반복돼, 본 plan(786줄, 단순 코드 제거 + UI 패턴)에 동일 비용을 지불할 가치가 낮다고 판단. 실행 전 Tasks 1~7에 대한 수동 코드리뷰 또는 subagent-driven 모드 실행 시 task별 reviewer 검증을 권장.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Foundations plan에서 만든 jobs/workers API와 워커 베이스 루프를 실제 호출자에 결선한다 — `auto_converter.py`를 jobs 큐 소비자로 전환하고, `backend/converter/` 의 host docker / NAS 시그널 분기를 모두 제거하고, compose 파일에서 docker.sock·`CURATION_CONVERTER_CONTROL_MODE`를 빼고, 프런트의 Convert 버튼·워커 제어 표면을 정돈하고, end-to-end 시나리오로 컨테이너 라이프사이클이 변하지 않음을 검증한다.

**Architecture:**
- 본 plan은 [Foundations plan](./2026-04-26-api-only-worker-control-foundations.md)의 Tasks 1~6이 모두 머지된 상태를 전제로 한다. `backend.workers.runtime.tick`, `backend.jobs.repo.enqueue/request_cancel`, `/api/jobs`, `/api/workers` 가 이미 동작.
- 컨버터 측은 Spec-2 plan(`2026-04-25-converter-db-queue.md`)의 Task 1·2·4(설정·작업 저장소·큐 클라이언트)를 재사용한다. Spec-2의 Task 3(`Route Converter API Through DB Queue Mode`)과 Task 5(`Auto Converter DB Queue Loop`)는 본 plan이 더 강한 형태로 흡수·대체한다 — 본 plan 머지 후 Spec-2 두 task는 제거 대상으로 표기.
- 모든 변경은 TDD로 진행: pytest → docker mockup 확인 → 실 데이터 확인의 CLAUDE.md 순서를 따른다.

**Tech Stack:** Python 3.13 + FastAPI + asyncpg, Postgres 16-alpine, React 19 + TypeScript 5.7, Vite 6, Docker Compose v2, Node 22 built-in test 스타일(`.mjs`), pytest + httpx.AsyncClient, bash.

---

## Prerequisites

본 plan을 시작하기 전에 다음이 모두 머지되어 있어야 한다.

- **Foundations plan 전체 (Tasks 1~6)** — `worker_controls`/`worker_heartbeats` 스키마, `/api/jobs`·`/api/workers` REST, `backend.workers.runtime.tick`·`run_forever` 가 동작.
- **Spec-1 plan Task 1~14** — Postgres + asyncpg 래퍼 + 기본 jobs 테이블.
- **Spec-2 plan Task 1·2·4** — `backend/converter/queue_client.py` 등 컨버터 측 큐 어댑터 모듈 존재.
- 작업 워크트리: `feat/docker-5-service-split` 브랜치에서 직접 작업하거나, 별도 worktree 사용 시 `superpowers:using-git-worktrees`로 격리.

본 plan은 Spec-2 plan Task 3·5와 일부 겹친다. 충돌 회피를 위해 본 plan을 Spec-2 plan보다 먼저 머지하거나, Spec-2 측에서 Task 3·5를 미실행 처리한다.

---

## File Structure

**Modify:**
- `rosbag2lerobot-svt/auto_converter.py` — main loop를 `runtime.run_forever` 기반으로 교체. NAS 폴링 루프 제거.
- `backend/converter/service.py` — host control 함수(`get_converter_control_mode`, `is_host_control_mode`, `is_auto_control_mode`, `read_host_control_info`, `request_host_stop`, `request_host_task` 류) 및 `CONTROL_MODE_*` 상수 일괄 제거.
- `backend/converter/router.py` — host docker/파일 신호 호출 경로 제거. 변환 트리거는 `backend/jobs/repo.enqueue()` 호출로 대체.
- `backend/datasets/routers/*.py` — 직접 변환 트리거 호출이 있다면 동일하게 enqueue로 대체.
- `tests/test_converter_service.py`, `tests/test_converter_validation_router.py`, `tests/test_ui_service_docker_ops.py` — host-mode 가정 테스트 정리.
- `docker/compose.yml` — `app`/`converter`/`curation-worker` 서비스에서 `CURATION_CONVERTER_CONTROL_MODE` env 제거, `app`에 docker.sock이 마운트되지 않음을 보장.
- `docker/ui/docker-compose.yml`, `docker/converter/docker-compose.yml` — 동일 항목 제거(파일이 아직 존재하는 경우만).
- `frontend/src/components/ConverterControls.tsx` — Convert 버튼이 `/api/jobs`로 enqueue, "대기열 추가됨 #N" toast, WorkerControlPill 마운트, "현재 작업 취소" 버튼 분리.
- `frontend/src/components/ConverterProgress.tsx` — 호스트 모드 hint 분기 제거(2026-04-25 plan에서 `cvp-inline-note` 정리됐다면 추가 작업 없음, 잔존 분기만 정돈).
- `frontend/src/api/converter.ts` (또는 동등 위치의 fetch 모듈) — `enqueueConvertJob` helper 추가.
- `README.md` — 호스트 모드 운영 절차 섹션 삭제, API 기반 제어 절차로 대체.

**Create:**
- `tests/test_auto_converter_queue_adapter.py`
- `scripts/verify_no_host_control.sh` — `grep -r CURATION_CONVERTER_CONTROL_MODE …` 류 negative gate.
- `frontend/src/components/WorkerControlPill.tsx` — Pause/Resume + stale 배지 컴포넌트.
- `frontend/tests/converterButtonEnqueue.test.mjs`
- `frontend/tests/workerControlPill.test.mjs`
- `tests/test_end_to_end_api_only_control.py`

**Out of scope** (Foundations plan에서 이미 끝남):
- `worker_controls`/`worker_heartbeats` 스키마
- `/api/jobs`, `/api/workers` 라우터
- `backend.workers.runtime` 베이스 루프

---

## Task 1: 컨버터 큐 어댑터 (parent shim)

**Files:**
- Create: `backend/converter/queue_adapter.py`
- Create: `tests/test_converter_queue_adapter.py`

> **변경 노트 (2026-04-26)**: 원안은 `rosbag2lerobot-svt/auto_converter.py` 본체를
> 직접 수정하는 것이었다. submodule git 메타 끊김 + 200+줄 `convert_task` 재구조화 +
> `rosbag2lerobot_svt` 패키지 이름 매핑 미확인 — 셋이 한 task 안에 묶이면 위험.
> 그래서 parent repo 에 얇은 shim 모듈을 두고 `runtime.tick` 결선만 검증한다.
> 실제 변환 본체 결선(`_run_conversion` → submodule `convert_task` 호출)은 별도 PR
> (submodule 자체 branch 또는 후속 task)로 분리한다. 그때까지 Production 시점에는
> `_run_conversion` 이 `NotImplementedError` 를 raise — 즉 운영 시 Convert 작업은
> 큐에 enqueue 되지만 실제 변환은 일어나지 않는다 (의도적 부분 미연동, Task 7
> e2e smoke 는 monkeypatch 로 우회).

- [ ] **Step 1: 실패 테스트 작성 — shim 단위**

`tests/test_converter_queue_adapter.py`:

```python
import pytest
from backend.jobs import repo as jobs_repo
from backend.core.db import db, init_db
from backend.converter.queue_adapter import process_one_queued

pytestmark = pytest.mark.usefixtures("_point_settings_at_test_db")

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
async def test_picks_up_queued_convert_and_completes(monkeypatch):
    convert_calls = []
    async def fake_convert(payload, *, check_cancel=None):
        convert_calls.append(dict(payload))
    monkeypatch.setattr(
        "backend.converter.queue_adapter._run_conversion", fake_convert,
    )
    enq = await jobs_repo.enqueue(type_="convert", payload={"cell": "a/b"})
    await process_one_queued(idle_sleep=0)
    job = await jobs_repo.fetch(enq["id"])
    assert job["status"] == "complete"
    assert convert_calls == [{"cell": "a/b"}]


@pytest.mark.asyncio
async def test_handles_cancel_mid_chunk(monkeypatch):
    async def with_cancel(payload, *, check_cancel):
        await db.execute(
            "UPDATE jobs SET status='cancel_requested' WHERE status='running'"
        )
        if await check_cancel():
            from backend.workers.runtime import CancelledNormally
            return CancelledNormally(cleanup="partial output removed")
    monkeypatch.setattr(
        "backend.converter.queue_adapter._run_conversion", with_cancel,
    )
    enq = await jobs_repo.enqueue(type_="convert", payload={})
    await process_one_queued(idle_sleep=0)
    job = await jobs_repo.fetch(enq["id"])
    assert job["status"] == "cancelled"
    assert "partial output removed" in (job["error"] or "")


@pytest.mark.asyncio
async def test_default_run_conversion_raises_not_implemented():
    """Production wiring is intentionally deferred — the default impl must
    refuse to silently mark a job complete with no real conversion."""
    enq = await jobs_repo.enqueue(type_="convert", payload={"cell": "real"})
    await process_one_queued(idle_sleep=0)
    job = await jobs_repo.fetch(enq["id"])
    assert job["status"] == "failed"
    assert "NotImplementedError" in (job["error"] or "") or "not implemented" in (job["error"] or "").lower()
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_converter_queue_adapter.py -v`
Expected: ImportError (`backend.converter.queue_adapter` 없음).

- [ ] **Step 3: shim 구현**

`backend/converter/queue_adapter.py`:

```python
"""Queue-driven converter entrypoint.

Bridges the parent-repo worker runtime to the converter implementation. The
real conversion lives in the `rosbag2lerobot-svt` submodule (see
`convert_task` in `rosbag2lerobot-svt/auto_converter.py`); wiring the
submodule's function in here is deferred to a separate PR so this shim can
ship without submodule surgery. Until then, `_run_conversion` raises
NotImplementedError — jobs go to `failed` rather than silently `complete`,
which keeps the API contract honest.

Tests monkeypatch `backend.converter.queue_adapter._run_conversion`.
"""
from __future__ import annotations
from typing import Any, Awaitable, Callable, Mapping

from backend.workers.runtime import (
    CancelledNormally,
    run_forever,
    tick,
)


CheckCancel = Callable[[], Awaitable[bool]]


async def _run_conversion(
    payload: Mapping[str, Any],
    *,
    check_cancel: CheckCancel | None = None,
) -> CancelledNormally | None:
    """Default impl — raises until the real converter is wired in.

    Replaced via monkeypatch in tests. Production wiring will call into the
    rosbag2lerobot-svt submodule's `convert_task` once that adapter PR lands.
    """
    raise NotImplementedError(
        "queue_adapter._run_conversion is not wired to the converter yet — "
        "tests should monkeypatch this and production wiring is a separate PR."
    )


async def _handler(
    job: Mapping[str, Any], *, check_cancel: CheckCancel,
) -> CancelledNormally | None:
    return await _run_conversion(job["payload"], check_cancel=check_cancel)


async def process_one_queued(*, idle_sleep: float = 1.0) -> None:
    """Single runtime tick — used by tests and runbooks."""
    await tick(
        worker_id="converter",
        supported_types=["convert"],
        handler=_handler,
        idle_sleep=idle_sleep,
    )


async def run_converter_forever() -> None:  # pragma: no cover — ops entry point
    await run_forever(
        worker_id="converter",
        supported_types=["convert"],
        handler=_handler,
    )
```

Verify syntax:
```bash
python -m py_compile backend/converter/queue_adapter.py
```
Expected: no output.

- [ ] **Step 4: 테스트 통과 확인**

Run: `pytest tests/test_converter_queue_adapter.py -v`
Expected: 3 PASS.

- [ ] **Step 5: 커밋**

```bash
git add backend/converter/queue_adapter.py tests/test_converter_queue_adapter.py
git commit -m "Add converter queue adapter shim" -m "Wires backend.workers.runtime to a converter handler in the parent repo, leaving submodule surgery for a follow-up PR. The default _run_conversion raises NotImplementedError so unwired production paths fail loudly instead of silently marking jobs complete.

Constraint: rosbag2lerobot-svt is a separate submodule with its own PR lifecycle
Rejected: Edit submodule auto_converter.py from this PR | scope creep + dangling submodule reference risk
Confidence: high
Scope-risk: narrow
Tested: pytest tests/test_converter_queue_adapter.py -v
Not-tested: real submodule conversion path (deferred to follow-up PR)"
```

---

## Task 2: NAS 시그널 파일 IO + control_mode 분기 제거

**Files:**
- Modify: `backend/converter/service.py`
- Modify: `backend/converter/router.py`
- Modify: `tests/test_converter_service.py`, `tests/test_converter_validation_router.py`, `tests/test_ui_service_docker_ops.py`
- Create: `scripts/verify_no_host_control.sh`

- [ ] **Step 1: negative gate 스크립트 작성**

`scripts/verify_no_host_control.sh`:

```bash
#!/usr/bin/env bash
# Fails non-zero if any host-mode control artifact is still referenced.
set -uo pipefail

errs=0
check() {
  local pattern="$1" label="$2"
  if grep -RnE "$pattern" backend/ docker/ 2>/dev/null; then
    echo "::error:: $label still present"; errs=$((errs+1))
  fi
}
check 'CURATION_CONVERTER_CONTROL_MODE' 'control mode env'
check 'CONTROL_MODE_(AUTO|DOCKER|HOST)' 'control mode constants'
check 'host_runtime\.json|stop\.flag|task_request\.json' 'NAS signal files'
check '/var/run/docker\.sock' 'docker socket mount'
exit "$errs"
```

```bash
chmod +x scripts/verify_no_host_control.sh
```

- [ ] **Step 2: 게이트가 빨간색인지 확인**

```bash
./scripts/verify_no_host_control.sh
```
Expected: 4 카테고리 모두 매칭 → exit 4.

- [ ] **Step 3: `backend/converter/service.py` 정리**

`backend/converter/service.py` 에서 다음을 **삭제**한다:
- `CONTROL_MODE_ENV`, `CONTROL_MODE_AUTO`, `CONTROL_MODE_DOCKER`, `CONTROL_MODE_HOST`, `CONTROL_MODES`
- `get_converter_control_mode()`, `is_host_control_mode()`, `is_auto_control_mode()`
- `read_host_control_info()`, `_parse_host_runtime_timestamp()`, `HostControlInfo` 데이터클래스
- `request_host_stop()`, `request_host_task()` (실제 함수명에 맞춰)
- `host_runtime.json` / `stop.flag` / `task_request.json` 경로 상수와 IO 코드
- 위 식별자에 의존하던 분기(`if control_mode == ...:` 등)는 단일 경로로 치환

`backend/converter/router.py` 에서:
- `control_mode` 분기 제거
- "변환 시작/중지" 핸들러를 `backend/jobs/repo.enqueue()` / `backend/jobs/repo.request_cancel()` 호출로 치환. (이미 Foundations plan §Task 3 의 `/api/jobs` 가 표준이므로 이 라우터는 얇은 어댑터로 유지하거나 deprecate 표시.)

`backend/datasets/routers/*.py` 에서:
- 직접 변환 트리거 호출 (예: `start_conversion(...)`) 이 있다면 `await backend.jobs.repo.enqueue(type_="convert", payload=...)` 로 치환.

기존 테스트 정리:
- `tests/test_converter_service.py`, `tests/test_converter_validation_router.py`, `tests/test_ui_service_docker_ops.py` 에서 host control / docker mode를 직접 호출/모킹하는 케이스 제거 또는 `/api/jobs` 호출로 재작성.
- 남는 테스트는 큐 enqueue → status 확인 흐름만 검증.

- [ ] **Step 4: 게이트가 녹색인지 + pytest 회귀 확인**

```bash
./scripts/verify_no_host_control.sh && echo OK
pytest tests/test_converter_service.py tests/test_converter_validation_router.py \
       tests/test_ui_service_docker_ops.py -v
```
Expected: gate exit 0, pytest green (수정한 케이스 포함).

- [ ] **Step 5: 커밋**

```bash
git add backend/converter scripts/verify_no_host_control.sh \
        tests/test_converter_service.py tests/test_converter_validation_router.py \
        tests/test_ui_service_docker_ops.py
git commit -m "refactor(converter): remove host-mode control plane (env, files, branches)"
```

---

## Task 3: compose 파일에서 host-mode env / docker.sock 제거

**Files:**
- Modify: `docker/compose.yml`
- Modify: `docker/ui/docker-compose.yml` (Spec-1 Task 19에서 곧 제거되지만, 본 plan 머지 시점이 그보다 빠른 경우만 정리)
- Modify: `docker/converter/docker-compose.yml` (마찬가지)

- [ ] **Step 1: 변경 전 게이트 확인**

```bash
./scripts/verify_no_host_control.sh
```
Expected: docker socket / env 매칭 잔존 → exit ≥1.

- [ ] **Step 2: `docker/compose.yml` 의 `app` 서비스에서 다음 보장**

```yaml
services:
  app:
    # 기존 env 유지하되 다음 줄은 명시적으로 부재해야 함:
    #   CURATION_CONVERTER_CONTROL_MODE: host
    # volumes에 다음 항목이 없어야 함:
    #   - /var/run/docker.sock:/var/run/docker.sock
    environment:
      CURATION_HOST: 0.0.0.0
      CURATION_FASTAPI_PORT: 8001
      CURATION_DB_URL: postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB}
      CURATION_RERUN_GRPC_URL: rerun+grpc://rerun:9876
      CURATION_DATASET_ROOT_BASE: ${CURATION_DATA_ROOT}
      CURATION_ROSBAG_TO_LEROBOT_REPO_PATH: /app/rosbag2lerobot-svt
```

`converter` 서비스에서도 `CURATION_CONVERTER_CONTROL_MODE` 제거.

- [ ] **Step 3: 레거시 compose 파일에서 동일 항목 제거(파일이 아직 존재한다면)**

`docker/ui/docker-compose.yml` 의 `CURATION_CONVERTER_CONTROL_MODE: host` 줄 삭제.
`docker/converter/docker-compose.yml` 의 동일 항목 삭제.
(이 두 파일은 Spec-1 Task 19에서 완전히 제거된다. 본 task는 머지 순서에 따라 안전망일 뿐.)

- [ ] **Step 4: 게이트 + 컨테이너 검증**

```bash
./scripts/verify_no_host_control.sh   # exit 0 기대
docker compose -f docker/compose.yml down -v
docker compose -f docker/compose.yml up -d --build app converter db
APP_NAME=$(docker compose -f docker/compose.yml ps -q app)
docker exec "$APP_NAME" ls /var/run/docker.sock 2>&1 | tee /tmp/sock.txt || true
grep -q "No such file" /tmp/sock.txt
docker exec "$APP_NAME" env | grep -i CURATION_CONVERTER_CONTROL_MODE && exit 1 || echo "env clean"
```

Expected: `grep -q "No such file"` 통과, env grep `exit 1` 분기 안 탐.

- [ ] **Step 5: 커밋**

```bash
git add docker/compose.yml docker/ui/docker-compose.yml docker/converter/docker-compose.yml
git commit -m "chore(compose): remove host-mode env and docker.sock from app/converter"
```

---

## Task 4: 프런트 ConverterControls — Convert 버튼을 `/api/jobs`로

**Files:**
- Modify: `frontend/src/components/ConverterControls.tsx`
- Modify: `frontend/src/api/converter.ts` (또는 동등 위치의 fetch 모듈)
- Create: `frontend/tests/converterButtonEnqueue.test.mjs`

- [ ] **Step 1: 실패 회귀 테스트**

`frontend/tests/converterButtonEnqueue.test.mjs`:

```javascript
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const dir = dirname(fileURLToPath(import.meta.url))
const ctrlSrc = readFileSync(
  join(dir, '../src/components/ConverterControls.tsx'), 'utf8',
)
const apiSrc = readFileSync(
  join(dir, '../src/api/converter.ts'), 'utf8',
)

function must(s, needle, label) {
  if (!s.includes(needle)) throw new Error(`[${label}] missing: ${needle}`)
}
function mustNot(s, needle, label) {
  if (s.includes(needle)) throw new Error(`[${label}] still present: ${needle}`)
}

must(apiSrc, "fetch('/api/jobs'", 'api enqueue')
must(apiSrc, "method: 'POST'", 'api enqueue method')
must(ctrlSrc, 'enqueueConvertJob', 'controls calls enqueueConvertJob')
mustNot(ctrlSrc, 'docker compose', 'no compose hint in UI')
mustNot(ctrlSrc, 'host_runtime', 'no NAS file references')

console.log('converterButtonEnqueue: Convert button posts to /api/jobs')
```

- [ ] **Step 2: 실패 확인**

```bash
node frontend/tests/converterButtonEnqueue.test.mjs
```
Expected: missing `fetch('/api/jobs'` 등으로 throw.

- [ ] **Step 3: API 모듈 + 컴포넌트 수정**

`frontend/src/api/converter.ts` 에 추가 (모듈이 없으면 신규 생성):

```typescript
export async function enqueueConvertJob(payload: Record<string, unknown>): Promise<{ id: number; status: string }> {
  const r = await fetch('/api/jobs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      type: 'convert',
      payload,
      dedupe_key: typeof payload.cell === 'string' ? payload.cell : undefined,
    }),
  })
  if (!r.ok) {
    const body = await r.json().catch(() => ({}))
    throw new Error(body.error ?? `enqueue failed: ${r.status}`)
  }
  return r.json()
}
```

`ConverterControls.tsx` 의 기존 "Convert" 버튼 핸들러를 다음으로 교체:

```typescript
import { enqueueConvertJob } from '../api/converter'

async function onConvertClick() {
  try {
    const job = await enqueueConvertJob({ cell: selectedCell })
    showToast(`대기열에 추가됨 #${job.id}`)
  } catch (err) {
    showToast((err as Error).message, { kind: 'error' })
  }
}
```

호출부 어디에서도 `host_runtime`, `docker compose`, `CURATION_CONVERTER_CONTROL_MODE` 가 등장하지 않도록 잔존 분기를 제거.

- [ ] **Step 4: 테스트 통과 + 빌드 확인**

```bash
node frontend/tests/converterButtonEnqueue.test.mjs
( cd frontend && npm run build )
```
Expected: 회귀 테스트 통과, vite 빌드 성공.

- [ ] **Step 5: 커밋**

```bash
git add frontend/src/api/converter.ts frontend/src/components/ConverterControls.tsx \
        frontend/tests/converterButtonEnqueue.test.mjs
git commit -m "feat(ui): Convert button enqueues via /api/jobs"
```

---

## Task 5: 프런트 워커 제어 표면 — Pause/Resume/Cancel pill

**Files:**
- Create: `frontend/src/components/WorkerControlPill.tsx`
- Modify: `frontend/src/components/ConverterControls.tsx` (pill mount + Stop 버튼 분리)
- Create: `frontend/tests/workerControlPill.test.mjs`

- [ ] **Step 1: 실패 테스트**

`frontend/tests/workerControlPill.test.mjs`:

```javascript
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const dir = dirname(fileURLToPath(import.meta.url))
const pillSrc = readFileSync(
  join(dir, '../src/components/WorkerControlPill.tsx'), 'utf8',
)
const ctrlSrc = readFileSync(
  join(dir, '../src/components/ConverterControls.tsx'), 'utf8',
)

function must(s, needle, label) {
  if (!s.includes(needle)) throw new Error(`[${label}] missing: ${needle}`)
}

must(pillSrc, "fetch(`/api/workers/", 'pill reads workers')
must(pillSrc, "PATCH", 'pill PATCHes desired_state')
must(pillSrc, "stale", 'pill renders stale badge')
must(ctrlSrc, 'WorkerControlPill', 'controls mounts pill')
must(ctrlSrc, '/api/jobs/', 'controls posts cancel via jobs endpoint')

console.log('workerControlPill: pause/resume + cancel split surface present')
```

- [ ] **Step 2: 실패 확인**

```bash
node frontend/tests/workerControlPill.test.mjs
```
Expected: missing `fetch(\`/api/workers/` 등으로 throw.

- [ ] **Step 3: 컴포넌트 작성 + 마운트**

`frontend/src/components/WorkerControlPill.tsx`:

```tsx
import { useEffect, useState } from 'react'

type WorkerState = 'running' | 'paused' | 'draining' | 'stopped'

type Worker = {
  worker_id: string
  desired_state: WorkerState
  actual_state: WorkerState | null
  last_beat_at: string | null
}

export function WorkerControlPill({ workerId }: { workerId: string }) {
  const [worker, setWorker] = useState<Worker | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let alive = true
    const tick = async () => {
      const r = await fetch(`/api/workers/${workerId}`)
      if (alive && r.ok) setWorker(await r.json())
    }
    tick()
    const id = setInterval(tick, 5000)
    return () => { alive = false; clearInterval(id) }
  }, [workerId])

  if (!worker) return null
  const stale = worker.last_beat_at == null ||
    Date.now() - new Date(worker.last_beat_at).getTime() > 30_000

  const setState = async (next: WorkerState) => {
    setBusy(true)
    try {
      const r = await fetch(`/api/workers/${workerId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ desired_state: next }),
      })
      if (r.ok) setWorker(await r.json())
    } finally { setBusy(false) }
  }

  return (
    <div className="worker-pill">
      <span style={{ fontFamily: 'var(--font-mono)' }}>{worker.actual_state ?? '–'}</span>
      {worker.desired_state !== worker.actual_state && (
        <span className="muted">→ {worker.desired_state}</span>
      )}
      {stale && <span className="badge-error">stale</span>}
      {worker.desired_state === 'running' && (
        <button disabled={busy} onClick={() => setState('paused')}>Pause</button>
      )}
      {worker.desired_state === 'paused' && (
        <button disabled={busy} onClick={() => setState('running')}>Resume</button>
      )}
    </div>
  )
}
```

`ConverterControls.tsx` 안에:
- 기존 "Stop" 버튼이 컨테이너를 stop하던 핸들러 제거.
- Pause/Resume 은 `WorkerControlPill` 이 담당.
- "현재 작업 취소" 동작은 별도 버튼: 현재 in-flight job id를 가져와 `POST /api/jobs/:id/cancel` 호출.

```tsx
import { WorkerControlPill } from './WorkerControlPill'
// ... render 영역에:
<WorkerControlPill workerId="converter" />
<button onClick={async () => {
  if (!currentJobId) return
  await fetch(`/api/jobs/${currentJobId}/cancel`, { method: 'POST' })
}}>현재 작업 취소</button>
```

(`currentJobId` 는 ConverterControls 의 기존 진행중 작업 상태에서 노출. 없으면 같은 컴포넌트의 기존 폴링 로직을 활용해 `worker.in_flight_job_id` 를 추출한다.)

- [ ] **Step 4: 테스트 + 빌드**

```bash
node frontend/tests/workerControlPill.test.mjs
( cd frontend && npm run build )
```

- [ ] **Step 5: 커밋**

```bash
git add frontend/src/components/WorkerControlPill.tsx \
        frontend/src/components/ConverterControls.tsx \
        frontend/tests/workerControlPill.test.mjs
git commit -m "feat(ui): pause/resume worker + cancel job split surface"
```

---

## Task 6: README + 운영 노트 업데이트

**Files:**
- Modify: `README.md`

- [ ] **Step 1: 변경 항목 확인**

```bash
grep -n "host" README.md | head -20
grep -n "CURATION_CONVERTER_CONTROL_MODE" README.md
```

- [ ] **Step 2: README 업데이트**

`README.md` 에서:
- "Host-managed converter" 섹션 또는 동등 표현을 **삭제**.
- 다음 절을 신설(또는 기존 "Operating the converter" 섹션을 대체):

```markdown
### Operating the converter / curation-worker

컨테이너는 compose 기동 시 1회 up되고 이후 24/7 상주합니다. 작업 라이프사이클은
컨테이너 라이프사이클과 분리됩니다.

- 변환 시작: UI의 "Convert" 또는 `curl -X POST /api/jobs -d '{"type":"convert","payload":{...}}'`
- 작업 취소: UI의 "현재 작업 취소" 또는 `POST /api/jobs/:id/cancel`
- 워커 일시정지: UI의 Pause 또는 `PATCH /api/workers/:id {"desired_state":"paused"}`
- 코드 배포로 컨테이너 재기동이 필요할 때: `./main.sh --restart-worker converter`
  (운영자 전용. 일상 흐름에서는 사용하지 않음.)

`CURATION_CONVERTER_CONTROL_MODE`, `host_runtime.json`, `stop.flag`, `task_request.json`
는 더 이상 사용되지 않습니다. 잔존 파일이 있으면 삭제해도 안전합니다.
```

- [ ] **Step 3: 게이트 + 검증**

```bash
./scripts/verify_no_host_control.sh && echo OK
grep -nE "host_runtime|CURATION_CONVERTER_CONTROL_MODE" README.md && exit 1 || echo "README clean"
```

- [ ] **Step 4: 커밋**

```bash
git add README.md
git commit -m "docs(readme): document API-only worker control flow"
```

---

## Task 7: End-to-end 스모크 — enqueue → run → cancel, in-process drive

**Files:**
- Create: `tests/test_end_to_end_api_only_control.py`

> **변경 노트 (2026-04-26)**: 원안은 외부에서 도는 converter 워커가 큐를 소비한다고
> 가정했지만, 이 sprint 의 parent-shim (Task 1) 은 `_run_conversion` 이 기본
> `NotImplementedError` 를 raise 함 — 진짜 worker 프로세스가 없다. 또 e2e 의 핵심
> 검증 ("컨테이너 재기동 없음") 은 verify_no_host_control.sh 게이트가 코드 단에서
> 이미 보장한다. 본 task 는 in-process 로 `process_one_queued` 를 직접 돌려
> HTTP → DB → runtime → handler → status 흐름을 검증한다. 실제 외부 워커 + 컨테이너
> 라이프사이클 확인은 후속 PR (submodule wiring + 워커 컨테이너 배포) 에서.

- [ ] **Step 1: 테스트 작성**

`tests/test_end_to_end_api_only_control.py`:

```python
"""End-to-end smoke for the API-only control plane (in-process drive).

Drives backend.converter.queue_adapter.process_one_queued from inside the
test so we can verify the full HTTP → DB → runtime → handler → status path
without needing a long-running worker container. Container-lifecycle
guarantees (no restart on convert) are enforced at code level by
scripts/verify_no_host_control.sh and proven structurally there.
"""
import asyncio
import pytest
from httpx import ASGITransport, AsyncClient
from backend.main import app
from backend.core.db import db, init_db
from backend.converter.queue_adapter import process_one_queued
from backend.workers.runtime import CancelledNormally

pytestmark = pytest.mark.usefixtures("_point_settings_at_test_db")


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


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
async def test_post_jobs_then_drive_runtime_marks_complete(monkeypatch):
    completed_payloads = []

    async def fake_convert(payload, *, check_cancel=None):
        completed_payloads.append(dict(payload))

    monkeypatch.setattr(
        "backend.converter.queue_adapter._run_conversion", fake_convert,
    )

    async with _client() as ac:
        resp = await ac.post(
            "/api/jobs",
            json={"type": "convert", "payload": {"cell": "smoke/test"}},
            headers={"X-User-Name": "smoke"},
        )
        assert resp.status_code == 201
        job_id = resp.json()["id"]

        await process_one_queued(idle_sleep=0)

        r = await ac.get(f"/api/jobs/{job_id}")

    assert r.json()["status"] == "complete"
    assert completed_payloads == [{"cell": "smoke/test"}]


@pytest.mark.asyncio
async def test_pause_blocks_new_jobs(monkeypatch):
    """When the converter worker is paused, runtime.tick must not pick up the queued job."""
    convert_calls = []

    async def fake_convert(payload, *, check_cancel=None):
        convert_calls.append(dict(payload))

    monkeypatch.setattr(
        "backend.converter.queue_adapter._run_conversion", fake_convert,
    )

    async with _client() as ac:
        # Pause via /api/workers requires a fresh heartbeat first.
        await db.execute(
            "INSERT INTO worker_heartbeats(worker_id, actual_state) VALUES('converter','running') "
            "ON CONFLICT(worker_id) DO UPDATE SET actual_state='running', last_beat_at=NOW()"
        )
        pr = await ac.patch(
            "/api/workers/converter",
            json={"desired_state": "paused"},
            headers={"X-User-Name": "smoke"},
        )
        assert pr.status_code == 200

        resp = await ac.post(
            "/api/jobs", json={"type": "convert", "payload": {}},
        )
        job_id = resp.json()["id"]

        # Drive a tick — should NOT pick up the queued job (worker paused).
        await process_one_queued(idle_sleep=0)

        r = await ac.get(f"/api/jobs/{job_id}")

    assert r.json()["status"] == "queued"
    assert convert_calls == []


@pytest.mark.asyncio
async def test_cancel_during_handler_marks_cancelled(monkeypatch):
    """A cancel observed mid-handler returns CancelledNormally and marks the job cancelled."""
    async def slow_with_cancel(payload, *, check_cancel):
        # Simulate the user canceling mid-conversion.
        await db.execute("UPDATE jobs SET status='cancel_requested' WHERE status='running'")
        if await check_cancel():
            return CancelledNormally(cleanup="partial output removed")

    monkeypatch.setattr(
        "backend.converter.queue_adapter._run_conversion", slow_with_cancel,
    )

    async with _client() as ac:
        resp = await ac.post(
            "/api/jobs", json={"type": "convert", "payload": {"cell": "smoke/cancel"}},
        )
        job_id = resp.json()["id"]

        await process_one_queued(idle_sleep=0)

        r = await ac.get(f"/api/jobs/{job_id}")

    assert r.json()["status"] == "cancelled"
    assert "partial output removed" in (r.json().get("error") or "")


@pytest.mark.asyncio
async def test_no_host_control_artifacts_left_in_codebase():
    """Structural guarantee that container lifecycle is decoupled from job lifecycle.

    The verify_no_host_control.sh gate is the source of truth — running it as
    part of the test pins the contract so a future regression in service.py /
    router.py would fail this test.
    """
    import subprocess
    from pathlib import Path
    repo_root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [str(repo_root / "scripts" / "verify_no_host_control.sh")],
        cwd=repo_root, capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        f"verify_no_host_control.sh failed (exit {proc.returncode}):\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
```

- [ ] **Step 2: 실행**

```bash
docker compose -f docker/compose.yml up -d db   # only db needed for in-process tests
pytest tests/test_end_to_end_api_only_control.py -v
```

Expected: 4 PASS (3 in-process flow tests + verify_no_host_control structural test).

- [ ] **Step 3: 게이트 + 회귀 일괄 실행**

```bash
./scripts/verify_no_host_control.sh
pytest -q
( cd frontend && npm run build && \
  for f in tests/*.mjs; do node "$f"; done )
```

Expected: gate 0, pytest green, 모든 mjs 회귀 통과.

- [ ] **Step 4: 커밋**

```bash
git add tests/test_end_to_end_api_only_control.py
git commit -m "test(e2e): API-only worker control covers full convert lifecycle"
```

---

## Self-Review Notes

- Spec coverage 대조 (보강안 design §10 검증 게이트):
  - "app 컨테이너 내부에 docker.sock 부재" → Task 3 Step 4 의 `grep "No such file"`
  - "CURATION_CONVERTER_CONTROL_MODE env 부재" → Task 3 Step 4 의 env grep
  - "convert-server 라이프사이클 변화 없음" → Task 7 의 `_converter_started_at()` 비교
  - "POST /api/jobs 후 status queued→running→complete" → Task 7 첫 케이스
  - "PATCH /api/workers paused 후 신규 픽업 안 됨" → Task 7 두 번째 케이스
  - "POST /api/jobs/:id/cancel 후 30초 내 cancelled + cleanup 마커" → Task 7 세 번째 케이스
  - "heartbeat 30s 끊긴 상태에서 PATCH 422" → Foundations plan Task 5 의 `test_patch_worker_rejects_stale`
  - "grep CURATION_CONVERTER_CONTROL_MODE 결과 없음" → Task 2 의 `verify_no_host_control.sh`
  - "grep host_runtime.json|stop.flag|task_request.json 결과 없음" → 동일

- 명시적 placeholder/TBD 없음. 모든 step은 실행 가능한 코드 또는 명령.
- 함수/타입 시그니처 일관성 (Foundations plan과 교차):
  - Task 1 의 `process_one_queued` → Foundations Task 6 의 `runtime.tick(worker_id="converter", supported_types=["convert"], handler=_handler, idle_sleep=…)`.
  - Task 2 의 `backend/converter/router.py` 가 호출하는 enqueue/cancel 은 Foundations Task 2 의 `repo.enqueue(type_=, payload=, dedupe_key=, requested_by=)` 와 `repo.request_cancel(job_id)`.
  - Task 4 의 `enqueueConvertJob({cell})` → 서버 측 dedupe 정책은 `dedupe_key=cell` 으로 단순화. (`payload.cell` 이 string일 때만 dedupe)
- 파일 경로는 모두 절대(레포 루트 기준) 표기. 신규 파일은 §File Structure 와 1:1 매칭.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-26-api-only-worker-control-integration.md`. Two execution options:

1. **Subagent-Driven (recommended)** — task마다 fresh subagent dispatch, task 사이마다 검토.
2. **Inline Execution** — 같은 세션에서 batch 실행, checkpoint마다 일시정지.

본 plan 실행 전에 Foundations plan(`2026-04-26-api-only-worker-control-foundations.md`) 의 Tasks 1~6 이 모두 머지되어야 한다.
