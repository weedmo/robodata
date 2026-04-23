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
    { phase: 'converting', recordingIndex: 6, recordingTotal: 12, recordingSerial: 'R_001' },
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
