# Convert 진행 피드백 UI 개선 구현 계획

> **에이전트 워커용:** 필수 서브스킬 — superpowers:subagent-driven-development (권장) 또는 superpowers:executing-plans 로 task별 실행. 각 step은 `- [ ]` 체크박스로 추적.

**Goal:** Convert 버튼 클릭 후 진행 상황을 recording 단위로 즉시·연속적으로 보여주도록 ConverterPage 카드를 강화한다.

**Architecture:** 백엔드는 `auto_converter.py` 에 `Recording:` 로그 1줄 + `router.py` 에 정규식/이벤트 케이스 1개만 추가(기존 스트림과 호환). 프론트는 `ConverterProgress` 의 `taskLive` Map 을 `TaskLivePayload` 로 확장하고 순수 reducer(`applyTaskLiveEvent`)로 분리해 테스트 가능하게 한다. 활성 카드에 라이브 라인·펄스 테두리·ghost fill 을 추가하고 Convert 버튼 라벨을 상태머신화한다.

**Tech Stack:** Python 3 (backend), FastAPI/re, React 19 + TypeScript + Vite (frontend), pytest (backend tests), tsx(=TypeScript execute) + top-level assertion 스크립트 (frontend tests), CSS custom properties + @keyframes.

**참조 문서:**
- 스펙: `docs/superpowers/specs/2026-04-24-convert-progress-feedback-design.md`
- 프로젝트 테스트 순서 규칙: `CLAUDE.md` — (1) pytest → (2) Docker mockup → (3) 실제 data

---

## Task 1: 백엔드 — `Recording:` 로그 1줄 추가 (`auto_converter.py`)

**Files:**
- Modify: `rosbag2lerobot-svt/auto_converter.py` (convert_task 함수 루프 초입 + 변환 호출 직전)

**배경 (필수 읽기):**
- `convert_task(cell, task, recordings, state)` 함수가 pending recordings 를 순회하면서 `convert_single_recording` 을 호출한다.
- 카드 UI 는 `done/total_task` (예: `5/12`) 축으로 표기하므로 로그도 같은 축으로 찍어야 한다.
- `creator.has_episode(serial)` 이 `True` 인 serial 은 skip 되므로 "시도 카운트" 는 skip 이후에 증가시켜야 한다.

**`recordings` 리스트는 pending 만 담겨있다**는 것을 확인하고 루프 상단에서 전체 절대 총량(n_total)을 계산한다.

- [ ] **Step 1: 현재 코드 읽기 (맥락 파악)**

Run: `sed -n '175,245p' rosbag2lerobot-svt/auto_converter.py`
Expected: `for serial in recordings:` 루프가 보이고, 그 안에 `creator.has_episode(serial)` skip 분기와 `n_frames = convert_single_recording(...)` 호출이 보인다. `creator` 는 첫 recording 에서 lazy init 된다.

- [ ] **Step 2: 변경 작업 — 루프 이전에 total 계산, 루프 안에 시도 카운터 + 로그 라인 추가**

`convert_task` 함수 내 기존 `for serial in recordings:` 부근을 다음처럼 고친다. 아래는 **수정 후 전체 블록** (기존 로직 보존):

```python
    logger.info(
        "Converting %s: %d new recordings (output: %s)",
        cell_task, len(recordings), output_root,
    )

    # --- ADDED: 진행 표기를 위한 절대 총량 계산 ---
    # creator 미초기화 시 state 에서, 초기화 후엔 creator._existing_serials 로 동기화된다.
    # 루프 시작 시점의 done 기준값(변환된 개수)만 필요하므로 state 값만 사용.
    n_done_at_start = state.get_converted_count(cell_task)
    n_total = n_done_at_start + len(recordings)
    attempts_in_run = 0
    # ---------------------------------------------

    for serial in recordings:
        if shutdown_event.is_set():
            logger.info("Shutdown requested — stopping conversion of %s", cell_task)
            break

        ep_start = time.monotonic()
        try:
            metacard_path = RAW_BASE / cell / task / serial / "metacard.json"
            mcap_path = RAW_BASE / cell / task / serial / f"{serial}_0.mcap"

            # Create DataCreator on first recording (needs config from metacard)
            if creator is None:
                metacard = json.loads(metacard_path.read_text(encoding="utf-8"))
                config = mcap_reader.build_extraction_config(
                    detail=metacard,
                    fps=int(metacard.get("fps", 30)),
                    robot_type=str(metacard.get("robot_type", "")),
                )
                creator = DataCreator(
                    repo_id=task,
                    root=str(output_root),
                    robot_type=config.robot_type,
                    action_order=config.action_order,
                    joint_order=config.joint_order,
                    camera_names=config.camera_names,
                    fps=config.fps,
                )
                # Trigger lazy dataset load so _existing_serials is populated
                creator.has_episode("")
                # Sync state's converted_count with dataset reality
                state.sync_converted_count(cell_task, len(creator._existing_serials))
                # --- ADDED: creator 초기화로 done_at_start 가 바뀔 수 있으므로 재동기화 ---
                n_done_at_start = state.get_converted_count(cell_task)
                n_total = n_done_at_start + len(recordings)
                # ------------------------------------------------------------------

            # Skip already-converted recordings
            if creator.has_episode(serial):
                logger.debug("  Skip (already exists): %s/%s", cell_task, serial)
                continue

            # --- ADDED: 시도 카운터 증가 + 로그 1줄 ---
            attempts_in_run += 1
            current_abs = n_done_at_start + attempts_in_run
            logger.info(
                "  Recording: %s/%s (%d of %d)",
                cell_task, serial, current_abs, n_total,
            )
            # ------------------------------------------

            metacard = json.loads(metacard_path.read_text(encoding="utf-8"))
            n_frames = convert_single_recording(
                mcap_path=mcap_path,
                metacard_path=metacard_path,
                creator=creator,
                hz_min_ratio=HZ_MIN_RATIO,
                custom_metadata={
                    "Serial_number": serial,
                    "tags": metacard.get("tags", []),
                    "grade": "",
                    "intervention": bool(metacard.get("intervention", False)),
                    "is_succeed": bool(metacard.get("is_succeed", True)),
                },
            )
```

(아래 기존 코드 — `creator._rebuilt_from_corruption` 분기부터 — 는 건드리지 않는다.)

- [ ] **Step 3: 파이썬 문법/임포트 검증**

Run: `python3 -m py_compile rosbag2lerobot-svt/auto_converter.py`
Expected: 무응답(성공).

- [ ] **Step 4: Commit**

```bash
git add rosbag2lerobot-svt/auto_converter.py
git -C rosbag2lerobot-svt add auto_converter.py 2>/dev/null || true
git commit -m "feat(converter): log recording start with n/total per run"
```

> 참고: `rosbag2lerobot-svt` 는 서브모듈이다. 서브모듈 내부 커밋 + 상위 포인터 업데이트 모두 필요하다. 먼저 서브모듈 디렉토리 안에서 커밋한 후 상위에서 submodule 포인터 커밋하는 2단계가 필요하면:
> ```bash
> cd rosbag2lerobot-svt && git add auto_converter.py \
>   && git commit -m "feat(converter): log recording start with n/total per run" \
>   && cd .. \
>   && git add rosbag2lerobot-svt \
>   && git commit -m "chore: bump rosbag2lerobot-svt for recording-start log"
> ```

---

## Task 2: 백엔드 — `_RECORDING_RE` 정규식 + 파싱 케이스 (TDD)

**Files:**
- Test: `tests/test_converter_router.py` (기존 파일에 테스트 케이스 추가)
- Modify: `backend/converter/router.py` (정규식 1개 + `_parse_log_line` 내 케이스 1개 추가)

- [ ] **Step 1: 실패하는 테스트 추가**

`tests/test_converter_router.py` 파일 끝에 다음 3개 테스트 추가:

```python
def test_parse_recording_start_line():
    event = _parse_log_line(
        "2026-04-24 12:04:10 [INFO]   Recording: "
        "cell003/pick_and_place/20260424_120410_000001 (6 of 12)",
    )

    assert event == {
        "type": "recording_start",
        "ts": "2026-04-24 12:04:10",
        "recording": "cell003/pick_and_place/20260424_120410_000001",
        "index": 6,
        "total": 12,
    }


def test_parse_recording_start_line_single():
    event = _parse_log_line(
        "2026-04-24 12:04:10 [INFO]   Recording: cell001/task_a/R_001 (1 of 1)",
    )

    assert event == {
        "type": "recording_start",
        "ts": "2026-04-24 12:04:10",
        "recording": "cell001/task_a/R_001",
        "index": 1,
        "total": 1,
    }


def test_parse_recording_start_line_three_level_task():
    event = _parse_log_line(
        "2026-04-24 12:04:10 [INFO]   Recording: cell001/outer/inner/R_042 (3 of 10)",
    )

    assert event == {
        "type": "recording_start",
        "ts": "2026-04-24 12:04:10",
        "recording": "cell001/outer/inner/R_042",
        "index": 3,
        "total": 10,
    }
```

- [ ] **Step 2: 테스트 실행해서 실패 확인**

Run: `python -m pytest tests/test_converter_router.py -v`
Expected: 3개 신규 테스트가 모두 실패 (반환값이 `None` 이거나 기존 정규식에 안 걸림). 기존 4개는 통과.

- [ ] **Step 3: 정규식 + 파싱 케이스 추가**

`backend/converter/router.py` 의 정규식 선언 블록(기존 `_FINALIZED_RE`, `_SCAN_RE` 근처 ~60줄 부근)에 한 줄 추가:

```python
_RECORDING_RE = re.compile(
    r"Recording:\s+(.+?)\s+\((\d+)\s+of\s+(\d+)\)"
)
```

그리고 `_parse_log_line` 함수 내부, 기존 `converting_m = _CONVERTING_RE.search(msg)` 블록 **직후** 에 다음 케이스를 끼워넣는다(순서 중요: `converted` → `failed` → `converting` → **`recording_start`** → `finalizing` → `finalized` → `scan`):

```python
    rec_m = _RECORDING_RE.search(msg)
    if rec_m:
        return {
            "type": "recording_start", "ts": ts,
            "recording": rec_m.group(1),
            "index": int(rec_m.group(2)),
            "total": int(rec_m.group(3)),
        }
```

- [ ] **Step 4: 테스트 실행해서 통과 확인**

Run: `python -m pytest tests/test_converter_router.py -v`
Expected: 7개 테스트 전부 통과.

- [ ] **Step 5: Commit**

```bash
git add backend/converter/router.py tests/test_converter_router.py
git commit -m "feat(converter): parse recording_start events from log stream"
```

---

## Task 3: 프론트 — `LogEvent` 타입 확장

**Files:**
- Modify: `frontend/src/types/index.ts` (LogEventType union, LogEvent interface)

- [ ] **Step 1: 타입 확장**

`frontend/src/types/index.ts` 에서 `LogEventType` 유니언에 `'recording_start'` 추가, `LogEvent` 인터페이스에 `index?`, `total?` 이 **이미 존재**(기존 scan 용 total/tasks/pending 과 별개 의미) 하는지 확인해 주석 갱신.

기존 정의 (134~157줄):

```ts
export type LogEventType =
  | 'converted'
  | 'failed'
  | 'converting'
  | 'finalizing'
  | 'finalized'
  | 'scan'
  | 'warning'
  | 'info'
  | 'error'

export interface LogEvent {
  type: LogEventType
  ts: string
  recording?: string
  frames?: number
  duration?: number
  error_code?: string
  reason?: string
  task?: string
  count?: number
  tasks?: number
  pending?: number
  message?: string
}
```

다음처럼 수정:

```ts
export type LogEventType =
  | 'converted'
  | 'failed'
  | 'converting'
  | 'recording_start'
  | 'finalizing'
  | 'finalized'
  | 'scan'
  | 'warning'
  | 'info'
  | 'error'

export interface LogEvent {
  type: LogEventType
  ts: string
  recording?: string
  frames?: number
  duration?: number
  error_code?: string
  reason?: string
  task?: string
  count?: number
  tasks?: number
  pending?: number
  message?: string
  // recording_start 전용 — 카드의 done/total_task 축과 같은 의미.
  index?: number
  total?: number
}
```

- [ ] **Step 2: 타입체크**

Run: `cd frontend && npx tsc --noEmit`
Expected: 에러 없음.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/types/index.ts
git commit -m "feat(frontend): add recording_start log event type"
```

---

## Task 4: 프론트 — `applyTaskLiveEvent` 순수 reducer + `TaskLivePayload` (TDD)

**Files:**
- Modify: `frontend/src/components/converterProgressReducer.ts` (신규 파일)
- Modify: `frontend/src/components/ConverterProgress.tsx` (기존 내부 함수 삭제 후 새 파일에서 import)
- Test: `frontend/tests/converterProgressLive.test.ts` (신규)

**배경:** 기존 `ConverterProgress.tsx` 안에 `applyTaskLiveEvent(live, ev, convertingTask)` 함수(41~67줄) + `sameTaskLive` 가 있다. Map 변이 방식이라 테스트가 어렵다. 이를 순수 함수 + 구조체로 분리.

- [ ] **Step 1: 실패하는 테스트 파일 작성**

`frontend/tests/converterProgressLive.test.ts` 신규 파일:

```ts
import {
  applyEvent,
  initialState,
  resetLive,
  type LiveState,
  type TaskLivePayload,
} from '../src/components/converterProgressReducer'
import type { LogEvent } from '../src/types'

function assertEqual<T>(actual: T, expected: T, label: string) {
  const a = JSON.stringify(actual)
  const e = JSON.stringify(expected)
  if (a !== e) {
    throw new Error(`${label}: expected ${e}, got ${a}`)
  }
}

function payloadWithoutTime(p: TaskLivePayload | undefined): Omit<TaskLivePayload, 'recordingStartedAt' | 'failureFlashUntil'> | undefined {
  if (!p) return undefined
  const { recordingStartedAt: _a, failureFlashUntil: _b, ...rest } = p
  return rest
}

// Scenario 1: 단일 task 전체 사이클
{
  let s: LiveState = initialState()
  const task = 'cell001/pick'

  const events: LogEvent[] = [
    { type: 'converting', ts: 't1', task, count: 12 },
    { type: 'recording_start', ts: 't2', recording: `${task}/R_001`, index: 6, total: 12 },
    { type: 'converted', ts: 't3', recording: `${task}/R_001`, frames: 100, duration: 2.1 },
    { type: 'recording_start', ts: 't4', recording: `${task}/R_002`, index: 7, total: 12 },
    { type: 'finalizing', ts: 't5', task },
    { type: 'finalized', ts: 't6', task },
  ]

  const expectedAfter: Array<Partial<TaskLivePayload>> = [
    { phase: 'converting' },
    { phase: 'converting', recordingIndex: 6, recordingTotal: 12, recordingSerial: 'R_001' },
    { phase: 'converting', recordingIndex: 6, recordingTotal: 12, recordingSerial: 'R_001' }, // converted clears startedAt (checked separately)
    { phase: 'converting', recordingIndex: 7, recordingTotal: 12, recordingSerial: 'R_002' },
    { phase: 'finalizing' },
    { phase: 'done' },
  ]

  for (let i = 0; i < events.length; i++) {
    s = applyEvent(s, events[i])
    const got = payloadWithoutTime(s.live.get(task))
    const exp = expectedAfter[i]
    assertEqual(got, exp, `scenario1 step ${i} (${events[i].type})`)
  }

  // converted 직후 startedAt 이 reset 되어야 함
  // (시퀀스를 다시 검증)
  let s2 = initialState()
  s2 = applyEvent(s2, events[0])
  s2 = applyEvent(s2, events[1])
  const afterRec = s2.live.get(task)?.recordingStartedAt
  s2 = applyEvent(s2, events[2])
  const afterConv = s2.live.get(task)?.recordingStartedAt
  if (afterRec === undefined || afterConv !== undefined) {
    throw new Error(`scenario1: startedAt lifecycle wrong (rec=${afterRec}, conv=${afterConv})`)
  }
}

// Scenario 2: recording_start 누락 (로그 한 줄 빠진 경우) — fallback
{
  let s: LiveState = initialState()
  const task = 'cell001/pick'
  s = applyEvent(s, { type: 'converting', ts: 't1', task, count: 5 })
  s = applyEvent(s, { type: 'converted', ts: 't2', recording: `${task}/R_x`, frames: 10 })
  s = applyEvent(s, { type: 'converted', ts: 't3', recording: `${task}/R_y`, frames: 10 })
  const p = s.live.get(task)
  if (p?.phase !== 'converting') {
    throw new Error(`scenario2: phase should stay converting, got ${p?.phase}`)
  }
  if (p?.recordingIndex !== undefined) {
    throw new Error(`scenario2: index should stay undefined without recording_start, got ${p?.recordingIndex}`)
  }
}

// Scenario 3: 여러 task 순차 converting — 이전 task 정리
{
  let s: LiveState = initialState()
  s = applyEvent(s, { type: 'converting', ts: 't1', task: 'A/x', count: 3 })
  s = applyEvent(s, { type: 'recording_start', ts: 't2', recording: 'A/x/R1', index: 1, total: 3 })
  s = applyEvent(s, { type: 'converting', ts: 't3', task: 'B/y', count: 2 })
  // A 는 더 이상 active 아니어야 함
  if (s.live.has('A/x')) {
    throw new Error(`scenario3: A/x should be cleared when B/y starts, live=${JSON.stringify(Array.from(s.live.entries()))}`)
  }
  if (s.live.get('B/y')?.phase !== 'converting') {
    throw new Error(`scenario3: B/y should be converting`)
  }
}

// Scenario 4: failed 이벤트 → failureFlashUntil 설정
{
  let s: LiveState = initialState()
  const task = 'cell001/pick'
  s = applyEvent(s, { type: 'converting', ts: 't1', task, count: 3 })
  s = applyEvent(s, { type: 'recording_start', ts: 't2', recording: `${task}/R_1`, index: 1, total: 3 })
  const before = Date.now()
  s = applyEvent(s, { type: 'failed', ts: 't3', recording: `${task}/R_1`, error_code: 'E_X', reason: 'boom' })
  const p = s.live.get(task)
  if (!p?.failureFlashUntil || p.failureFlashUntil < before) {
    throw new Error(`scenario4: failureFlashUntil should be in future, got ${p?.failureFlashUntil}`)
  }
  // phase 는 converting 유지
  if (p.phase !== 'converting') {
    throw new Error(`scenario4: phase should stay converting, got ${p.phase}`)
  }
}

// Scenario 5: container 상태가 바뀌면 reset
{
  const s2: LiveState = resetLive()
  if (s2.live.size !== 0) throw new Error(`scenario5: resetLive should return empty map`)
  if (s2.activeConvertingTask !== null) throw new Error(`scenario5: resetLive should clear activeConvertingTask`)
}

console.log('converterProgressLive: all scenarios passed')
```

- [ ] **Step 2: 테스트 실행해 실패 확인**

Run: `cd frontend && npx tsx tests/converterProgressLive.test.ts`
Expected: 모듈이 존재하지 않아 `Cannot find module '../src/components/converterProgressReducer'` 로 실패.

- [ ] **Step 3: 리듀서 모듈 구현**

`frontend/src/components/converterProgressReducer.ts` 신규 파일:

```ts
import type { LogEvent } from '../types'

export type LivePhase = 'converting' | 'finalizing' | 'done'

export interface TaskLivePayload {
  phase: LivePhase
  recordingIndex?: number
  recordingTotal?: number
  recordingSerial?: string
  recordingStartedAt?: number
  failureFlashUntil?: number
}

export interface LiveState {
  live: Map<string, TaskLivePayload>
  activeConvertingTask: string | null
}

export function initialState(): LiveState {
  return { live: new Map(), activeConvertingTask: null }
}

export function resetLive(): LiveState {
  return initialState()
}

function taskFromRecording(recording: string): string {
  const parts = recording.split('/')
  return parts.slice(0, -1).join('/')
}

function serialFromRecording(recording: string): string {
  const parts = recording.split('/')
  return parts[parts.length - 1] ?? ''
}

function cloneLive(map: Map<string, TaskLivePayload>): Map<string, TaskLivePayload> {
  return new Map(map)
}

export function applyEvent(state: LiveState, ev: LogEvent): LiveState {
  const next = cloneLive(state.live)
  let active = state.activeConvertingTask

  if (ev.type === 'converting' && ev.task) {
    // 이전 active task 가 아직 converting 중으로 남아있으면 정리
    if (active && active !== ev.task) {
      const prev = next.get(active)
      if (prev?.phase === 'converting') next.delete(active)
    }
    next.set(ev.task, { phase: 'converting' })
    active = ev.task
    return { live: next, activeConvertingTask: active }
  }

  if (ev.type === 'recording_start' && ev.recording) {
    const task = taskFromRecording(ev.recording)
    const prev = next.get(task)
    next.set(task, {
      phase: prev?.phase ?? 'converting',
      recordingIndex: ev.index,
      recordingTotal: ev.total,
      recordingSerial: serialFromRecording(ev.recording),
      recordingStartedAt: Date.now(),
      failureFlashUntil: prev?.failureFlashUntil,
    })
    return { live: next, activeConvertingTask: active }
  }

  if (ev.type === 'converted' && ev.recording) {
    const task = taskFromRecording(ev.recording)
    const prev = next.get(task)
    if (prev) {
      next.set(task, { ...prev, recordingStartedAt: undefined })
    }
    return { live: next, activeConvertingTask: active }
  }

  if (ev.type === 'failed' && ev.recording) {
    const task = taskFromRecording(ev.recording)
    const prev = next.get(task)
    if (prev) {
      next.set(task, {
        ...prev,
        failureFlashUntil: Date.now() + 500,
      })
    }
    return { live: next, activeConvertingTask: active }
  }

  if (ev.type === 'finalizing' && ev.task) {
    const prev = next.get(ev.task)
    // 이미 done 으로 확정된 task 는 되돌리지 않음
    if (prev?.phase === 'done') return state
    next.set(ev.task, { phase: 'finalizing' })
    if (active === ev.task) active = null
    return { live: next, activeConvertingTask: active }
  }

  if (ev.type === 'finalized' && ev.task) {
    next.set(ev.task, { phase: 'done' })
    if (active === ev.task) active = null
    return { live: next, activeConvertingTask: active }
  }

  // scan / warning / info / error 등은 live 에 영향 없음
  return state
}

export function applyEvents(state: LiveState, events: LogEvent[]): LiveState {
  let s = state
  for (const ev of events) s = applyEvent(s, ev)
  return s
}
```

- [ ] **Step 4: 테스트 재실행해 통과 확인**

Run: `cd frontend && npx tsx tests/converterProgressLive.test.ts`
Expected: `converterProgressLive: all scenarios passed` 출력.

- [ ] **Step 5: `ConverterProgress.tsx` 에서 기존 `applyTaskLiveEvent` 제거, 신규 리듀서 연결**

`frontend/src/components/ConverterProgress.tsx` 상단 import 추가:

```ts
import {
  applyEvents,
  initialState,
  resetLive,
  type LiveState,
} from './converterProgressReducer'
```

파일에서 **삭제할 것들** (기존 줄 번호는 참고용):

1. `type TaskLive = 'converting' | 'finalizing' | 'done'` (9줄)
2. `function applyTaskLiveEvent(...)` 전체 (41~67줄)
3. `function sameTaskLive(...)` 전체 (69~75줄)
4. `useRef` import 사용 부분(`convertingTaskRef`) — 더 이상 필요 없으면 `useRef` 자체도 import 에서 제거. `wsRef` 때문에 유지 필요.

기존 state 선언 (86~87줄 근처):

```ts
  const convertingTaskRef = useRef<string | null>(null)
  const [taskLive, setTaskLive] = useState<Map<string, TaskLive>>(new Map())
```

다음으로 교체:

```ts
  const [liveState, setLiveState] = useState<LiveState>(() => initialState())
```

기존 useEffect(89~105줄):

```ts
  useEffect(() => {
    if (containerState !== 'running') {
      convertingTaskRef.current = null
      setTaskLive(prev => (prev.size === 0 ? prev : new Map()))
      return
    }
    setTaskLive(prev => {
      const next = new Map(prev)
      let convertingTask = convertingTaskRef.current
      for (const ev of events) {
        convertingTask = applyTaskLiveEvent(next, ev, convertingTask)
      }
      convertingTaskRef.current = convertingTask
      return sameTaskLive(prev, next) ? prev : next
    })
  }, [containerState, events])
```

다음으로 교체:

```ts
  useEffect(() => {
    if (containerState !== 'running') {
      setLiveState(prev => (prev.live.size === 0 ? prev : resetLive()))
      return
    }
    // events 는 매 connect 시 [] 로 리셋되므로 매번 처음부터 재구성.
    setLiveState(() => applyEvents(initialState(), events))
  }, [containerState, events])
```

카드 렌더링 내부의 `lifecycleLive = taskLive.get(t.cell_task)` 부터 `const fillWidth = ...` 까지의 블록(219~233줄) 은 **Task 5 에서 전면 재작성** 한다. 이 Task 4 에서는 타입 에러 없이 빌드되도록 해당 블록을 임시로 다음처럼 교체:

```ts
          const payload = liveState.live.get(t.cell_task)
          const lifecycleLive = payload?.phase
```

(나머지 기존 `live`, `barClass`, `fillClass`, `fillWidth` 계산 줄은 그대로 둔다 — Task 5 에서 삭제·교체.)

- [ ] **Step 6: 타입체크**

Run: `cd frontend && npx tsc --noEmit`
Expected: 에러 없음. (`TaskLive` 심볼은 더 이상 참조되지 않음 — 파일에서 제거되어 있음.)

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/converterProgressReducer.ts \
        frontend/src/components/ConverterProgress.tsx \
        frontend/tests/converterProgressLive.test.ts
git commit -m "refactor(converter): extract taskLive to pure reducer with payload"
```

---

## Task 5: 프론트 — elapsed 타이머 훅 + 라이브 라인 렌더링

**Files:**
- Modify: `frontend/src/components/ConverterProgress.tsx` (렌더링 JSX + useEffect 타이머)

- [ ] **Step 1: 타이머 state + useEffect 추가**

`ConverterProgress` 함수 시작부(기존 `const [starting, setStarting]` 바로 아래)에 추가:

```ts
  const [nowTick, setNowTick] = useState<number>(() => Date.now())

  useEffect(() => {
    // 어떤 카드라도 recordingStartedAt 이 있으면 1초 tick
    let hasActiveTimer = false
    liveState.live.forEach(p => {
      if (p.phase === 'converting' && p.recordingStartedAt) hasActiveTimer = true
    })
    if (!hasActiveTimer) return

    const id = setInterval(() => setNowTick(Date.now()), 1000)
    return () => clearInterval(id)
  }, [liveState])
```

그리고 헬퍼 함수(파일 상단, `taskCell` 근처):

```ts
function formatElapsed(ms: number): string {
  const sec = Math.max(0, Math.floor(ms / 1000))
  const m = Math.floor(sec / 60)
  const s = sec % 60
  return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`
}
```

- [ ] **Step 2: 카드 렌더링 JSX 변경 — 라이브 라인 + Convert 버튼 라벨 상태머신**

Task 4 Step 5 에서 임시로 남겨둔 기존 블록(`const live = ...`, `barClass`, `fillClass`, `fillWidth`)을 **전면 삭제**하고 아래 블록으로 교체한다. Task 5 에서 필요한 import (`TaskLivePayload`) 는 Task 4 에서 추가한 import 목록에 추가:

```ts
import {
  applyEvents,
  initialState,
  resetLive,
  type LiveState,
  type TaskLivePayload,
} from './converterProgressReducer'
```

```tsx
          const payload: TaskLivePayload | undefined = liveState.live.get(t.cell_task)
          const phase = payload?.phase
          const pct = t.total > 0 ? Math.round((t.done / t.total) * 100) : 0
          const hasPending = t.pending > 0

          // phase → 카드 테두리/바 스타일
          const isLiveActive = phase === 'converting' || phase === 'finalizing'
          const isFailureFlashing = !!payload?.failureFlashUntil
            && payload.failureFlashUntil > nowTick

          const cardClass =
            'cvp-card'
            + (isLiveActive ? ' is-live' : '')
            + (isFailureFlashing ? ' is-failure-flash' : '')

          const barClass = phase === 'finalizing' ? 'cvp-card-bar is-finalizing' : 'cvp-card-bar'
          const fillClass = phase === 'converting'
            ? 'cvp-card-bar-fill is-converting'
            : 'cvp-card-bar-fill'
          const fillWidth = phase === 'finalizing' || phase === 'done' ? '100%' : `${pct}%`

          // ghost fill (다음 recording 자리를 반투명으로 예고)
          const ghostLeft = phase === 'converting' && t.total > 0 ? pct : 0
          const ghostWidth = phase === 'converting' && t.total > 0
            ? Math.round((1 / t.total) * 100)
            : 0

          // 라이브 라인 텍스트
          let liveLine: { label: string; serial?: string } | null = null
          if (phase === 'converting') {
            if (payload?.recordingIndex && payload?.recordingTotal) {
              const elapsed = payload.recordingStartedAt
                ? formatElapsed(nowTick - payload.recordingStartedAt)
                : null
              liveLine = {
                label: elapsed
                  ? `Recording ${payload.recordingIndex}/${payload.recordingTotal} · ${elapsed}`
                  : `Recording ${payload.recordingIndex}/${payload.recordingTotal}`,
                serial: payload.recordingSerial,
              }
            } else {
              liveLine = { label: 'Converting…' }
            }
          } else if (phase === 'finalizing') {
            liveLine = { label: 'Finalizing…' }
          } else if (phase === 'done') {
            liveLine = { label: 'Done' }
          }

          // Convert 버튼 라벨 상태머신
          const isStartingThis = starting === t.cell_task
          const buttonDisabled = !canStart || !hasPending || isLiveActive || isStartingThis
          const buttonLabel =
            isStartingThis ? 'Starting…'
              : isLiveActive ? 'Running'
                : phase === 'done' && !hasPending ? 'Convert'
                  : 'Convert'
```

(JSX `return` 내부의 `<div key={t.cell_task} className="cvp-card">` 를 `<div key={t.cell_task} className={cardClass}>` 로 바꾼다.)

기존 footer 의 status badge 블록 (247~268줄) 을 다음으로 교체:

```tsx
              <div className="cvp-card-footer">
                <div className="cvp-card-footer-left">
                  {liveLine && (
                    <span
                      className={`cvp-live-line cvp-live-${phase}`}
                      role="status"
                      aria-live="polite"
                    >
                      <span className="dot" />
                      {liveLine.label}
                      {liveLine.serial && (
                        <span
                          className="cvp-live-serial"
                          style={{ fontFamily: 'var(--font-mono)' }}
                        >
                          {liveLine.serial}
                        </span>
                      )}
                    </span>
                  )}
                  {!liveLine && t.failed > 0 && (
                    <div className="cvp-card-failed">{t.failed} failed</div>
                  )}
                </div>
                <button
                  type="button"
                  className="btn-secondary cvp-card-convert"
                  disabled={buttonDisabled}
                  title={getTaskConvertTitle({
                    dockerAvailable,
                    canStart,
                    hasPending,
                  })}
                  onClick={() => startTask(t.cell_task)}
                >
                  {buttonLabel}
                </button>
              </div>
```

그리고 기존 bar 블록(243~245줄) 을 ghost fill 추가한 버전으로 교체:

```tsx
              <div className={barClass}>
                <div className={fillClass} style={{ width: fillWidth }} />
                {phase === 'converting' && ghostWidth > 0 && (
                  <div
                    className="cvp-card-bar-ghost"
                    style={{ left: `${ghostLeft}%`, width: `${ghostWidth}%` }}
                  />
                )}
              </div>
```

- [ ] **Step 3: 타입체크**

Run: `cd frontend && npx tsc --noEmit`
Expected: 에러 없음.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/ConverterProgress.tsx
git commit -m "feat(converter): live line with recording counter and elapsed timer"
```

---

## Task 6: 프론트 — CSS (`.is-live` 테두리 펄스, ghost fill, live 라인, failure flash, reduced-motion)

**Files:**
- Modify: `frontend/src/App.css` (기존 converter 카드 블록 1468줄 근처 뒤에 추가)

- [ ] **Step 1: CSS 추가**

`frontend/src/App.css` 내 `@keyframes cvp-pulse` 정의(1453~1456줄) **바로 뒤**, `@media (prefers-reduced-motion: reduce)` 블록(1458줄) **앞** 에 아래를 삽입:

```css
/* --- Live card state (A안) --- */
.cvp-card.is-live {
  border-color: var(--c-blue);
  box-shadow: 0 0 0 1px var(--c-blue-dim);
  animation: cvp-live-pulse 2s ease-in-out infinite;
}

@keyframes cvp-live-pulse {
  0%, 100% { box-shadow: 0 0 0 1px var(--c-blue-dim); }
  50%      { box-shadow: 0 0 0 2px var(--c-blue); }
}

.cvp-card.is-failure-flash {
  border-color: var(--c-red);
  box-shadow: 0 0 0 2px var(--c-red);
  transition: box-shadow 200ms ease-out, border-color 200ms ease-out;
}

/* Ghost fill — 진행 중 카드의 다음 칸 자리를 shimmer 로 예고 */
.cvp-card-bar { position: relative; }
.cvp-card-bar-ghost {
  position: absolute;
  top: 0; bottom: 0;
  background: linear-gradient(
    90deg,
    rgba(74,158,255,0) 0%,
    rgba(74,158,255,0.35) 50%,
    rgba(74,158,255,0) 100%
  );
  background-size: 200% 100%;
  animation: cvp-shimmer 1.4s linear infinite;
  pointer-events: none;
}

/* 라이브 라인 — 기존 status-badge 자리를 확장 */
.cvp-live-line {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  font-weight: 600;
  color: var(--c-blue);
}
.cvp-live-line .dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--c-blue);
  animation: cvp-blink 1.1s ease-in-out infinite;
}
.cvp-live-line.cvp-live-finalizing {
  color: var(--c-blue);
}
.cvp-live-line.cvp-live-finalizing .dot {
  background: var(--c-blue);
}
.cvp-live-line.cvp-live-done {
  color: var(--c-green);
}
.cvp-live-line.cvp-live-done .dot {
  background: var(--c-green);
  animation: none;
}
.cvp-live-serial {
  font-size: 10px;
  color: var(--text-muted);
  margin-left: 2px;
}
```

그리고 기존 `@media (prefers-reduced-motion: reduce)` 블록(1458~1468줄 근처)에 항목 추가 — 기존 블록을 다음으로 확장:

```css
@media (prefers-reduced-motion: reduce) {
  .cvp-bar-fill,
  .cvp-card-bar-fill {
    transition: none;
  }

  .cvp-card-bar-fill.is-converting,
  .cvp-card-bar.is-finalizing,
  .cvp-card-bar-ghost,
  .cvp-status-badge .dot,
  .cvp-live-line .dot,
  .cvp-card.is-live {
    animation: none;
  }
}
```

- [ ] **Step 2: Vite 개발 서버로 시각 확인**

Run: `cd frontend && npx vite --port 5173` (백그라운드)
브라우저에서 `http://localhost:5173` 열어 ConverterPage 로 이동, DevTools 에서 `.cvp-card.is-live` 클래스 수동 토글해 테두리 펄스·ghost fill·라이브 라인 렌더 확인. 멈춘 후 서버 종료.

Expected: 테두리가 2초 주기 파란색 펄스, ghost fill 이 오른쪽으로 흐르는 shimmer, 라이브 라인 텍스트 파란색.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/App.css
git commit -m "style(converter): live card pulse, ghost fill, live line, failure flash"
```

---

## Task 7: 프론트 — ConverterPage Hero "Scanning…" 칩

**Files:**
- Modify: `frontend/src/components/ConverterProgress.tsx` (Hero 옆)

**조건:** `containerState === 'running'` + `activeConvertingTask === null` + 최근 이벤트 중 `scan` 이 있고 `converting` 이 아직 없음.

- [ ] **Step 1: Scanning 판단 로직 + 렌더**

`ConverterProgress` 내부, `totals` 계산 직후 에 다음 로직 추가:

```ts
  const hasAnyConverting = Array.from(liveState.live.values()).some(
    p => p.phase === 'converting',
  )
  const lastEvent = events[events.length - 1]
  const isScanning =
    containerState === 'running'
    && !hasAnyConverting
    && !!lastEvent
    && lastEvent.type === 'scan'
```

Hero 블록(`<div className="cvp-hero">`) 내 `<div className="cvp-hero-right">` 안의 `<div className="cvp-pills">` **뒤**에 칩 추가:

```tsx
            {isScanning && (
              <span className="cvp-pill cvp-pill-scanning" role="status" aria-live="polite">
                <span className="cvp-pill-label">Scanning…</span>
              </span>
            )}
```

`App.css` 에 `.cvp-pill-scanning` 스타일 추가(기존 `.cvp-pill-yellow` 근처):

```css
.cvp-pill-scanning {
  background: var(--c-blue-dim);
  color: var(--c-blue);
  animation: cvp-blink 1.2s ease-in-out infinite;
}
@media (prefers-reduced-motion: reduce) {
  .cvp-pill-scanning { animation: none; }
}
```

- [ ] **Step 2: 타입체크**

Run: `cd frontend && npx tsc --noEmit`
Expected: 에러 없음.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/ConverterProgress.tsx frontend/src/App.css
git commit -m "feat(converter): scanning pill before first task converting event"
```

---

## Task 8: 프론트 — ConverterLogs `● LIVE` 인디케이터 + `recording_start` EventRow

**Files:**
- Modify: `frontend/src/components/ConverterLogs.tsx`

- [ ] **Step 1: `eventTeaser` + `EventRow` 에 `recording_start` 케이스 추가**

`eventTeaser` 함수 내 `case 'converting':` 위 또는 아래에 추가:

```ts
    case 'recording_start':
      return `Recording ${recordingName(event.recording!)} (${event.index}/${event.total})`
```

`EventRow` 컴포넌트 switch 문에 신규 case 추가 — 기존 `case 'converting':` **다음**:

```tsx
    case 'recording_start':
      return (
        <div className="log-event log-converting">
          {time}
          <span className="log-badge log-badge-active">REC</span>
          <span className="log-task">{recordingTask(event.recording!)}</span>
          <span className="log-recording" style={{ fontFamily: 'var(--font-mono)' }}>
            {recordingName(event.recording!)}
          </span>
          <span className="log-meta">
            {event.index}/{event.total}
          </span>
        </div>
      )
```

- [ ] **Step 2: toggle 버튼에 `● LIVE` 인디케이터**

`ConverterLogs` 컴포넌트 내에서 실행 중 여부 판단 + 버튼 라벨 확장. `function ConverterLogs({ containerState, events, open, onToggle }: Props)` 내부 `const counts = ...` 아래에 다음 추가:

```ts
  const isRunning = containerState === 'running'
```

기존 toggle 버튼(`<button className="cvl-toggle">`) 내 `<span className="cvl-toggle-label">Activity</span>` 를 다음으로 교체:

```tsx
        <span className="cvl-toggle-label">
          Activity
          {isRunning && (
            <span className="cvl-live-indicator" aria-label="live">
              <span className="cvl-live-dot" /> LIVE
            </span>
          )}
        </span>
```

`App.css` 에 스타일 추가(`.cvl-teaser` 근처 1625줄):

```css
.cvl-live-indicator {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-left: 8px;
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 0.05em;
  color: var(--c-red);
}
.cvl-live-dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--c-red);
  animation: cvp-blink 1s ease-in-out infinite;
}
@media (prefers-reduced-motion: reduce) {
  .cvl-live-dot { animation: none; }
}
```

- [ ] **Step 3: 타입체크**

Run: `cd frontend && npx tsc --noEmit`
Expected: 에러 없음.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/ConverterLogs.tsx frontend/src/App.css
git commit -m "feat(converter): LIVE indicator and recording_start event row"
```

---

## Task 9: 프론트 — 빌드 회귀 확인

**Files:** (read-only)

- [ ] **Step 1: 풀 타입체크 + 프로덕션 빌드**

Run: `cd frontend && npx tsc -b && npx vite build`
Expected: 타입 에러 0, 빌드 성공, `dist/` 생성.

- [ ] **Step 2: 프론트 모든 테스트 실행**

Run: `cd frontend && for f in tests/*.test.ts; do echo "=== $f ==="; npx tsx "$f"; done`
Expected: `converterUx.test.ts`, `appChrome.test.ts`, `converterProgressLive.test.ts` 모두 예외 없이 완료.

- [ ] **Step 3: 백엔드 pytest 회귀**

Run: `python -m pytest tests/ -x -q`
Expected: 전체 통과 (또는 변경과 무관한 기존 실패만).

- [ ] **Step 4: 회귀 이상 있을 경우만** 수정 후 수정본을 **이전 Task 의 연장 커밋** 으로 분리 커밋. 이상 없으면 다음 Task 로.

---

## Task 10: Docker mockup 으로 실로그 검증

**Files:** (실행만)

프로젝트 규칙 상 pytest 통과 후 Docker mockup 단계.

- [ ] **Step 1: 컨테이너 빌드**

Run: `cd rosbag2lerobot-svt && docker compose -f ../docker/converter/docker-compose.yml -p convert-server build` (또는 `./main.sh` 메뉴 `7`)
Expected: 이미지 빌드 성공.

- [ ] **Step 2: mockup 데이터로 실행**

Run: `bash main.sh` 메뉴 `7` (Converter Build + Run) — mockup NAS 경로가 설정돼 있다는 전제. 실제 경로는 `CURATION_DATA_ROOT` 환경변수로 지정 가능.
Expected: 컨테이너 stdout 에 다음 패턴 라인 등장:

```
2026-04-24 HH:MM:SS [INFO]   Recording: <cell>/<task>/<serial> (N of M)
```

- [ ] **Step 3: UI 띄우고 육안 검증**

Run: `bash main.sh` 메뉴 `3` (Up UI + Converter)
브라우저 `http://localhost:18080` → Converter 페이지. 체크 항목:

- [ ] 카드 Convert 버튼 → "Starting…" → "Running" 전환, 버튼이 파란 펄스 테두리 있는 카드에 고정
- [ ] 카드 안에 `▶ Recording N/M · MM:SS` 라이브 라인, 그 아래 serial (mono)
- [ ] 진행률 바 오른쪽에 파란 shimmer(ghost fill) 한 칸 자리
- [ ] Activity 드로어 toggle 옆에 빨간 `● LIVE` 점
- [ ] Activity 펼치면 `REC cell/task/serial (6/12)` 로그 라인 보임
- [ ] Start 버튼 클릭 직후 scan 직전까지 Hero 옆에 `Scanning…` 칩

- [ ] **Step 4: 접근성 — reduce-motion 검증**

브라우저 DevTools → Rendering → "Emulate CSS media feature prefers-reduced-motion: reduce" → 테두리 펄스·ghost shimmer·LIVE 점 깜빡임 모두 정지, 정적 색만 유지.

- [ ] **Step 5: QA 스크린샷 저장**

브라우저에서 다음 상태 각각 스크린샷 후 저장:

- `qa-11-convert-live-idle.png`
- `qa-12-convert-live-running.png`
- `qa-13-convert-live-finalizing.png`
- `qa-14-convert-live-reduced-motion.png`

- [ ] **Step 6: Commit (스크린샷)**

```bash
git add qa-11-*.png qa-12-*.png qa-13-*.png qa-14-*.png
git commit -m "test: QA screenshots for converter live feedback"
```

---

## Task 11: 실제 데이터 검증

**Files:** (실행만)

- [ ] **Step 1: NAS 마운트 확인**

Run: `ls /mnt/synology/data/data_div/2026_1/lerobot/`
Expected: cell 디렉토리 목록.

- [ ] **Step 2: 실제 데이터로 한 task 실행**

UI 에서 pending 있는 task 하나 선택 → Convert 클릭. 5~10 recording 분량 권장.

- [ ] **Step 3: 실제 값 검증**

- [ ] `Recording N/M` 의 M 이 카드 상단 `done/total` 의 total 과 일치
- [ ] recording 완료될 때마다 N 이 1 증가, 진행률 바 한 칸 차오름, ghost fill 이 오른쪽으로 한 칸 이동
- [ ] 실패(빨간 플래시) 섞였을 때 — N 이 계속 올라가고, 테두리가 500ms 붉게 번쩍 후 파란색 복귀
- [ ] finalizing → done 전환 자연스러움
- [ ] 상단 Start 로 배치 실행했을 때 scan → converting 전환 시 `Scanning…` 칩이 잠깐 뜨다 사라짐

- [ ] **Step 4: 이상 없으면 main 으로 통합 준비 완료**

---

## Self-Review 체크리스트

- [ ] 스펙 §3.1 (로그 라인) → Task 1 ✓
- [ ] 스펙 §3.2 (정규식/파서) → Task 2 ✓
- [ ] 스펙 §3.3 (하위 호환) → Task 2 Step 5 회귀 테스트로 검증 ✓
- [ ] 스펙 §4.1 (타입) → Task 3 ✓
- [ ] 스펙 §4.2 (payload 상태머신) → Task 4 ✓
- [ ] 스펙 §4.3 (이벤트 전이) → Task 4 (리듀서 + 테스트 시나리오) ✓
- [ ] 스펙 §4.4 (활성 카드 렌더) → Task 5, 6 ✓
- [ ] 스펙 §4.5 (라이브 라인 내용) → Task 5 ✓
- [ ] 스펙 §4.6 (Convert 버튼 라벨) → Task 5 ✓
- [ ] 스펙 §4.7 (Elapsed 타이머) → Task 5 ✓
- [ ] 스펙 §4.8 (실패 플래시) → Task 4 리듀서 + Task 6 CSS ✓
- [ ] 스펙 §4.9 (Scanning 칩) → Task 7 ✓
- [ ] 스펙 §4.10 (Activity LIVE + REC) → Task 8 ✓
- [ ] 스펙 §4.11 (접근성) → Task 6, 7, 8 의 reduced-motion 블록 ✓
- [ ] 스펙 §5 (엣지 케이스) → Task 4 테스트 시나리오 2~5 ✓
- [ ] 스펙 §6 (pytest → Docker → 실 data) → Task 2, 9, 10, 11 ✓

---

## 실행 후보 서브스킬

**옵션 1 — Subagent-Driven (권장)**: task 별 fresh 서브에이전트. 빠른 반복.
**옵션 2 — Inline Execution**: 현 세션에서 batch 실행, checkpoint 로 리뷰.
