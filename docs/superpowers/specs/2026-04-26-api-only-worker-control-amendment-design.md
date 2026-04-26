# API-only Worker Control — Spec-1 횡단 보강안

- 날짜: 2026-04-26
- 상태: 보강안 (Spec-1 / Spec-2 / Spec-3 횡단)
- 베이스 spec: [`2026-04-22-docker-5-service-split-spec1-design.md`](./2026-04-22-docker-5-service-split-spec1-design.md)
- 관련: [`2026-04-25-converter-db-queue-design.md`](./2026-04-25-converter-db-queue-design.md) (Spec-2), [`2026-04-17-multi-user-curation-design.md`](./2026-04-17-multi-user-curation-design.md)

---

## 1. 배경 / 이유

현재 backend의 converter 제어는 세 모드를 지원한다:

- `docker` — `app` 컨테이너가 host docker daemon을 호출해 `convert-server` 컨테이너를 stop/start
- `host` — `app`이 NAS에 heartbeat/request 파일을 떨어뜨리고, host에서 도는 변환 워커가 이를 폴링
- `auto` — 위 둘 자동 폴백

운영 시점에서 모두 **컨테이너 라이프사이클(또는 파일 시스템 신호)** 을 작업 라이프사이클의 대용으로 쓰고 있다. 이 방식은 multi-PC + 컨테이너 분리 환경에서 다음 문제가 누적된다:

1. `app`이 docker socket(또는 NAS의 임시 파일)을 통해 호스트 자원을 직접 쥔다 → 보안·권한 분리 어려움
2. **컨테이너 stop = in-flight 작업 강제 종료**. 부분 산출물 정리 시점이 모호 (메모리에 있는 *audit_incomplete* 패턴과 어긋남)
3. 여러 PC가 동시에 stop/start를 누르면 docker daemon 호출 race + 파일 큐 race가 함께 발생
4. docker daemon hang 또는 NFS 지연이 UI 응답 시간으로 그대로 노출
5. Spec-2가 converter를 `jobs` 테이블 소비자로 전환하는 순간 **컨테이너 라이프사이클 제어는 의미가 사라진다** — Spec-1에서 미리 정리해두지 않으면 짧은 기간 동안 두 제어 모델이 공존한다

본 보강안은 **Spec-1 시점부터 docker daemon 호출과 NAS 파일 시그널을 모두 제거**하고, 모든 제어를 **상주 컨테이너 + REST API + Postgres 제어 플레인**으로 통일한다.

## 2. 결정 요약

| 항목 | Before | After |
|---|---|---|
| 컨테이너 라이프사이클 | UI가 docker stop/start로 토글 | compose 기동 시 1회 up, 이후 24/7 상주 |
| 작업 시작 | `docker start convert-server` 또는 NAS 요청 파일 | `INSERT INTO jobs (type='convert', payload=...)` |
| 작업 중지 | `docker stop convert-server` (in-flight 강제 종료) | `UPDATE jobs SET status='cancel_requested'` (워커가 안전 지점에서 정리) |
| 워커 일시정지 | (사실상 없음 — stop으로 대체) | `worker_controls.desired_state = 'paused'` (in-flight는 진행, 신규 픽업만 차단) |
| 워커 상태 노출 | `docker ps` + container logs + heartbeat 파일 | `GET /api/workers` (DB 조회 단일 출처) |
| host docker socket | `app`에 마운트 가능 | **어떤 application 컨테이너에도 마운트 안 함** |
| NAS 시그널 파일 | `host_runtime.json`, `stop.flag`, `task_request.json` | **모두 제거**. 동일 시맨틱을 DB로 흡수 |
| `CURATION_CONVERTER_CONTROL_MODE` env | `auto/docker/host` | **제거**. 단일 모드(=API/DB)만 존재 |

## 3. 적용 범위 (어느 spec이 무엇을 흡수하는가)

| Spec | 본 보강안의 흡수 항목 |
|---|---|
| **Spec-1** | `worker_controls`/`worker_heartbeats` 테이블을 `init.sql`에 포함. `app` 컨테이너의 docker socket·host 시그널 코드 제거. `/api/jobs`·`/api/workers` 라우터 신설 (큐 소비는 Spec-2 이후). |
| **Spec-2** | converter를 jobs 큐 소비자로 전환할 때 본 보강안의 `desired_state` 시맨틱과 `cancel_requested` 계약을 그대로 따름. |
| **Spec-3** | curation-worker(split/merge/delete)도 동일 시맨틱 채택. |

본 보강안은 Spec-1 PR **안에** 포함된다 (별도 후속 PR 아님). Spec-1 §8 구현 순서의 §8.1(인프라 골격), §8.2(백엔드 DB 레이어), §8.5(`main.sh`)에 각각 변경 사항을 흡수한다 — 본 문서 §9·§10 에서 그 위치를 명시.

## 4. DB 변경 — Spec-1 §5 보강

Spec-1 §5.2 `jobs` 테이블은 그대로 둔다 (이미 `cancel_requested` / `cancelled` 상태와 `heartbeat_at` 컬럼을 갖고 있음).

다음 두 테이블을 `docker/db/init.sql`에 **신규 추가**한다.

```sql
CREATE TYPE worker_state AS ENUM (
    'running',     -- 큐를 정상 소비 중
    'paused',      -- 신규 픽업 안 함, in-flight는 계속 진행
    'draining',    -- 신규 픽업 안 함 + in-flight 종료 후 idle
    'stopped'      -- 모든 작업 정지 신호 (idle 상태 유지)
);

CREATE TABLE worker_controls (
    worker_id     TEXT PRIMARY KEY,           -- 'converter', 'curation-worker'
    desired_state worker_state NOT NULL DEFAULT 'running',
    updated_by    TEXT,                        -- X-User-Name
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    note          TEXT
);

CREATE TABLE worker_heartbeats (
    worker_id        TEXT PRIMARY KEY,
    actual_state     worker_state NOT NULL,
    pid              INTEGER,
    container_id     TEXT,                     -- 진단용. 제어 신호로는 쓰지 않음
    last_beat_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    in_flight_job_id BIGINT REFERENCES jobs(id),
    detail           JSONB
);
CREATE INDEX idx_worker_heartbeats_recent ON worker_heartbeats(last_beat_at);
```

`init.sql`은 기본 행도 시드한다:
```sql
INSERT INTO worker_controls (worker_id, desired_state)
    VALUES ('converter', 'running'), ('curation-worker', 'running')
    ON CONFLICT DO NOTHING;
```

## 5. 워커 메인 루프 계약

상주 워커(컨테이너 1개당 프로세스 1개)는 다음 루프를 따른다.

```
while True:
    upsert worker_heartbeats (actual_state, last_beat_at = NOW(), in_flight_job_id)

    desired = SELECT desired_state FROM worker_controls WHERE worker_id = $self
    if desired in ('paused', 'draining', 'stopped'):
        sleep(short)            # 새 작업 픽업 안 함
        continue                # in-flight가 있다면 그것만 finish 처리

    job = SELECT ... FROM jobs WHERE status='queued' AND type IN $supported
          ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1
    if job is None:
        sleep(short); continue

    UPDATE jobs SET status='running', worker_id=$self, started_at=NOW()
    try:
        run job in cooperative chunks
            after each chunk:
                UPDATE jobs SET heartbeat_at=NOW()
                if jobs.status == 'cancel_requested':
                    cleanup partial output (or mark partial-fail)
                    UPDATE jobs SET status='cancelled', finished_at=NOW()
                    break
        else:
            UPDATE jobs SET status='complete', finished_at=NOW(), result=...
    except:
        UPDATE jobs SET status='failed', finished_at=NOW(), error=...
```

핵심 계약 4가지:

1. 워커는 **자기 컨테이너 라이프사이클을 작업 종료 신호로 사용하지 않는다**. SIGTERM은 운영자 수동 개입 시에만 발생.
2. cancel은 **안전 지점에서만 확인**한다 (예: ROS bag 파일 1개 처리 단위 사이, ffmpeg 인코딩 단계 사이). 프로세스 강제 kill 아님.
3. cancel 확인 후 **부분 산출물은 명시적으로 정리하거나 partial-fail 마커로 남긴다**. silent하게 두지 않음 (메모리: *audit_incomplete*).
4. heartbeat는 **fresh 보장**. `last_beat_at < NOW() - 30s`이면 UI는 워커를 `stale`로 표시하고 `desired_state` 전환을 거부한다.

## 6. API 표면

`backend/converter/router.py`(host docker 호출)와 `backend/datasets/routers/*`(직접 변환 트리거)의 호출 경로는 다음으로 대체된다.

```
POST   /api/jobs
       body: { type, payload, dedupe_key? }
       headers: X-User-Name
       201: { id, type, status: 'queued', created_at }
       409: { error: 'duplicate_dedupe_key', existing_job_id }

GET    /api/jobs/:id
GET    /api/jobs?type=&status=&dataset_id=&limit=&since=
       (SSE 채널과 분리. 폴링 보조용)

POST   /api/jobs/:id/cancel
       headers: X-User-Name
       202: { id, status: 'cancel_requested' }
       409: { error: 'already_terminal', current_status }

GET    /api/workers                       # 모든 워커 (control + heartbeat join)
GET    /api/workers/:id
PATCH  /api/workers/:id
       body: { desired_state, note? }
       headers: X-User-Name
       200: { id, desired_state, actual_state, last_beat_at }
       422: { error: 'worker_stale', last_beat_at }   # heartbeat 30s 초과 시 거부
       422: { error: 'illegal_transition', from, to } # ex: stopped → running 직접 전환 금지
```

상태 전이 규칙:

```
running   ⇄ paused          (즉시)
running   → draining        (즉시; in-flight 종료 후 자연히 idle)
draining  → running         (in-flight 없음일 때만)
paused    → draining        (즉시)
*         → stopped         (관리자 명시 액션, note 필수)
stopped   → running         (관리자 명시 액션, note 필수)
```

기존 UI 액션 매핑:

| UI 액션 | Before | After |
|---|---|---|
| Convert (변환 시작) | `docker start convert-server` 또는 host request 파일 작성 | `POST /api/jobs {type:'convert', payload:{cell_path}, dedupe_key:'cell/task'}` |
| Stop (단일 작업) | `docker stop convert-server` | `POST /api/jobs/:id/cancel` |
| Pause queue (신규 노출) | (없었음) | `PATCH /api/workers/converter {desired_state:'paused'}` |
| Resume queue | docker start | `PATCH /api/workers/:id {desired_state:'running'}` |
| Status | docker ps + heartbeat 파일 read | `GET /api/workers` + `GET /api/jobs?status=running` |

## 7. SSE 통합

본 보강은 [`2026-04-17-multi-user-curation-design.md`](./2026-04-17-multi-user-curation-design.md)의 SSE 채널을 재사용한다. 신규 이벤트 종류:

```
job_updated     { id, type, status, dataset_id?, by? }
worker_updated  { worker_id, desired_state, actual_state, last_beat_at }
```

워커가 `jobs.status` 또는 `worker_heartbeats`를 갱신할 때마다 `NOTIFY job_channel` / `NOTIFY worker_channel` 을 보낸다. `app` 컨테이너의 SSE 허브가 LISTEN 후 모든 연결된 브라우저에 fan-out.

## 8. UI 영향 (frontend)

`frontend/src/components/Converter*.tsx` 와 host 모드 힌트 텍스트(`d36b4f2 Delegate dataset edits to LeRobot tools` 이후 변경된 영역)는 다음과 같이 정돈된다.

- **Convert** 버튼: 즉시 disabled 되지 않음. 누르면 즉시 toast "대기열에 추가됨 (#123)" + 큐 위치 표시. 후속 SSE로 status 추적.
- **Stop** 버튼: "현재 작업 취소" 와 "큐 일시정지" 두 동작으로 시각적 분리 (드롭다운 또는 별도 버튼).
- **Status pill**: `desired_state` ↔ `actual_state` 가 다르면 작은 갈매기(예: `paused → running 적용 중`) 표시. heartbeat가 30초 이상 묵으면 빨간 `stale` 배지.
- **Host-mode 힌트** (`24f43af feat(converter-ui): host-mode hint renders on a single surface`)는 제거. 단일 제어 모델만 존재하므로 모드 안내가 불필요.

## 9. 코드 정리 (Spec-1 §6 보강)

다음 파일/식별자는 Spec-1 구현 PR 안에서 제거한다.

```
backend/converter/service.py
  - get_converter_control_mode() / is_host_control_mode() / is_auto_control_mode()
  - read_host_control_info() / request_host_stop() / request_host_task() (이름은 다를 수 있음)
  - CONTROL_MODE_* 상수
  - host_runtime.json / stop.flag / task_request.json 파일 IO 전부

backend/converter/router.py
  - host docker 호출 경로 (있다면)
  - control_mode 분기 (단일 경로로 합침)

docker/ui/docker-compose.yml (Spec-1 §8.6에서 어차피 제거 예정)
  - CURATION_CONVERTER_CONTROL_MODE: host
  - docker.sock 마운트 (있는 경우)

docker/compose.yml (신규)
  - app 서비스에 docker.sock 마운트하지 **않음** (negative requirement, 리뷰 체크리스트로 보장)
```

새로 추가할 모듈:

```
backend/workers/control.py        # PATCH /api/workers, GET /api/workers
backend/jobs/router.py            # POST/GET/CANCEL /api/jobs
backend/jobs/service.py           # enqueue, cancel, list, fetch
backend/core/queue.py             # SELECT FOR UPDATE SKIP LOCKED 헬퍼 (Spec-1 §5.4 db 래퍼와 결합)
```

`backend/converter/`와 `backend/datasets/routers/*`에서 host 제어 호출을 제거하고, 변환 트리거가 필요한 자리에서는 `jobs.service.enqueue()`를 호출한다.

## 10. Spec-1 §9 검증 게이트 추가

다음 항목을 Spec-1 검증 체크리스트에 합친다.

- [ ] `app` 컨테이너 내부에서 `ls /var/run/docker.sock` 결과 없음 (마운트 안 됨)
- [ ] `docker exec app env | grep CURATION_CONVERTER_CONTROL_MODE` 결과 없음
- [ ] compose 기동 후 `docker inspect convert-server | jq '.[0].State.StartedAt'` 가 한 사이클(예: 1시간) 동안 변하지 않음 — 즉 라이프사이클 변화 없음
- [ ] `POST /api/jobs {type:'convert', ...}` 후 `status` 가 `queued → running → complete` 로 이동 (워커·컨테이너 재기동 없이)
- [ ] `PATCH /api/workers/converter {desired_state:'paused'}` 후 새 jobs가 picked up 되지 않음. in-flight job은 계속 진행됨
- [ ] `POST /api/jobs/:id/cancel` 후 30초 내(작업 chunk 1개 길이 기준) `cancelled` 도달 + 부분 산출물이 정리되거나 partial-fail 마커가 남음
- [ ] worker heartbeat가 30초 끊긴 상태에서 `PATCH /api/workers/:id` 가 422 (`worker_stale`)로 거부됨
- [ ] `grep -r CURATION_CONVERTER_CONTROL_MODE backend/ docker/` 결과 없음
- [ ] `grep -r host_runtime.json\\|stop.flag\\|task_request.json backend/` 결과 없음

## 11. 위험 / 트레이드오프

| 위험 | 완화 |
|---|---|
| 워커가 정말 멈춰있는데 heartbeat만 살아있는 좀비 | `actual_state` 와 `last_beat_at` 둘 다 워커가 갱신. 워커 메인 루프가 진짜 멈추면 둘 다 stale → UI가 30s TTL로 감지. fail-safe로 사람 개입 |
| `cancel_requested`인데 안전 지점이 멀어 취소가 오래 걸림 | UI에 "취소 진행 중 (n초 경과)" 표시. 일정 시간 초과 시 운영자가 컨테이너 재기동(예외 경로). 일상 흐름엔 노출 안 함 |
| 컨테이너 재기동이 정말 필요할 때 (코드 배포 등) | `main.sh --restart-worker <id>` 의 운영자 전용 명령으로 분리. UI에는 노출하지 않음 |
| 24/7 상주로 인한 메모리 누수 | Spec-1 §12의 OOM 항목과 동일. `mem_limit` + 정기 재기동(운영 매뉴얼)로 흡수 |
| in-flight job이 워커 크래시로 영원히 `running` 상태 | `jobs.heartbeat_at` TTL (예: 5분) 초과 시 별도 reaper 작업이 `failed`로 마감. Spec-2/3에서 reaper 구현 (Spec-1 스코프 밖) |
| Spec-1 시점에는 converter가 jobs를 소비하지 않음 | Spec-1 PR에서 임시로 `auto_converter.py`에 얇은 어댑터를 추가: idle 시 `jobs WHERE type='convert' AND status='queued'` 1건 픽업 → 기존 변환 함수 호출 → `complete` 마감. Spec-2 본 구현 시 어댑터 제거. (Spec-1 §8.4 또는 §8 신규 단계로 흡수) |

## 12. 마이그레이션 노트

- **기존 host 모드 사용자**: NAS의 `host_runtime.json`, `stop.flag`, `task_request.json` 파일은 Spec-1 PR 머지와 함께 의미를 잃는다. PR 머지 직전에 운영자가 host 워커를 정상 종료하고 위 파일을 삭제하도록 운영 노트에 명시.
- **다른 PC들의 브라우저 캐시**: 기존 UI는 host 모드 status를 5초마다 폴링한다. PR 머지 후 첫 페이지 로드 시 신규 SSE 채널로 자동 전환. 별도 마이그레이션 스크립트 불필요.
- **롤백**: 본 보강안은 Spec-1 PR 안에 포함되므로 Spec-1 §10의 롤백 절차(브랜치/워크트리 폐기)와 동일.

## 13. 명시적 스코프 밖

- in-flight 작업 자동 retry 정책 (예: failed → 재시도 N회) — Spec-2/3에서 결정
- `worker_heartbeats` 기반 알림 (slack 등 외부 알림) — Spec-3 이후
- `PATCH /api/workers` 권한 제한 — 인증 spec과 별도 진행
- 워커 수평 스케일 (워커 1개당 컨테이너 1개를 N개로) — 큐는 `SKIP LOCKED`로 이미 N-safe하지만, 운영 스코프는 별도 검토
