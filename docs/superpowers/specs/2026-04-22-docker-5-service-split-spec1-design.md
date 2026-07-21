# Docker 5-서비스 분리 — Spec-1 (인프라 스켈레톤 + Postgres 도입)

> **[ARCHIVED / SUPERSEDED — 2026-07-21]** — 이 문서의 Rerun 서비스·프록시·API·SDK 통합은 제거되었습니다. 과거 기록으로만 보존하며 구현 지침으로 사용하지 마세요. 현재 구성은 `README.md`를 기준으로 합니다.

날짜: 2026-04-22
상태: Archived / superseded
대상 브랜치: `feat/docker-5-service-split` (신규)

---

## 1. 배경 / 목적

현재 curation-tools는 두 개의 독립 compose 스택(`docker/ui/`, `docker/converter/`)으로 운영되며, DB는 백엔드 컨테이너 내부에 SQLite 파일로 존재한다. 시각화(Rerun)는 백엔드 프로세스 내부에 임베디드되어 있다.

운영 규모가 커지면서 다음 문제가 드러났다:
- **리소스 충돌 우려**: split/merge/delete 워크로드가 converter와 같은 컨테이너에서 돌면 converter에 간섭한다.
- **DB 격리 부재**: SQLite 파일이 백엔드 컨테이너 볼륨 안에 있어 다른 워커가 직접 접근 곤란.
- **기동 단위 불명확**: UI와 converter가 별도 compose 프로젝트라 의존성/네트워크 공유가 어색함.
- **Rerun 통합 부족**: 독립 뷰어 컨테이너로 떠 있지 않아 여러 세션 동시 지원이 어렵다.

본 스펙은 시스템을 **5개의 분리된 서비스**로 재구성하는 전체 계획의 **1단계**이다.

## 2. 스코프 분해

전체 재구성 작업은 크기가 커서 3개 스펙으로 분해한다.

| Spec | 범위 | 상태 |
|---|---|---|
| **Spec-1 (본 문서)** | 인프라 스켈레톤 + Postgres 도입 + Rerun 독립화 + main.sh 재구성 + SQLite 백업 | 본 문서 |
| **Spec-2** | Converter를 DB 큐 워커로 전환 (`auto_converter.py` 리팩터) | 설계 문서 작성: `2026-04-25-converter-db-queue-design.md` |
| **Spec-3** | curation-worker에 split/merge/delete 로직 이관 | 후속 설계 대상 |

본 스펙 완료 시점의 상태:
- 5개 컨테이너가 전부 기동 가능하지만
- Converter는 **기존 auto_converter 로직 그대로** 동작 (큐 미연동)
- curation-worker는 **placeholder**로 기동만 되고 실질 작업 없음
- 백엔드 DB 호출 경로는 **Postgres로 완전 전환**, 기존 기능은 동일하게 동작

## 3. 결정 사항 요약

브레인스토밍 중 확정된 결정들.

| 결정 | 값 |
|---|---|
| DB 엔진 | PostgreSQL 16 (alpine) |
| DB 도입 방식 | 전면 전환, 새 DB는 빈 상태로 시작 |
| 기존 SQLite 데이터 | 백업만 보관 (이관 없음, 새 Postgres는 빈 DB로 시작) |
| Rerun 구성 | 독립 `rerun serve` 컨테이너 |
| 프론트의 Rerun 접근 | nginx `/rerun/` reverse proxy → iframe |
| Sync worker 역할 | grade 부여된 데이터의 split/merge/delete |
| Sync worker 이름 | `curation-worker` |
| 트리거 방식 | Postgres `jobs` 테이블 폴링 (Spec-2/3에서 적용) |
| Converter 트리거 | Spec-2에서 DB 큐로 전환 (본 스펙에서는 현행 유지) |
| compose 파일 | 단일 `docker/compose.yml` + profiles |
| 기본 프로파일 | `app`, `nginx`, `db`, `rerun` |
| `convert` 프로파일 | `converter` 서비스 |
| `curator` 프로파일 | `curation-worker` 서비스 |
| NAS 마운트 | 호스트와 동일 경로로 공유 (`${CURATION_DATA_ROOT}:${CURATION_DATA_ROOT}`) |
| DB 드라이버 | `asyncpg` + 얇은 래퍼 (SQLAlchemy 미도입) |
| 스키마 버전 관리 | `schema_versions` 테이블 (Alembic 미도입) |
| Postgres 포트 | 호스트 기본 비공개, `CURATION_PG_HOST_PORT` 지정 시 선택 노출 |
| 격리 방식 | git worktree + 신규 브랜치 `feat/docker-5-service-split` |

### 3.1 검토한 접근과 채택안

| 접근 | 판단 |
|---|---|
| Postgres 전환과 converter DB 큐를 한 PR에 모두 구현 | 거절. DB 드라이버, compose, API, worker claim protocol, UI 상태가 동시에 흔들려 롤백 단위가 커진다. |
| SQLite를 유지하고 `convert_requests.json`만 DB 비슷한 테이블로 대체 | 거절. worker 간 공유 DB 요구가 이미 명확하고, SQLite 파일은 컨테이너/워커 간 접근성이 낮다. |
| **Spec-1에서 Postgres 기반을 먼저 만들고, Spec-2에서 converter를 DB 큐 소비자로 전환** | 채택. DB 전환 리스크와 converter queue 리스크를 분리하면서 `jobs` 테이블 계약은 먼저 고정한다. |

## 4. 아키텍처

### 4.1 서비스 토폴로지

```
┌──────────────────────────────────────────────────────────────┐
│  compose network: curation_net                               │
│                                                              │
│  ┌────────────┐   ┌─────────┐   ┌──────────┐                │
│  │ app        │──▶│ db      │◀──│ converter│  (profile:     │
│  │ (FastAPI)  │   │ (pg 16) │   │          │   convert)     │
│  └──┬─────────┘   └─────────┘   └──────────┘                │
│     │                 ▲                                      │
│     │                 │        ┌──────────────┐              │
│     │                 └────────│curation-     │ (profile:    │
│     │                          │worker        │  curator)    │
│     │                          └──────────────┘              │
│  ┌──▼──┐   ┌──────────┐                                      │
│  │nginx│──▶│ rerun    │ gRPC:9876  web:9090                  │
│  └──┬──┘   └──────────┘                                      │
└─────┼────────────────────────────────────────────────────────┘
      │
   host:${CURATION_UI_PORT:-18080}
```

### 4.2 서비스 상세

| 서비스 | 빌드 컨텍스트 | 내부 포트 | 호스트 노출 | 프로파일 | depends_on (healthy) |
|---|---|---|---|---|---|
| `app` | `docker/ui/Dockerfile.app` | 8001 | — | default | `db` |
| `nginx` | `docker/ui/Dockerfile.nginx` | 80 | `${CURATION_UI_PORT:-18080}` | default | `app` |
| `db` | `postgres:16-alpine` + `init.sql` | 5432 | 선택적 (`${CURATION_PG_HOST_PORT}`) | default | — |
| `rerun` | `docker/rerun/Dockerfile` | 9876, 9090 | — (nginx proxy 경유) | default | — |
| `converter` | `docker/converter/Dockerfile` (기존) | — | — | `convert` | `db`¹ |
| `curation-worker` | `docker/curation-worker/Dockerfile` (신규, placeholder) | — | — | `curator` | `db` |

¹ Spec-1 시점에는 converter가 DB를 사용하지 않지만, Spec-2에서 큐 소비자가 될 때 동일 설정을 재사용하기 위해 선제적으로 `depends_on: db`를 지정한다. Spec-1에서 converter가 `db` 기동 전에 떠도 기능상 문제 없음.

### 4.3 네트워크 / 주요 env

- 내부 통신은 compose 기본 DNS. 서비스명으로 접근 (`db:5432`, `rerun:9876`).
- 공통 env:
  - `CURATION_DB_URL=postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB}`
  - `CURATION_RERUN_GRPC_URL=rerun+grpc://rerun:9876`
  - `CURATION_DATASET_ROOT_BASE=${CURATION_DATA_ROOT}`

### 4.4 볼륨

| 이름 | 유형 | 용도 | 마운트 대상 |
|---|---|---|---|
| `${CURATION_DATA_ROOT}:${CURATION_DATA_ROOT}` | bind | NAS 데이터 | app, converter, curation-worker (rw), rerun (ro) |
| `curation_pg_data` | named | Postgres 영속 | `db:/var/lib/postgresql/data` |

## 5. DB 설계

### 5.1 기존 테이블 이식 (SQLite v4 → Postgres)

| 변경 항목 | Before | After |
|---|---|---|
| PK auto-increment | `INTEGER PRIMARY KEY AUTOINCREMENT` | `SERIAL PRIMARY KEY` |
| 타임스탬프 | `TEXT DEFAULT strftime('%Y-%m-%dT%H:%M:%SZ', 'now')` | `TIMESTAMPTZ NOT NULL DEFAULT NOW()` |
| JSON 저장 (`features`, `tags`) | `TEXT` | `JSONB` |
| grade 체크 | `CHECK(grade IN ('good','normal','bad'))` | 동일 유지 |
| legacy `episode_annotations` | 존재 (v4에서 drop/보존) | **제거** (새 DB 빈 상태 시작) |

남는 테이블: `datasets`, `dataset_stats`, `episode_serials`, `annotations`.

### 5.2 신규 테이블 — `jobs`

Spec-2/3에서 사용할 공통 작업 큐.

```sql
CREATE TYPE job_type AS ENUM (
    'convert',
    'split',
    'merge',
    'delete',
    'sync_good_episodes',
    'stamp_cycles'
);
CREATE TYPE job_status AS ENUM (
    'queued',
    'running',
    'complete',
    'failed',
    'cancel_requested',
    'cancelled'
);

CREATE TABLE jobs (
    id                  BIGSERIAL PRIMARY KEY,
    type                job_type   NOT NULL,
    status              job_status NOT NULL DEFAULT 'queued',
    payload             JSONB      NOT NULL DEFAULT '{}'::jsonb,
    progress            JSONB      NOT NULL DEFAULT '{}'::jsonb,
    result              JSONB,
    error               TEXT,
    attempts            INTEGER    NOT NULL DEFAULT 0,
    worker_id           TEXT,
    dedupe_key          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at          TIMESTAMPTZ,
    heartbeat_at        TIMESTAMPTZ,
    cancel_requested_at TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ
);
CREATE INDEX idx_jobs_queued ON jobs(type, created_at) WHERE status = 'queued';
CREATE INDEX idx_jobs_running ON jobs(type, worker_id) WHERE status IN ('running', 'cancel_requested');
CREATE UNIQUE INDEX idx_jobs_active_dedupe ON jobs(type, dedupe_key)
    WHERE dedupe_key IS NOT NULL AND status IN ('queued', 'running', 'cancel_requested');
```

워커 디스패치 쿼리 패턴(참고, Spec-2/3에서 사용):
```sql
SELECT id, payload FROM jobs
WHERE status='queued' AND type=$1
ORDER BY created_at
FOR UPDATE SKIP LOCKED
LIMIT 1;
```

상태명은 기존 API와 프론트가 이미 쓰는 표현에 맞춘다. 기존 in-memory `DatasetOpsService`의 `queued/running/complete/failed` 응답을 그대로 DB-backed job 응답으로 옮기고, converter cancel/stop을 위해 `cancel_requested/cancelled`만 추가한다.

`dedupe_key`는 같은 작업의 중복 큐잉을 막기 위한 선택 필드다. Converter는 `type='convert'`, `dedupe_key='cell/task'`를 사용한다. split/merge/delete 계열은 payload에 따라 별도 dedupe 정책을 Spec-3에서 결정한다.

### 5.3 스키마 버전 관리

`PRAGMA user_version` 대체:
```sql
CREATE TABLE schema_versions (
    version    INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```
`init_db()`는 현재 최고 버전을 확인하고 필요한 마이그레이션을 순차 적용한다. Alembic은 도입하지 않는다. 새 Postgres는 빈 DB로 시작하므로 SQLite row migration은 구현하지 않는다.

### 5.4 DB 접근 레이어

- 드라이버: `asyncpg` + `asyncpg.create_pool()`
- `backend/core/db.py`를 얇은 래퍼로 재작성
  - `?` 플레이스홀더를 `$1,$2,...`로 변환하는 헬퍼
  - `execute`, `fetch_one`, `fetch_all`, `transaction()` 컨텍스트 매니저
  - 기존 `aiosqlite.Row`와 유사한 `asyncpg.Record` 접근 패턴 유지 → call site 변경 최소화
- `backend/core/config.py`에 `db_url` 필드 추가 (기존 `db_path`는 호환용으로 잠시 유지, 6단계에서 제거)

### 5.5 SQLite 백업 게이트

Postgres 전환은 데이터 이관을 하지 않는다. 대신 첫 Postgres 기동 전 다음을 완료해야 한다.

1. 호스트 SQLite 파일과 Docker volume SQLite 파일을 `docs/db-backup/<timestamp>/`에 복사한다.
2. 각 백업 파일의 size, mtime, sha256을 `MANIFEST.txt`에 기록한다.
3. 백업 스크립트가 실패하면 `main.sh --up`은 Postgres 전환 안내를 중단한다.

이 백업은 롤백/감사용 보존물이며 새 Postgres로 자동 복원하지 않는다.

## 6. 파일 레이아웃

```
curation-tools/
├── docker/
│   ├── compose.yml                     ← 신규, 단일 소스
│   ├── .env.example                    ← 신규
│   ├── db/
│   │   └── init.sql                    ← 신규
│   ├── ui/
│   │   ├── Dockerfile.app              ← 유지
│   │   ├── Dockerfile.nginx            ← 유지
│   │   └── nginx.conf                  ← 수정 (/rerun/ proxy 추가)
│   ├── converter/
│   │   └── Dockerfile                  ← 유지 (본 스펙에선 변경 없음)
│   ├── curation-worker/
│   │   ├── Dockerfile                  ← 신규 (placeholder)
│   │   └── placeholder.py              ← 신규
│   └── rerun/
│       └── Dockerfile                  ← 신규
│
├── docker/ui/docker-compose.yml        ← 삭제 (6단계에서)
├── docker/converter/docker-compose.yml ← 삭제 (6단계에서)
│
├── scripts/
│   └── backup_sqlite_metadata.sh       ← 신규
│
├── docs/
│   └── db-backup/
│       ├── .gitkeep
│       └── (백업 생성물, gitignore)
│
├── main.sh                             ← 대폭 개편
├── start.sh                            ← 하이브리드 모드 업데이트
└── .gitignore                          ← docs/db-backup/* 추가
```

### 6.1 `docker/compose.yml` 핵심 (요약)

- `networks.curation_net` 정의
- `volumes.curation_pg_data` 정의
- 6개 서비스 (위 4.2 표대로)
- 프로파일: `convert`, `curator`
- 모든 컨테이너에 `depends_on: db: {condition: service_healthy}` (Postgres 준비 완료 대기)

### 6.2 `docker/.env.example`

```env
# Host paths
CURATION_DATA_ROOT=/mnt/synology/data/data_div/2026_1
CURATION_UI_PORT=18080

# Postgres (change the password!)
POSTGRES_DB=curation
POSTGRES_USER=curation
POSTGRES_PASSWORD=change-me-in-env

# Optional: expose Postgres on host for debugging
# CURATION_PG_HOST_PORT=127.0.0.1:5433
```

실제 `.env`는 `.gitignore`에 추가.

### 6.3 `docker/ui/nginx.conf` 추가 블록

```
location /rerun/ {
    proxy_pass http://rerun:9090/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_buffering off;
}
```
프론트 `RerunViewer.tsx`의 iframe src를 `/rerun/` 상대경로로 변경.

### 6.4 `docker/curation-worker/placeholder.py`

```python
import time, logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("curation-worker")
while True:
    log.info("curation-worker placeholder — queue consumer not yet implemented (Spec-3)")
    time.sleep(60)
```

### 6.5 `scripts/backup_sqlite_metadata.sh`

백업 대상:
1. `$HOME/.local/share/curation-tools/metadata.db`
2. Docker volume `ui_service_db` (alpine 컨테이너로 tar 추출)
3. `$CURATION_DB_PATH` (env 지정 시)

출력:
```
docs/db-backup/YYYYMMDDTHHMMSS/
  host-metadata.db
  ui_service_db.tar.gz
  MANIFEST.txt   (각 파일 mtime/size/sha256)
```

## 7. `main.sh` 재구성

### 7.1 메뉴 (인터랙티브)

```
============================================
  Curation Tools — Docker Launcher
  Default: app/nginx/db/rerun   Profiles: convert | curator
  Status: app ● nginx ● db ● rerun ● | converter ○ curator ○
  UI: http://localhost:18080/   Data: /mnt/synology/.../2026_1
============================================
 Core
  1) Build all images
  2) Build all (no-cache)
  3) Up (default: app + nginx + db + rerun)
  4) Up + convert profile
  5) Up + curator profile
  6) Up everything (all profiles)
  7) Down (stop all, keep volumes)

 Logs & Shell
  8) Logs — all (follow)
  9) Logs — pick service
 10) Shell — app
 11) Shell — converter
 12) Shell — curation-worker
 13) psql — db

 Maintenance
 14) Backup SQLite metadata → docs/db-backup/
 15) Reset DB (drop volume, re-init)   [⚠ confirm]

  0) Exit (or ESC)
```

### 7.2 비대화 진입점 (CI/스크립팅)

```
./main.sh --up                 # default profile
./main.sh --up-convert
./main.sh --up-curator
./main.sh --up-all
./main.sh --down
./main.sh --logs [svc]
./main.sh --shell app|converter|curation-worker|db
./main.sh --backup-sqlite
./main.sh --reset-db --yes
```

### 7.3 `.env` 검증

`main.sh` 진입 시 `docker/.env` 존재 여부 확인. 없으면 `.env.example`을 복사하고 `POSTGRES_PASSWORD` 편집 안내 후 중단.

### 7.4 `start.sh` 업데이트

네이티브 dev 모드 유지. 단, 다음으로 변경:
- `docker compose -f docker/compose.yml up -d db` 선행
- 호스트 백엔드는 `CURATION_DB_URL=postgresql://.../localhost:${CURATION_PG_HOST_PORT}/curation`로 붙음
- 종료 시 `db`는 그대로 두거나 flag(`--stop-db`)로 선택 정지

## 8. 구현 순서

격리 먼저 확보한 뒤 단계별로 진행한다. 각 단계는 별도 PR 단위로 생성한다.

### 0. 격리 작업 공간 생성 (코드 수정 전)

- 신규 브랜치 `feat/docker-5-service-split` (base: `main`)
- git worktree 생성: `../curation-tools-docker-split/`
  - 현재 저장소와 `.git`을 공유하되 워킹 디렉터리는 완전 분리
  - 현재 브랜치 `feat/rosbag2lerobot-svt-converter`의 uncommitted 변경은 무영향
  - 서브모듈(`rosbag2lerobot-svt`)은 `git submodule update --init --recursive`로 동기화
- 이후 모든 1~7단계 작업은 `../curation-tools-docker-split/`에서 수행
- **검증**: worktree에서 `git status`가 clean이고 원본 저장소 상태가 변하지 않음

### 1. 인프라 골격

- `docker/compose.yml`, `docker/.env.example`, `docker/db/init.sql` 생성
- `docker/rerun/Dockerfile`, `docker/curation-worker/{Dockerfile, placeholder.py}` 생성
- 기존 `docker/{ui,converter}/docker-compose.yml`은 **아직 삭제하지 않음** (안전망)
- **검증**: `docker compose -f docker/compose.yml up -d db` → `pg_isready` 성공, `docker compose logs db`에 `init.sql` 적용 로그

### 2. 백엔드 DB 레이어 교체

- `backend/core/db.py` 재작성 (asyncpg pool + 얇은 래퍼)
- `backend/core/config.py`에 `db_url` 필드 추가 (기존 `db_path`와 공존)
- 53개 호출 지점 시그니처 조정
- **검증**: `pytest tests/test_db.py`(Postgres 테스트 DB) 통과, 기존 스모크 테스트 green

### 3. SQLite 백업 스크립트

- `scripts/backup_sqlite_metadata.sh` 작성
- `.gitignore`에 `docs/db-backup/*` 추가, `.gitkeep` 유지
- **검증**: 기존 호스트/볼륨의 SQLite가 `docs/db-backup/<ts>/`로 복사되고 `MANIFEST.txt` 생성. 백업 실패 시 Postgres 초기화/기동 단계로 진행하지 않음

### 4. Rerun 독립 컨테이너 + nginx proxy

- `docker/rerun/Dockerfile` 완성 (rerun-sdk 0.22+)
- `docker/ui/nginx.conf`에 `/rerun/` location 추가
- 백엔드 `rerun_service.py`가 `CURATION_RERUN_GRPC_URL` 사용하도록 수정
- 프론트 `RerunViewer.tsx` iframe src를 `/rerun/`로 변경
- **검증**: `compose up -d`로 기본 세트 기동 → UI의 Rerun 패널 정상 렌더

### 5. `main.sh` 재구성

- 섹션 7.1/7.2 대로 교체
- `start.sh` 하이브리드 모드 업데이트
- **검증**: `./main.sh --up`/`--up-all`/`--down` 정상 동작

### 6. 기존 compose 파일 제거

- `docker/ui/docker-compose.yml`, `docker/converter/docker-compose.yml` 삭제
- README/CI에서 참조 업데이트
- **검증**: `grep -r "docker/ui/docker-compose\|docker/converter/docker-compose"` 결과 없음

### 7. `start.sh` 하이브리드 모드 검증

- 호스트에서 `./start.sh` 실행 → 호스트 백엔드가 컨테이너 Postgres에 붙어 기존 UI 기능 동일 동작
- **검증**: Overview / dataset / converter status 엔드포인트 200

## 9. 검증 게이트 (전체)

Spec-1 완료 조건:

- [ ] `./main.sh --up`로 기본 세트 기동, 헬스체크 전부 `healthy`
- [ ] `http://localhost:18080/` 정상 렌더, 데이터셋 목록 로드 OK
- [ ] `/api/health` 응답 200
- [ ] Rerun iframe 정상 임베드
- [ ] `./main.sh --up-convert` 시 converter 기동 + 기존 auto_converter 동작 유지
- [ ] `./main.sh --up-curator` 시 placeholder 워커 기동, 주기 로그 확인
- [ ] `./main.sh --backup-sqlite` 실행 시 백업 디렉터리 생성
- [ ] 기존 `pytest` 스위트 green
  - **테스트 DB 전략**: compose의 `db` 서비스를 재사용한다. `docker/db/init.sql`이 `curation`과 `curation_test` 두 데이터베이스를 생성하도록 작성. pytest fixture는 `curation_test`에 붙고 테스트 사이에 스키마 drop+recreate로 격리. testcontainers 도입은 스코프 밖.
- [ ] `./main.sh --down` 시 전부 내려가고 `curation_pg_data` 볼륨 보존

## 10. 롤백 전략

| 수준 | 방법 |
|---|---|
| **전체 폐기** | `git worktree remove ../curation-tools-docker-split` + `git branch -D feat/docker-5-service-split`. 원본 저장소 무영향 |
| **단계별** | 각 PR 단위로 `git revert` |
| **DB 롤백** | Postgres 컨테이너 삭제 + `docker volume rm curation_pg_data` 후 `docs/db-backup/<ts>/`에서 SQLite 복원. SQLite row migration은 없으므로 Postgres에서 새로 만든 metadata는 자동 역이관하지 않음. 2단계에서 `db_url` 미설정 시 기존 SQLite fallback 경로를 잠시 남겨두고 6단계에서 제거 |

## 11. 머지 전략

- Spec-1 완료 후 `feat/docker-5-service-split` → `main` PR
- 현재 작업 브랜치 `feat/rosbag2lerobot-svt-converter`와 충돌 가능성은 머지 시점에 rebase 또는 충돌 해결로 대응

## 12. 가정 / 미해결 리스크

- **`features` 컬럼 JSON 구조 미검증** — 새 Postgres는 빈 DB라 기존 row 파싱 리스크는 낮다. 다만 코드가 문자열 JSON을 기대하는 호출 지점은 2단계 착수 전 점검한다.
- **Rerun `rerun serve` CLI 플래그 버전 호환성** — 0.22+의 정확한 옵션은 4단계 구현 시 공식 문서(context7 MCP) 확인.
- **`mem_limit` 레거시** — compose v2에서는 `deploy.resources.limits.memory` 권장. 본 스펙은 `mem_limit` 유지 (기존 converter compose 호환), 별도 정리 대상.
- **SQLite 하드코딩된 테스트** — 2단계 착수 전 `grep aiosqlite tests/`로 스캔 필요.
- **기존 metadata 공백** — 새 Postgres는 빈 DB이므로 첫 기동 후 데이터셋 재등록/재스캔이 필요하다. annotations도 새로 시작한다.

## 13. Spec-1 스코프 밖 (명시적 제외)

- Converter → DB 큐 소비자 전환 (Spec-2)
- split/merge/delete 로직을 curation-worker로 이관 (Spec-3)
- Alembic 도입
- 테스트 인프라 전면 개편 (testcontainers 등)
- OOM 복구 (`2026-04-22-converter-oom-recovery.md`와 독립 진행)
- Rerun 고급 기능 (auth, multi-recording replay 등)
