# Convert 진행 피드백 UI 개선 설계

**작성일**: 2026-04-24
**대상**: `ConverterPage` (카드 단위 Convert + 상단 Start 양쪽)
**접근**: A안 — 카드 강화형 (기존 레이아웃 유지, 활성 카드 내부에서 라이브 피드백 제공)

## 1. 문제

사용자가 Convert(또는 Start) 버튼을 눌렀을 때 "돌고 있는지" 체감이 부족하다.

- 버튼이 잠깐 `Starting…` 후 다시 `Convert`로 돌아가 "눌렸나?" 싶음
- 카드 진행률 바는 recording 하나가 **완전히 끝나야** 갱신 (수십 초~수 분의 정적 구간)
- 변환 상태 배지(`Converting` / `Finalizing`)는 카드 footer 작은 글자
- Activity 드로어는 기본 접힘
- 즉, **즉시성 · 진행 중 시각화 · 노출 방식** 세 층 모두 피드백 부족

## 2. 스코프

### 포함
- `ConverterPage`의 카드 단위 **Convert** 버튼, 상단 **Start** 버튼 양쪽의 피드백 강화
- 진행 해상도: **recording 단위** (백엔드에 recording 시작 로그 1줄 추가)
- 활성 카드 시각 강조 (테두리 펄스)
- 현재 처리 중인 recording 정보(`index/total`, `serial`, 경과 시간) 카드 내 인라인 표시
- Convert 버튼 라벨 상태머신 개선 (`Starting…` / `Running` 유지)
- 진행률 바 "진행 중" ghost fill
- Activity 드로어의 라이브 인디케이터(`● LIVE`)와 `recording_start` 이벤트 렌더링

### 제외 (YAGNI)
- frame 단위(%) 진행률
- ETA 계산 / 남은 시간 예측
- Activity 드로어 자동 펼침 (C안 성격 — A안에서는 제외)
- 새 페이지 / 새 탭 / 새 상위 컴포넌트
- 재시도 자동화 UI

## 3. 백엔드 변경

### 3.1 `rosbag2lerobot-svt/auto_converter.py`

`convert_single_recording` 호출 직전에 로그 1줄 추가. 카드의 `done/total_task` 표기와 숫자 축을 맞추기 위해 **절대 index** 기준으로 찍는다 (= 이 task의 전체 recordings 중 몇 번째).

루프 진입 전 한 번 계산:

```python
n_done_at_start = len(converted_map.get(cell_task, set()))
n_total = n_done_at_start + len(pending_serials)
```

루프 내부 — skip되지 않고 실제 convert로 진입하는 순간 직전:

```python
current_abs = n_done_at_start + attempts_in_run  # 1-based (attempts_in_run: 이 run에서 시도한 횟수)
logger.info("  Recording: %s/%s (%d of %d)", cell_task, serial, current_abs, n_total)
n_frames = convert_single_recording(...)
```

- `attempts_in_run`은 이 run에서 실제 시도 횟수 누적 변수(실패 포함). skip된 serial은 카운트 안 함 — 카드 `done`과 동기 유지.
- 출력 예 (카드가 `5/12`일 때 다음 recording 시도):

```
2026-04-24 12:04:10 [INFO]   Recording: cell003/pick_and_place/R_042 (6 of 12)
```

### 3.2 `backend/converter/router.py`

정규식과 이벤트 케이스 추가. 기존 `_CONVERTED_RE`, `_CONVERTING_RE`, `_FINALIZING_RE` 등과 같은 패턴.

```python
_RECORDING_RE = re.compile(
    r"Recording:\s+(.+?)\s+\((\d+)\s+of\s+(\d+)\)"
)

# _parse_log_line 내부에 케이스 추가
rec_m = _RECORDING_RE.search(msg)
if rec_m:
    return {
        "type": "recording_start", "ts": ts,
        "recording": rec_m.group(1),
        "index": int(rec_m.group(2)),
        "total": int(rec_m.group(3)),
    }
```

### 3.3 하위 호환

- 기존 `converted` / `converting` / `finalizing` / `finalized` 이벤트 동작 불변
- WebSocket `/api/converter/logs`는 파싱된 이벤트를 그대로 스트림 — 새 타입도 경유만
- 프론트에서 처리 못하는 타입은 조용히 무시 (현재 동작과 동일)

## 4. 프론트엔드 설계

### 4.1 타입 (`frontend/src/types/index.ts`)

```ts
export type LogEventType =
  | 'converted' | 'failed' | 'converting'
  | 'recording_start'          // 신규
  | 'finalizing' | 'finalized'
  | 'scan' | 'warning' | 'info' | 'error'

export interface LogEvent {
  // 기존 필드 유지
  index?: number    // recording_start 전용 (1-based, 카드 done/total과 같은 축)
  total?: number    // recording_start 전용 (task 전체 recordings 수)
  // 주의: 기존 scan 이벤트의 tasks/pending/count 필드와는 별개 의미
}
```

### 4.2 상태머신 (`ConverterProgress.tsx`)

기존 `taskLive: Map<cell_task, TaskLive>`를 per-task payload로 확장.

```ts
interface TaskLivePayload {
  phase: 'converting' | 'finalizing' | 'done'
  recordingIndex?: number
  recordingTotal?: number
  recordingSerial?: string      // e.g. "R_042"
  recordingStartedAt?: number   // Date.now()
  failureFlashUntil?: number    // Date.now() + 500 — 붉은 테두리 플래시
}
```

순수 reducer `applyTaskLiveEvent(state, event, convertingTask)` → `{ state, convertingTask }` 형태로 리팩터링해 단위 테스트 가능하게 한다. `Map`은 `useEffect` 내부에서만 조립.

### 4.3 이벤트 전이 규칙

| 이벤트 | 동작 |
|---|---|
| `converting` (task 시작) | 해당 task: `phase='converting'`, 나머지 recording 필드 reset. 이전 active task 있으면 그 task의 live 제거. |
| `recording_start` | task 추출 = `recording.split('/')[:-1].join('/')` → 해당 task payload에 index/total/serial/startedAt 갱신 |
| `converted` | 해당 task의 `recordingStartedAt` clear (다음 `recording_start` 대기). phase 변화 없음. |
| `failed` | 해당 task `failureFlashUntil = Date.now() + 500`. phase 유지. |
| `finalizing` | `phase='finalizing'`, recording 필드 전부 clear |
| `finalized` | `phase='done'` |
| containerState ≠ running | Map 전체 clear |
| WebSocket 재연결 | `events=[]` → useEffect가 빈 배열 재적용 (자연 reset) |

**누락 로그 fallback**: `recording_start` 없이 `converted`만 연속으로 들어오면 index는 갱신되지 않는다. phase는 converting 유지. 라이브 라인은 `Recording ?/?` 대신 `Converting…`으로 표시.

### 4.4 렌더링 — 활성 카드

```
┌─────────────────────────────────────────────┐
│ cell003  pick_and_place              5/12   │ ← header (그대로)
├─────────────────────────────────────────────┤
│ ▓▓▓▓▓▓░░▒░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  │ ← stable fill + ghost fill
├─────────────────────────────────────────────┤
│ ▶ Recording 6/12 · 00:48      [ Running ]  │ ← 라이브 라인 + 버튼
│   R_042                                     │ ← serial (mono, 작게)
├─────────────────────────────────────────────┤
│ Quick: passed    Full: not_run              │ ← validation (그대로)
└─────────────────────────────────────────────┘
```

- 테두리: `.cvp-card.is-live` 클래스 → 파란 2px border + 외곽 glow. `@keyframes` 느린 펄스(2s).
- ghost fill: `width = (1/total) * 100%`, `left = (done/total) * 100%`, 반투명 파랑 + shimmer.
- serial은 `fontFamily: var(--font-mono)` 인라인 스타일.

### 4.5 라이브 라인 내용

| 조건 | 표시 |
|---|---|
| phase='converting' + index/total/serial 있음 | `▶ Recording 6/12 · 00:48` + serial 줄 |
| phase='converting' + recording 정보 없음 | `▶ Converting…` |
| phase='finalizing' | `⟳ Finalizing…` (호박색) |
| phase='done' | `✓ Done` (녹색) |
| live 아님 + `t.failed > 0` | `{N} failed` (기존 유지) |

`role="status" aria-live="polite"` 부여.

### 4.6 Convert 버튼 라벨 상태

| 조건 | 라벨 | disabled |
|---|---|---|
| idle + pending > 0 | `Convert` | false |
| 로컬 `starting === cell_task` | `Starting…` | true |
| phase='converting' or 'finalizing' | `Running` | true |
| phase='done' | `Convert` (title="모두 변환됨") | true |
| canStart 조건 불충족 (docker/container) | 기존 title 로직 유지 | true |

**"Starting…" 최소 유지 시간**: 첫 `converting` 이벤트 수신 전까지 유지 (기존 try/finally 구조가 이미 커버). 만약 10초 넘으면 카드 하단에 `Container booting…` subtext 1줄.

### 4.7 Elapsed 타이머

- 상위 컴포넌트에서 `setInterval(() => setTick(t=>t+1), 1000)` — 단 **하나의** 카드라도 phase='converting' + startedAt 있을 때만 실행
- 포맷: `< 60s`는 `00:MM`, 이상은 `MM:SS`
- 타이머 off 상태에서는 interval 생성 자체를 안 해 idle cost 0

### 4.8 실패 플래시

`failed` 이벤트 수신 시 해당 task `.cvp-card.is-failure-flash` 500ms 토글 → CSS 트랜지션으로 테두리 색이 붉게 플래시 후 파란색(converting이면)으로 복귀. setTimeout로 `failureFlashUntil` expiry를 감시해 클래스 제거.

### 4.9 상단 "Start" (전체 배치) 케이스

- Hero 섹션 옆에 `Scanning…` 칩 추가 조건: `containerState==='running'` + 아직 어떤 task도 `converting` 이벤트 안 받음 + 마지막 이벤트가 `scan`
- 첫 `converting` 이벤트 수신 시 자연 제거

### 4.10 Activity 드로어 소규모 변경 (`ConverterLogs.tsx`)

- A안은 자동 펼침 **안 함**
- toggle 라벨: 실행 중이면 `Activity  ● LIVE` (작은 붉은 점, 깜빡임)
- `recording_start` EventRow 케이스 추가:
  ```
  12:04:10 · REC cell003/pick_and_place/R_042 (6 of 12)
  ```

### 4.11 접근성

- `@media (prefers-reduced-motion: reduce)`:
  - 테두리 펄스 → 정적 파란 테두리
  - ghost fill shimmer off
  - 실패 플래시 → instant (500ms 컬러 스냅 후 복귀)
  - `● LIVE` 깜빡임 off
- 라이브 라인 `aria-live="polite"`
- 테두리 색만으로 상태를 전달하지 않음 (라이브 라인 텍스트가 primary)

## 5. 에러/엣지 케이스

| 케이스 | 동작 |
|---|---|
| `recording_start`의 task가 현재 `tasks[]`에 없음 | 이벤트 무시 (다음 status refresh 대기) |
| `recording_start` 없이 `converted` 연속 | phase 유지, 라이브 라인 `Converting…` fallback |
| 여러 task 동시 converting 이벤트 (순차 배치) | 이전 active task의 live 제거, 새 task만 활성 |
| `failed` 후 다음 recording | 500ms 붉은 플래시 → 파란 펄스 복귀 |
| WebSocket drop → reconnect | 이벤트 버퍼 reset, live state 자연 재구성 |
| container 종료 | Map 전체 clear, 모든 카드 idle로 |

## 6. 테스트

프로젝트 규칙상 (1) pytest → (2) Docker mockup → (3) 실제 data 순.

### 6.1 pytest 단위

`tests/test_converter_router.py` 추가:
- `"  Recording: cell003/pick_and_place/R_042 (6 of 12)"` 파싱 → `{type:'recording_start', index:6, total:12, recording:...}`
- 엣지: `(1 of 1)`, 공백 다수
- 기존 `Converted:` 파서 회귀 검증 유지

### 6.2 프론트 단위 (Vitest)

신규 `frontend/tests/converterProgressLive.test.ts`:
- `applyTaskLiveEvent` 순수 reducer에 시퀀스 주입:
  - `converting` → `recording_start(6,12,R_042)` → `converted` → `recording_start(7,12,R_043)` → `finalizing` → `finalized`
  - 누락 로그 fallback 검증
  - 여러 task 동시 전환 검증
  - `failed` 이벤트로 `failureFlashUntil` 설정 검증

### 6.3 Docker mockup

- `rosbag2lerobot-svt` mockup 데이터로 변환 실행 → 실제 로그에 `Recording:` 라인 확인
- UI 육안 확인: 테두리 펄스, 라이브 라인, elapsed 타이머, Running 라벨, ghost fill
- Network 탭에서 WebSocket 수동 드랍 → 재연결 후 상태 복구

### 6.4 실제 data

- 실제 NAS 데이터 5~10 recording 분량 한 task 실행:
  - index/total 표기가 실제 pending과 일치
  - finalizing 전환 자연
  - 실패 recording 섞였을 때 카운터 유지

### 6.5 시각 QA

기존 `qa-*-converter-*.png` 패턴 따라:
- `qa-*-convert-live-idle.png`
- `qa-*-convert-live-running.png`
- `qa-*-convert-live-failed-flash.png`
- `qa-*-convert-live-reduced-motion.png`

## 7. 변경 파일 요약

- `rosbag2lerobot-svt/auto_converter.py` (+1 line)
- `backend/converter/router.py` (+1 regex, +1 case in `_parse_log_line`)
- `tests/test_converter_router.py` (+1~2 케이스)
- `frontend/src/types/index.ts` (`LogEventType` 확장, `LogEvent` 필드 2개)
- `frontend/src/components/ConverterProgress.tsx` (상태머신 확장, reducer 분리, 렌더링 변경)
- `frontend/src/components/ConverterLogs.tsx` (`● LIVE` 인디케이터, `recording_start` EventRow)
- `frontend/src/components/converterUx.ts` (Convert 버튼 title 로직 확장이 필요하면)
- `frontend/src/index.css` 또는 스타일 위치 — `.cvp-card.is-live`, 펄스 keyframes, ghost fill, reduced-motion 미디어쿼리
- `frontend/tests/converterProgressLive.test.ts` (신규)

## 8. 비목표 / 후속 스펙 가능성

- ETA, 남은 시간 추정 → `converted` 이벤트의 `duration` 이동 평균으로 별도 스펙
- Activity 자동 펼침(C안) — 사용자 피드백 후 고려
- Active Run 패널(B안) — 단일 실행 주력 시 재검토
- frame 단위 % — rosbag 구조상 중간 tick 비용/복잡도 평가 필요
