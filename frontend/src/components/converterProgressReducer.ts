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
  return {
    live: new Map(),
    activeConvertingTask: null,
  }
}

export function resetLive(): LiveState {
  return initialState()
}

export function taskFromRecording(recording?: string): string | null {
  if (!recording) return null
  const idx = recording.lastIndexOf('/')
  if (idx <= 0) return null
  return recording.slice(0, idx)
}

export function serialFromRecording(recording?: string): string | undefined {
  if (!recording) return undefined
  const idx = recording.lastIndexOf('/')
  return idx >= 0 ? recording.slice(idx + 1) || undefined : undefined
}

export function cloneLive(live: Map<string, TaskLivePayload>): Map<string, TaskLivePayload> {
  return new Map(Array.from(live, ([task, payload]) => [task, { ...payload }]))
}

// Parse event timestamp "YYYY-MM-DD HH:MM:SS" to milliseconds.
// Using ev.ts (not Date.now()) keeps the reducer deterministic under full
// replay — applyEvents(initialState(), events) is called on every events
// change, so Date.now() would keep resetting recordingStartedAt and
// renewing failureFlashUntil forever.
export function parseEventTs(ts?: string): number {
  if (!ts) return Date.now()
  const iso = ts.replace(' ', 'T')
  const parsed = Date.parse(iso)
  return Number.isFinite(parsed) ? parsed : Date.now()
}

export function applyEvent(state: LiveState, ev: LogEvent): LiveState {
  const live = cloneLive(state.live)
  let activeConvertingTask = state.activeConvertingTask

  if (ev.type === 'converting') {
    if (!ev.task) return state
    if (activeConvertingTask && activeConvertingTask !== ev.task && live.get(activeConvertingTask)?.phase === 'converting') {
      live.delete(activeConvertingTask)
    }
    live.set(ev.task, { phase: 'converting' })
    activeConvertingTask = ev.task
    return { live, activeConvertingTask }
  }

  if (ev.type === 'recording_start') {
    const task = taskFromRecording(ev.recording)
    if (!task) return state
    const prev = live.get(task)
    live.set(task, {
      phase: prev?.phase ?? 'converting',
      recordingIndex: ev.index,
      recordingTotal: ev.total,
      recordingSerial: serialFromRecording(ev.recording),
      recordingStartedAt: parseEventTs(ev.ts),
      failureFlashUntil: prev?.failureFlashUntil,
    })
    return { live, activeConvertingTask }
  }

  if (ev.type === 'converted') {
    const task = taskFromRecording(ev.recording)
    if (!task) return state
    const prev = live.get(task)
    if (!prev) return state
    live.set(task, {
      ...prev,
      recordingStartedAt: undefined,
    })
    return { live, activeConvertingTask }
  }

  if (ev.type === 'failed') {
    const task = taskFromRecording(ev.recording)
    if (!task) return state
    const prev = live.get(task)
    if (!prev) return state
    live.set(task, {
      ...prev,
      failureFlashUntil: parseEventTs(ev.ts) + 500,
    })
    return { live, activeConvertingTask }
  }

  if (ev.type === 'finalizing') {
    if (!ev.task) return state
    const prev = live.get(ev.task)
    if (prev?.phase === 'done') {
      return state
    }
    live.set(ev.task, {
      phase: 'finalizing',
      failureFlashUntil: prev?.failureFlashUntil,
    })
    return {
      live,
      activeConvertingTask: activeConvertingTask === ev.task ? null : activeConvertingTask,
    }
  }

  if (ev.type === 'finalized') {
    if (!ev.task) return state
    const prev = live.get(ev.task)
    live.set(ev.task, {
      phase: 'done',
      failureFlashUntil: prev?.failureFlashUntil,
    })
    return {
      live,
      activeConvertingTask: activeConvertingTask === ev.task ? null : activeConvertingTask,
    }
  }

  return state
}

export function applyEvents(state: LiveState, events: LogEvent[]): LiveState {
  return events.reduce(applyEvent, state)
}
