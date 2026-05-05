# Spec: Jobs 통일 (Phase 1)

> 비동기 장기 작업을 영속 큐(`/api/jobs`) 한 곳으로 모으고, `curation-worker`
> 컨테이너의 placeholder 를 실제 dispatcher 로 채우는 작업.

## 배경

curation-tools 의 비동기 장기 작업은 현재 **두 트랙으로 나뉘어 있습니다**.

- **Track A — 영속 큐**: `backend/jobs/repo.py` (Postgres `jobs` 테이블) +
  `backend/workers/runtime.py::tick()` + `backend/converter/queue_adapter.py`.
  현재는 `convert` 한 가지 Operation 만 실제로 흐릅니다.
- **Track B — 인메모리**: `backend/datasets/services/dataset_ops_service.py`
  의 `_jobs: dict[str, dict]` + `loop.run_in_executor(...)`. dataset-ops
  5종 (`split / merge / delete / sync_good_episodes / stamp_cycles`) 이 여기
  서 돕니다.

`backend/jobs/router.py:17` 의 `EnqueueBody.type` Literal 은 이미 6종을 모두
받게 돼 있고, `docker/curation-worker/placeholder.py` 에는

```
"""Spec-1 placeholder. Spec-3 replaces this with a real DB-queue consumer."""
```

라는 주석이 박혀 있습니다. 즉 이 작업의 정체는 **새 구조 도입이 아니라
"Spec-3 를 끝내는 일"** 입니다.

## 목표 / 비목표

**목표 (Phase 1)**

- 모든 비동기 장기 작업이 영속 큐 (`jobs` 테이블) 를 거치게 한다.
- `curation-worker` 컨테이너의 placeholder 를 실제 dispatcher 로 채운다.
- 사용자/UI 가 두 가지 실행 모델을 구분할 필요가 없게 한다.

**비목표 (Phase 2 로 분리)**

- dataset-ops 의 cancel 지원. 영속 큐의 `cancel_requested` 메커니즘은 그대로
  살아 있지만, 새 핸들러들은 우선 cancel 미관찰 (`check_cancel` 인자만 받고
  무시) 로 합류한다.
- UI 의 라우터 단일화. `/api/datasets/{split,merge,…}` 와
  `/api/datasets/ops/status/{id}` 는 façade 로 유지하고, UI 는 스키마 변경
  없이 동작한다.

## 결정 요약

| # | 결정 |
|---|---|
| Q1 | `/api/jobs` 로 통일 (현재 인메모리 경로는 절반 마이그레이션) |
| Q2 | `curation-worker` 한 컨테이너가 dataset-ops 5종을 모두 담당 |
| Q3 | cancel 고도화는 Phase 2 |
| Q4 | `external_id text` 컬럼 추가, UI/외부 식별자는 UUID |
| Q5 | 라우터는 façade 유지, UI 호환 |
| Q6 | `tick()` 시그니처를 `Mapping[str, JobHandler]` 로 deepening (b2). converter 는 단일 항목 mapping 으로 wrapper 만 갱신 |
| Q7 | `external_id` (서버 생성 UUID) 와 `dedupe_key` (도메인 중복 방지) 분리 |
| Q8 | backup/restore 는 `backend/jobs/runner_helpers.py` 로 승격, vendored engine 안 건드림 |
| Q9 | façade 가 `result jsonb` 를 평탄화해서 UI 호환 응답 반환 |
| Q10 | 레이어별 점진 — Infra → Pilot → Bulk 3개 PR |

## 새 도메인 어휘

`CONTEXT.md` 에 등록 (이 PR 흐름과 함께 도입):

- **Job** — 영속 비동기 작업 레코드
- **Operation** — Job 의 도메인 행위 (`convert`, `split`, …)
- **JobHandler** — 한 Operation 을 처리하는 async 함수
- **Worker** — `Mapping[str, JobHandler]` 를 가지고 `tick()` 루프를 도는 프로세스

## 단계별 PR 계획

### PR-1: Infra (시그니처 + 컬럼 + 헬퍼 + 컨테이너 골격)

**변경 파일**

- `backend/workers/runtime.py` — `tick()` / `run_forever()` 시그니처를
  `handlers: Mapping[str, JobHandler]` 로 deepening. `_claim` 의 SQL 은
  `type = ANY($supported)` 인자를 `list(handlers.keys())` 로부터 받음.
- `backend/converter/queue_adapter.py` — `tick(...)` 호출부를 단일 항목
  mapping `{"convert": _handler}` 로 바꿈. 그 외 동작 변경 없음.
- `backend/jobs/repo.py` — `enqueue()` 가 `external_id` (uuid4) 를 생성·반환.
  `fetch_by_external_id()` 추가. 응답 dict 에 `external_id` 포함.
- `docker/db/init.sql` (또는 새 마이그레이션 파일) — `jobs.external_id text`
  컬럼 + `UNIQUE INDEX ON jobs(external_id)`.
- `backend/jobs/runner_helpers.py` (신규) — `run_in_place_with_rollback()`.
  현 `DatasetOpsService._run_with_backup` 을 그대로 옮김.
- `docker/curation-worker/Dockerfile` — `placeholder.py` 대신 새 entry point
  실행.
- `backend/workers/curation_worker.py` (신규) — `HANDLERS: dict[str,
  JobHandler] = {}` 로 빈 mapping. `run_forever(handlers=HANDLERS)` 만 호출.
  PR-2 에서 항목이 채워진다.

**테스트**

- `tests/test_workers_runtime.py` — `tick()` 의 새 시그니처에 대한 mapping
  dispatch 단위 테스트 (두 타입 등록 + 각각 다른 핸들러 호출 검증).
- `tests/test_jobs_repo.py` — `external_id` 컬럼 채워지는지, 유일성, fetch
  검증.
- `tests/test_runner_helpers.py` (신규) — 성공/실패 시 backup 동작.
- 기존 converter 통합 테스트 회귀 없음.

**롤백 전략**: 새 컬럼/헬퍼는 추가만, 기존 컨버터 동작은 wrapper 한 줄 변경.
배포 후 문제 발견 시 PR 만 revert 하면 데이터 손상 없음.

### PR-2: Pilot — `stamp_cycles` 한 타입을 영속 큐로 이주

`stamp_cycles` 가 가장 좋은 파일럿:

- in-place backup/restore 패턴 (PR-1 의 `run_in_place_with_rollback` 검증)
- payload 가 단순 (`source_path`, `overwrite`)
- 결과가 단순 (`result_path` 만)
- 도메인 위험이 작음 (cycle stamp 가 잘못돼도 backup 으로 복구)

**변경 파일**

- `backend/datasets/services/cycle_stamp_handler.py` (신규) — JobHandler
  구현. `cycle_stamp_service.stamp_dataset_cycles` 호출 + `runner_helpers`
  사용.
- `backend/workers/curation_worker.py` — `HANDLERS["stamp_cycles"] = ...`
  등록.
- `backend/datasets/routers/dataset_ops.py` — `stamp_cycles` 엔드포인트가
  `repo.enqueue(type_="stamp_cycles", payload={...},
  dedupe_key=f"stamp_cycles:{source}")` 로 흐르고, `external_id` 를 응답.
  `get_job_status` 가 `external_id` 로 조회 가능하도록 분기 추가 (이 시점엔
  legacy in-memory 와 신규 둘 다 지원 — PR-3 에서 legacy 제거).
- `backend/datasets/services/dataset_ops_service.py` — `stamp_cycles` 관련
  메서드/`_run_stamp_cycles` 만 제거 (다른 4개는 그대로 in-memory).

**테스트**

- 핸들러 단위 테스트 (DB 없이 mock job dict).
- 통합 테스트: `tick()` + 임시 DB + 실제 cycle stamp 1회.
- 라우터 테스트 갱신: `repo.enqueue` mock 검증으로 변경.
- mockup data 시나리오 1회 (CLAUDE.md 의 테스트 순서 규칙 적용).

**롤백 전략**: `stamp_cycles` 한 타입만 이중 경로. 문제 시 라우터 한 줄
revert 로 in-memory 경로로 복귀.

### PR-3: Bulk — 나머지 4종 이주 + 정리

**변경 파일**

- `backend/datasets/services/{split,merge,delete,sync_good_episodes}_handler.py`
  4개 신규. 각 핸들러는 `dataset_ops_engine` 호출 + 필요 시 `runner_helpers`.
- `backend/workers/curation_worker.py` — 4개 등록.
- `backend/datasets/routers/dataset_ops.py` — 4개 엔드포인트 façade 전환.
  `JobStatusResponse` 평탄화 로직 일원화 (`result jsonb` → `result_path`,
  `summary`).
- `backend/datasets/services/dataset_ops_service.py` — 파일 삭제. 모듈 레벨
  싱글턴 `dataset_ops_service` import 도 제거.
- `tests/test_dataset_ops_router.py` — `repo.enqueue` mock 으로 전환,
  in-memory `_jobs` 검사 제거.

**테스트**

- 4개 핸들러 단위 테스트.
- 라우터 façade 테스트 (응답 모델이 기존과 동일한지 회귀 검증).
- 통합: 4종 각각 mockup data 로 1회.

**롤백 전략**: PR-3 가 가장 큰 변경 단위. 이슈 발견 시 PR-3 만 revert →
PR-2 의 단일 타입 영속화 + 4종 in-memory 상태로 복귀 (Phase 1 의 일시 단계
와 동일한 모양).

## 결과 jsonb 의 키 규약 (Q9 의 a)

각 핸들러는 `result jsonb` 에 평탄 키로 dump:

| Operation | jsonb keys |
|---|---|
| `convert` | (현재처럼 비워두거나, 최소한 `cells_processed: int` 정도) |
| `split` | `{"result_path": "..."}` |
| `merge` | `{"result_path": "..."}` |
| `delete` | `{"result_path": "..."}` |
| `stamp_cycles` | `{"result_path": "..."}` |
| `sync_good_episodes` | `{"result_path": "...", "summary": {"mode": "...", "created": int, "skipped_duplicates": int}}` |

façade (`get_job_status`) 가 jsonb 를 풀어 `JobStatusResponse` 모델로 응답.
응답 schema 는 변경하지 않는다.

## Phase 2 로 미루는 것

- dataset-ops 의 cancel 지원 (engine 레벨 협조 필요).
- UI 의 `/api/datasets/*` 엔드포인트 제거 + `/api/jobs/*` 단일 표면.
- `curation-worker` 의 replicas / 수평 확장 검증.

## 검증 순서 (CLAUDE.md 규칙 준수)

각 PR 마다:

1. `pytest` 단위 테스트
2. Docker 안에서 mockup data 시나리오
3. 실제 data 로 1회 (특히 PR-2/PR-3 의 in-place 작업은 실제 데이터로 backup
   복구 경로까지 한 번 돌려본다)
