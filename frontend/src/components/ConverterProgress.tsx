import { useEffect, useRef, useState } from 'react'
import type { ConverterState, ConverterTaskProgress, LogEvent } from '../types'
import {
  CONVERTER_HOST_CONTROL_HINT,
  getTaskConvertTitle,
  getValidationTitle,
} from './converterUx'
import {
  applyEvents,
  initialState,
  resetLive,
  type LiveState,
  type TaskLivePayload,
} from './converterProgressReducer'
import { enqueueConvertJob, retryFailedConvertJob } from '../api/converter'
import { ConverterTaskRawRecordings } from './ConverterTaskRawRecordings'

type ValidationMode = 'quick' | 'full'

const API = '/api/converter'
// Host-only mode has no WebSocket log stream, so a status poll that bumps
// `done` or a fresh `last_updated` is the only signal we get that a task is
// actively converting. Keep the "Running" badge visible briefly so the card
// doesn't flip back to "Convert" between 5s polls.
const PROGRESS_ACTIVE_WINDOW_MS = 15_000

function isRecentIso(iso: string | null | undefined, windowMs: number): boolean {
  if (!iso) return false
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return false
  return Date.now() - t < windowMs
}

const VALIDATION_STATUS_CLASS: Record<string, string> = {
  not_run: 'cvp-val-not-run',
  running: 'cvp-val-running',
  passed: 'cvp-val-passed',
  failed: 'cvp-val-failed',
  partial: 'cvp-val-partial',
}

interface Props {
  tasks: ConverterTaskProgress[]
  containerState: ConverterState
  dockerAvailable: boolean
  taskStartAvailable: boolean
  activeCellTask: string | null
  events: LogEvent[]
  onRefresh: () => void
}

function taskLabel(cell_task: string) {
  const parts = cell_task.split('/')
  return parts[parts.length - 1] || cell_task
}

function taskCell(cell_task: string) {
  const parts = cell_task.split('/')
  return parts[0] || ''
}

function formatElapsed(ms: number): string {
  const sec = Math.max(0, Math.floor(ms / 1000))
  const m = Math.floor(sec / 60)
  const s = sec % 60
  return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`
}

export function ConverterProgress({
  tasks,
  containerState,
  dockerAvailable,
  taskStartAvailable,
  activeCellTask,
  events,
  onRefresh,
}: Props) {
  const [starting, setStarting] = useState<string | null>(null)
  const [nowTick, setNowTick] = useState<number>(() => Date.now())
  const [runningValidation, setRunningValidation] = useState<Set<string>>(new Set())
  const [liveState, setLiveState] = useState<LiveState>(() => initialState())
  const [queued, setQueued] = useState<Set<string>>(new Set())
  const [toast, setToast] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)
  const doneSnapshotRef = useRef<Map<string, number>>(new Map())
  const progressAtRef = useRef<Map<string, number>>(new Map())

  useEffect(() => {
    const now = Date.now()
    let bumped = false
    for (const t of tasks) {
      const prevDone = doneSnapshotRef.current.get(t.cell_task)
      if (prevDone === undefined) {
        doneSnapshotRef.current.set(t.cell_task, t.done)
      } else if (t.done > prevDone) {
        doneSnapshotRef.current.set(t.cell_task, t.done)
        progressAtRef.current.set(t.cell_task, now)
        bumped = true
      }
    }
    if (bumped) setNowTick(now)
  }, [tasks])

  const isProgressActive = (cell_task: string): boolean => {
    const at = progressAtRef.current.get(cell_task)
    return at !== undefined && nowTick - at < PROGRESS_ACTIVE_WINDOW_MS
  }

  useEffect(() => {
    if (queued.size === 0) return
    setQueued(prev => {
      let changed = false
      const next = new Set(prev)
      prev.forEach(key => {
        const phase = liveState.live.get(key)?.phase
        const live = phase === 'converting' || phase === 'finalizing' || phase === 'done'
        if (live || isProgressActive(key)) {
          next.delete(key)
          changed = true
        }
      })
      return changed ? next : prev
    })
  }, [liveState, queued, nowTick])

  useEffect(() => {
    if (containerState !== 'running') {
      setLiveState(prev => (prev.live.size === 0 ? prev : resetLive()))
      return
    }
    setLiveState(() => applyEvents(initialState(), events))
  }, [containerState, events])

  useEffect(() => {
    if (!toast) return
    const id = setTimeout(() => setToast(null), 4000)
    return () => clearTimeout(id)
  }, [toast])

  useEffect(() => {
    let hasActiveTimer = false
    liveState.live.forEach(p => {
      if (p.phase === 'converting' && p.recordingStartedAt) hasActiveTimer = true
    })
    const hasRecentProgress = Array.from(progressAtRef.current.values()).some(
      at => Date.now() - at < PROGRESS_ACTIVE_WINDOW_MS,
    )
    if (!hasActiveTimer && !hasRecentProgress) return

    const id = setInterval(() => setNowTick(Date.now()), 1000)
    return () => clearInterval(id)
  }, [liveState, nowTick])

  const startTask = async (cell_task: string, retryFailed = false) => {
    setStarting(cell_task)
    try {
      const cell = taskCell(cell_task)
      const job = retryFailed
        ? (await retryFailedConvertJob(cell_task)).job
        : await enqueueConvertJob({ cell, cell_task })
      setQueued(prev => {
        if (prev.has(cell_task)) return prev
        const next = new Set(prev)
        next.add(cell_task)
        return next
      })
      setToast({ kind: 'ok', text: `대기열에 추가됨 #${job.id}` })
      onRefresh()
    } catch (err) {
      const message = err instanceof Error ? err.message : 'enqueue failed'
      console.error('enqueueConvertJob failed:', err)
      setToast({ kind: 'err', text: message })
    } finally {
      setStarting(null)
    }
  }

  const runValidation = async (cell_task: string, mode: ValidationMode) => {
    const key = `${cell_task}:${mode}`
    setRunningValidation(prev => {
      const next = new Set(prev)
      next.add(key)
      return next
    })
    try {
      const res = await fetch(`${API}/validate/${mode}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cell_task }),
      })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        console.error(`validate(${mode}) failed:`, body)
      }
      onRefresh()
    } finally {
      setRunningValidation(prev => {
        const next = new Set(prev)
        next.delete(key)
        return next
      })
    }
  }

  if (tasks.length === 0) {
    return <div className="cvp-empty">No conversion data</div>
  }

  const totals = tasks.reduce(
    (acc, t) => ({
      total: acc.total + t.total,
      done: acc.done + t.done,
      pending: acc.pending + t.pending,
      failed: acc.failed + t.failed,
    }),
    { total: 0, done: 0, pending: 0, failed: 0 },
  )
  const hasAnyConverting = Array.from(liveState.live.values()).some(
    p => p.phase === 'converting',
  )
  const lastEvent = events[events.length - 1]
  const isScanning = containerState === 'running'
    && !hasAnyConverting
    && !!lastEvent
    && lastEvent.type === 'scan'

  const overallPct = totals.total > 0 ? Math.round((totals.done / totals.total) * 100) : 0
  const canStart = taskStartAvailable && starting === null

  const canValidate = dockerAvailable
    && containerState !== 'running'
    && containerState !== 'building'
    && starting === null

  return (
    <div className="cvp-root">
      {!dockerAvailable && (
        <div className="cvp-inline-note" role="note">
          {CONVERTER_HOST_CONTROL_HINT}
        </div>
      )}
      {toast && (
        <div
          className={`cvp-toast cvp-toast-${toast.kind}`}
          role="status"
          aria-live="polite"
        >
          {toast.text}
        </div>
      )}
      <div className="cvp-hero">
        <div className="cvp-hero-left">
          <span className="cvp-pct">{overallPct}</span>
          <span className="cvp-pct-unit">%</span>
        </div>
        <div className="cvp-hero-right">
          <div className="cvp-bar-wide">
            <div className="cvp-bar-fill" style={{ width: `${overallPct}%` }} />
          </div>
          <div className="cvp-pills">
            <span className="cvp-pill cvp-pill-green">
              <span className="cvp-pill-num" style={{ fontFamily: 'var(--font-mono)' }}>{totals.done}</span>
              <span className="cvp-pill-label">done</span>
            </span>
            <span className="cvp-pill cvp-pill-yellow">
              <span className="cvp-pill-num" style={{ fontFamily: 'var(--font-mono)' }}>{totals.pending}</span>
              <span className="cvp-pill-label">pending</span>
            </span>
            {totals.failed > 0 && (
              <span className="cvp-pill cvp-pill-red">
                <span className="cvp-pill-num" style={{ fontFamily: 'var(--font-mono)' }}>{totals.failed}</span>
                <span className="cvp-pill-label">failed</span>
              </span>
            )}
          </div>
          {isScanning && (
            <span className="cvp-pill cvp-pill-scanning" role="status" aria-live="polite">
              <span className="cvp-pill-label">Scanning…</span>
            </span>
          )}
        </div>
      </div>

      <div className="cvp-cards">
        {tasks.map(t => {
          const payload: TaskLivePayload | undefined = liveState.live.get(t.cell_task)
          const phase = payload?.phase
          // Status-API `done` lags real progress because it counts only
          // recordings already flushed to parquet. During active conversion,
          // use the live `recording_start` event's index to project a
          // virtual `done` so the fraction and bar don't appear frozen.
          let liveDone = t.done
          if (phase === 'converting'
            && payload?.recordingIndex !== undefined
            && payload?.recordingTotal !== undefined
            && payload.recordingTotal > 0
            && t.total > 0
          ) {
            const batchBaseline = Math.max(0, t.total - payload.recordingTotal)
            const projected = batchBaseline + Math.max(0, payload.recordingIndex - 1)
            liveDone = Math.min(t.total, Math.max(t.done, projected))
          }
          const pct = t.total > 0 ? Math.round((liveDone / t.total) * 100) : 0
          const hasPending = t.pending > 0
          const hasRetry = t.retry > 0
          const hasFailed = t.failed > 0
          const hasWork = hasPending || hasRetry || hasFailed

          const isLiveActive = phase === 'converting' || phase === 'finalizing'
          const isHostActive = activeCellTask === t.cell_task
          const isProgressingNow = isHostActive
            || isProgressActive(t.cell_task)
            || isRecentIso(t.last_updated, PROGRESS_ACTIVE_WINDOW_MS)
          const isActive = isLiveActive || isProgressingNow
          const isFailureFlashing = !!payload?.failureFlashUntil
            && payload.failureFlashUntil > nowTick

          const cardClass = 'cvp-card'
            + (isActive ? ' is-live' : '')
            + (isFailureFlashing ? ' is-failure-flash' : '')

          const barClass = phase === 'finalizing' ? 'cvp-card-bar is-finalizing' : 'cvp-card-bar'
          const fillClass = phase === 'converting'
            ? 'cvp-card-bar-fill is-converting'
            : 'cvp-card-bar-fill'
          const fillWidth = phase === 'finalizing' || phase === 'done' ? '100%' : `${pct}%`

          const ghostLeft = phase === 'converting' && t.total > 0 ? pct : 0
          const ghostWidth = phase === 'converting' && t.total > 0
            ? Math.round((1 / t.total) * 100)
            : 0

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
          } else if (isProgressingNow) {
            liveLine = { label: 'Converting…' }
          }

          const isStartingThis = starting === t.cell_task
          const isQueuedThis = queued.has(t.cell_task) && !isProgressingNow
          const retryFailed = hasFailed
          const buttonDisabled = !canStart || !hasWork || isActive || isStartingThis || isQueuedThis
          const buttonLabel = isStartingThis
            ? 'Starting…'
            : isQueuedThis
              ? 'Queued'
              : isActive
                ? 'Running'
                : retryFailed
                  ? 'Retry failed'
                  : hasRetry
                    ? 'Retry'
                    : 'Convert'

          const validateDisabled = !canValidate
          const quick = t.validation.quick
          const full = t.validation.full
          const isQuickRunning = runningValidation.has(`${t.cell_task}:quick`)
          const isFullRunning = runningValidation.has(`${t.cell_task}:full`)

          return (
            <div key={t.cell_task} className={cardClass}>
              <div className="cvp-card-header">
                <span className="cvp-card-cell">{taskCell(t.cell_task)}</span>
                <span className="cvp-card-name">{taskLabel(t.cell_task)}</span>
                <span className="cvp-card-fraction" style={{ fontFamily: 'var(--font-mono)' }}>
                  {liveDone}/{t.total}
                </span>
              </div>
              <div className={barClass}>
                <div className={fillClass} style={{ width: fillWidth }} />
                {ghostWidth > 0 && (
                  <div
                    className="cvp-card-bar-ghost"
                    style={{
                      left: `${ghostLeft}%`,
                      width: `${ghostWidth}%`,
                    }}
                  />
                )}
              </div>
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
                  {!liveLine && (t.failed > 0 || t.retry > 0) && (
                    <div className="cvp-card-failed">
                      {t.failed > 0 && `${t.failed} failed`}
                      {t.failed > 0 && t.retry > 0 && ' · '}
                      {t.retry > 0 && `${t.retry} retry ready`}
                    </div>
                  )}
                </div>
                <button
                  type="button"
                  className="btn-secondary cvp-card-convert"
                  disabled={buttonDisabled}
                  title={getTaskConvertTitle({
                    dockerAvailable,
                    taskStartAvailable,
                    hasWork,
                  })}
                  onClick={() => startTask(t.cell_task, retryFailed)}
                >
                  {buttonLabel}
                </button>
              </div>

              <div className="cvp-card-validation-row">
                <div className="cvp-card-validation">
                  <div className="cvp-card-val-meta">
                    <span className="cvp-card-val-title">Quick</span>
                    <span className={`cvp-card-val-badge ${VALIDATION_STATUS_CLASS[quick.status] ?? 'cvp-val-not-run'}`}>
                      {quick.status}
                    </span>
                  </div>
                  <div className="cvp-card-val-summary">{quick.summary}</div>
                  <button
                    type="button"
                    className="btn-secondary cvp-card-validate"
                    disabled={validateDisabled || isQuickRunning}
                    title={getValidationTitle({
                      actionLabel: 'Quick Check',
                      dockerAvailable,
                      canValidate,
                    })}
                    onClick={() => runValidation(t.cell_task, 'quick')}
                  >
                    {isQuickRunning ? 'Checking...' : 'Quick Check'}
                  </button>
                </div>

                <div className="cvp-card-validation">
                  <div className="cvp-card-val-meta">
                    <span className="cvp-card-val-title">Full</span>
                    <span className={`cvp-card-val-badge ${VALIDATION_STATUS_CLASS[full.status] ?? 'cvp-val-not-run'}`}>
                      {full.status}
                    </span>
                  </div>
                  <div className="cvp-card-val-summary">{full.summary}</div>
                  <button
                    type="button"
                    className="btn-secondary cvp-card-validate"
                    disabled={validateDisabled || isFullRunning}
                    title={getValidationTitle({
                      actionLabel: 'Full Check',
                      dockerAvailable,
                      canValidate,
                    })}
                    onClick={() => runValidation(t.cell_task, 'full')}
                  >
                    {isFullRunning ? 'Checking...' : 'Full Check'}
                  </button>
                </div>
              </div>
              <ConverterTaskRawRecordings task={t.cell_task} />
            </div>
          )
        })}
      </div>
    </div>
  )
}
