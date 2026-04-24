# Converter DB Queue 전환 — Spec-2

날짜: 2026-04-25
상태: Approved for planning
선행 조건: `2026-04-22-docker-5-service-split-spec1-design.md` 완료

---

## 1. 목적

현재 production-style 구조에서 backend와 converter는 직접 통신하지 않고 NAS 공유 파일로 제어한다.

- backend가 `convert_requests.json`에 요청을 쓴다.
- converter가 파일을 polling해서 작업을 시작한다.
- 진행 이벤트는 `convert_events.jsonl`, runtime heartbeat는 `convert_runtime.json`에 남긴다.

이 구조는 단일 host-managed worker에는 충분하지만, 요청 보장/중복 방지/취소/감사/다중 worker 확장에는 약하다. Spec-2는 converter 요청과 job lifecycle의 source of truth를 Postgres `jobs` 테이블로 옮긴다.

## 2. 범위

포함:

- `/api/converter/start`가 `jobs(type='convert')` row를 생성한다.
- `/api/converter/stop`이 running convert job에 cancel request를 기록한다.
- converter worker가 Postgres에서 convert job을 claim/polling한다.
- converter job 상태를 `queued/running/complete/failed/cancel_requested/cancelled`로 관리한다.
- 기존 progress scan과 live event stream은 초기 전환에서 유지한다.
- `convert_requests.json`은 compatibility fallback으로만 남긴 뒤 제거한다.

제외:

- 모든 progress/event를 DB로 완전 이관하는 작업.
- split/merge/delete의 curation-worker 이관.
- worker autoscaling.
- 외부 message broker 도입.

## 3. 결정 사항

| 결정 | 값 |
|---|---|
| Queue backend | Postgres `jobs` 테이블 |
| Worker 접근 방식 | converter가 Postgres에 직접 연결해 polling/claim |
| Backend 역할 | job enqueue, status 조회, cancel request |
| 중복 방지 | `dedupe_key = cell_task` partial unique index |
| Claim 방식 | `FOR UPDATE SKIP LOCKED` |
| Progress source | 1차 전환에서는 기존 `convert_state.json` + NAS scan 유지 |
| Live activity source | 1차 전환에서는 기존 `convert_events.jsonl` WebSocket 유지 |
| File IPC 제거 순서 | `convert_requests.json` 먼저 제거, progress/event 파일은 후속 단계 |
| Fallback | `CONVERTER_QUEUE_BACKEND=files`에서 기존 파일 queue 사용 가능 |

## 4. 데이터 모델

Spec-1에서 생성한 `jobs` 테이블을 사용한다.

Convert job payload:

```json
{
  "cell_task": "cell001/task_a",
  "requested_by": "ui",
  "requested_from": "converter_page"
}
```

Convert job progress:

```json
{
  "phase": "scanning|converting|finalizing|idle",
  "active_cell_task": "cell001/task_a",
  "current_recording": "20260424_120000",
  "recording_index": 3,
  "recording_total": 12
}
```

Convert job result:

```json
{
  "cell_task": "cell001/task_a",
  "converted": 12,
  "failed": 0,
  "output_path": "/mnt/synology/data/data_div/2026_1/lerobot/cell001/task_a"
}
```

Status rules:

- `queued`: backend inserted the job and no worker has claimed it.
- `running`: worker claimed the job and heartbeat is fresh.
- `cancel_requested`: backend requested cancellation; worker should stop at a safe boundary.
- `cancelled`: worker acknowledged cancellation and stopped before normal completion.
- `complete`: worker finished the requested task successfully.
- `failed`: worker hit a fatal error or stale heartbeat recovery marked it failed.

## 5. Backend API Changes

### 5.1 `POST /api/converter/start`

Input remains compatible:

```json
{"cell_task": "cell001/task_a"}
```

Behavior:

1. Validate `cell_task`.
2. Insert into `jobs` with:
   - `type='convert'`
   - `status='queued'`
   - `dedupe_key=cell_task`
   - `payload.cell_task=cell_task`
3. If an active row already exists for the same `cell_task`, return that existing job as `already_queued`.
4. Return `202`.

Response:

```json
{
  "status": "queued",
  "job_id": 123,
  "cell_task": "cell001/task_a",
  "message": "queued"
}
```

### 5.2 `POST /api/converter/stop`

Behavior:

1. Find the newest active `convert` job.
2. If `queued`, mark it `cancelled`.
3. If `running`, set `status='cancel_requested'` and `cancel_requested_at=NOW()`.
4. If no active job exists, return 409 or 503 with the existing user-facing message shape.

The worker remains responsible for safe shutdown. It checks cancellation between recordings and during finalization boundaries already used by the current stop flag flow.

### 5.3 `GET /api/converter/status`

Status response keeps the current frontend contract, with these additions:

- `active_job_id`
- `active_cell_task` from the active DB job when present
- `task_start_available` from converter runtime heartbeat

Progress counts still come from `build_progress()` in the first DB-queue release. This keeps the UI stable while request ownership moves to DB.

### 5.4 `GET /api/converter/jobs/{job_id}`

Add a narrow status endpoint for job detail:

```json
{
  "job_id": 123,
  "operation": "convert",
  "status": "running",
  "created_at": "...",
  "started_at": "...",
  "completed_at": null,
  "error": null,
  "progress": {}
}
```

This endpoint mirrors `/api/datasets/ops/status/{job_id}` so frontend job polling can share patterns later.

## 6. Worker Protocol

### 6.1 Claim Loop

Converter runs with:

- `CURATION_DB_URL=postgresql://...`
- `CONVERTER_QUEUE_BACKEND=postgres`
- `CONVERTER_WORKER_ID=${HOSTNAME}` by default

Claim query shape:

```sql
WITH next_job AS (
    SELECT id
    FROM jobs
    WHERE type = 'convert' AND status = 'queued'
    ORDER BY created_at
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE jobs
SET status = 'running',
    worker_id = $1,
    attempts = attempts + 1,
    started_at = COALESCE(started_at, NOW()),
    heartbeat_at = NOW(),
    updated_at = NOW()
WHERE id = (SELECT id FROM next_job)
RETURNING id, payload;
```

If no job is available, the worker sleeps for `SCAN_INTERVAL` or a shorter DB queue poll interval such as 5 seconds.

### 6.2 Execution

For each claimed job:

1. Extract `payload.cell_task`.
2. Reuse existing single-task filtering logic from `ONLY_CELL_TASK`.
3. Convert pending recordings for that task only.
4. Update `jobs.progress` at task start, recording start, finalizing, and completion.
5. Continue writing `convert_state.json` and `convert_events.jsonl` for existing UI progress.

The converter no longer scans all unrequested tasks in DB queue mode.

### 6.3 Heartbeat

While a job is running, worker writes:

```sql
UPDATE jobs
SET heartbeat_at = NOW(), updated_at = NOW()
WHERE id = $1 AND worker_id = $2 AND status IN ('running', 'cancel_requested');
```

Heartbeat interval: 30 seconds.

Stale recovery:

- A running job with `heartbeat_at < NOW() - INTERVAL '5 minutes'` is stale.
- Recovery marks it `failed` with `error='worker heartbeat stale'`.
- Automatic retry is not enabled in the first release; retry is a new enqueue request.

### 6.4 Cancellation

Worker checks job status:

- before starting a task
- before each recording
- after each recording
- before finalization

If status is `cancel_requested`, worker stops at the next safe boundary and marks:

```sql
UPDATE jobs
SET status = 'cancelled',
    finished_at = NOW(),
    updated_at = NOW()
WHERE id = $1 AND worker_id = $2;
```

The old `convert_stop.flag` remains only for file fallback mode.

## 7. Migration Plan

### Phase 1: DB Queue Library

- Add `backend/converter/job_repository.py` for enqueue/status/cancel.
- Add converter-side queue client under `rosbag2lerobot-svt/nas/job_queue.py`.
- Keep existing file queue untouched.

### Phase 2: Backend Writes DB Jobs

- In Postgres deployment mode, `/api/converter/start` inserts DB jobs.
- In file fallback mode, keep `enqueue_task_start_request()`.
- Tests cover duplicate `cell_task` behavior and cancel status transitions.

### Phase 3: Converter Consumes DB Jobs

- Add `CONVERTER_QUEUE_BACKEND=postgres` path to `auto_converter.py`.
- Reuse existing conversion functions; avoid rewriting data conversion internals.
- Keep progress/event files for UI compatibility.

### Phase 4: Remove Request File Dependency

- Stop writing `convert_requests.json` in normal Postgres deployment.
- Keep read/write helpers only behind explicit fallback mode.
- Update README/main.sh runbook.

## 8. Error Handling

- Invalid `cell_task`: backend returns 422/409 using current error style.
- Duplicate active job: backend returns 202 with existing `job_id` and `message='already queued'`.
- DB unavailable in backend: `/api/converter/start` returns 503.
- DB unavailable in worker: worker logs error and retries connection; it does not start untracked conversion work.
- Worker crash: heartbeat becomes stale and recovery marks job failed.
- Conversion failure: worker marks job failed and writes the error string to `jobs.error`.
- Partial conversion before failure: existing `convert_state.json` remains authoritative for recording-level retry eligibility until progress is moved fully into DB.

## 9. Testing

Backend tests:

- enqueue creates `convert` job with `queued` status.
- duplicate active `cell_task` returns existing job.
- stop cancels queued job.
- stop marks running job `cancel_requested`.
- status includes active DB job fields.

Worker tests:

- claim query skips locked/running jobs.
- worker converts only requested `cell_task`.
- heartbeat updates while a fake long conversion runs.
- stale heartbeat recovery marks failed.
- cancellation between recordings marks cancelled.

Integration smoke:

- `./main.sh --up-convert`
- UI Converter page shows task as queued after Convert click.
- worker picks job and status moves to running.
- converted task reaches complete.
- no `convert_requests.json` write occurs in Postgres queue mode.

## 10. Rollback

- Set `CONVERTER_QUEUE_BACKEND=files`.
- Re-enable backend file queue path.
- Existing `jobs` rows can remain in Postgres for audit; they are ignored in file mode.
- If a DB-queued job was partially processed before rollback, recording-level retry behavior still follows `convert_state.json`.

## 11. Follow-up

After DB queue mode is stable:

1. Add `job_events` table and move live activity out of `convert_events.jsonl`.
2. Move converter runtime heartbeat out of `convert_runtime.json`.
3. Remove file queue fallback.
4. Reuse the same `jobs` table for curation-worker operations in Spec-3.
