import { useEffect, useRef, useState } from 'react'
import type { ConverterState, ConverterTaskProgress, LogEvent } from '../types'

type TaskLive = 'converting' | 'finalizing' | 'done'

type ValidationMode = 'quick' | 'full'

const API = '/api/converter'

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

function applyTaskLiveEvent(
  live: Map<string, TaskLive>,
  ev: LogEvent,
  convertingTask: string | null,
): string | null {
  if (!ev.task) return convertingTask
  const currentLive = live.get(ev.task)
  if (ev.type === 'converting') {
    if (convertingTask && convertingTask !== ev.task && live.get(convertingTask) === 'converting') {
      live.delete(convertingTask)
    }
    live.set(ev.task, 'converting')
    return ev.task
  }
  if (ev.type === 'finalizing') {
    if (currentLive === 'done') {
      return convertingTask
    }
    live.set(ev.task, 'finalizing')
    return convertingTask === ev.task ? null : convertingTask
  }
  if (ev.type === 'finalized') {
    live.set(ev.task, 'done')
    return convertingTask === ev.task ? null : convertingTask
  }
  return convertingTask
}

function sameTaskLive(a: Map<string, TaskLive>, b: Map<string, TaskLive>) {
  if (a.size !== b.size) return false
  for (const [key, value] of a) {
    if (b.get(key) !== value) return false
  }
  return true
}

export function ConverterProgress({
  tasks,
  containerState,
  dockerAvailable,
  events,
  onRefresh,
}: Props) {
  const [starting, setStarting] = useState<string | null>(null)
  const [runningValidation, setRunningValidation] = useState<Set<string>>(new Set())
  const convertingTaskRef = useRef<string | null>(null)
  const [taskLive, setTaskLive] = useState<Map<string, TaskLive>>(new Map())

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

  const startTask = async (cell_task: string) => {
    setStarting(cell_task)
    try {
      const res = await fetch(`${API}/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cell_task }),
      })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        console.error('start(task) failed:', body)
      }
      onRefresh()
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

  const overallPct = totals.total > 0 ? Math.round((totals.done / totals.total) * 100) : 0
  const canStart = dockerAvailable
    && containerState !== 'running'
    && containerState !== 'building'
    && starting === null

  const canValidate = canStart

  return (
    <div className="cvp-root">
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
        </div>
      </div>

      <div className="cvp-cards">
        {tasks.map(t => {
          const pct = t.total > 0 ? Math.round((t.done / t.total) * 100) : 0
          const hasPending = t.pending > 0
          const disabled = !canStart || !hasPending
          const validateDisabled = !canValidate
          const isStartingThis = starting === t.cell_task
          const quick = t.validation.quick
          const full = t.validation.full
          const isQuickRunning = runningValidation.has(`${t.cell_task}:quick`)
          const isFullRunning = runningValidation.has(`${t.cell_task}:full`)
          const lifecycleLive = taskLive.get(t.cell_task)
          const live = lifecycleLive === 'finalizing'
            ? 'finalizing'
            : hasPending && lifecycleLive === 'converting'
              ? 'converting'
              : t.done === t.total
                ? 'done'
                : undefined
          const barClass = live === 'finalizing' ? 'cvp-card-bar is-finalizing' : 'cvp-card-bar'
          const fillClass = live === 'converting'
            ? 'cvp-card-bar-fill is-converting'
            : 'cvp-card-bar-fill'
          const fillWidth = live === 'finalizing' || live === 'done' ? '100%' : `${pct}%`

          return (
            <div key={t.cell_task} className="cvp-card">
              <div className="cvp-card-header">
                <span className="cvp-card-cell">{taskCell(t.cell_task)}</span>
                <span className="cvp-card-name">{taskLabel(t.cell_task)}</span>
                <span className="cvp-card-fraction" style={{ fontFamily: 'var(--font-mono)' }}>
                  {t.done}/{t.total}
                </span>
              </div>
              <div className={barClass}>
                <div className={fillClass} style={{ width: fillWidth }} />
              </div>
              <div className="cvp-card-footer">
                <div className="cvp-card-footer-left">
                  {live === 'converting' && (
                    <span className="cvp-status-badge st-converting" role="status" aria-live="polite">
                      <span className="dot" />
                      Converting
                    </span>
                  )}
                  {live === 'finalizing' && (
                    <span className="cvp-status-badge st-finalizing" role="status" aria-live="polite">
                      <span className="dot" />
                      Finalizing
                    </span>
                  )}
                  {live === 'done' && (
                    <span className="cvp-status-badge st-done" role="status" aria-live="polite">
                      <span className="dot" />
                      Done
                    </span>
                  )}
                  {!live && t.failed > 0 && (
                    <div className="cvp-card-failed">{t.failed} failed</div>
                  )}
                </div>
                <button
                  type="button"
                  className="btn-secondary cvp-card-convert"
                  disabled={disabled}
                  onClick={() => startTask(t.cell_task)}
                >
                  {isStartingThis ? 'Starting...' : 'Convert'}
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
                    onClick={() => runValidation(t.cell_task, 'full')}
                  >
                    {isFullRunning ? 'Checking...' : 'Full Check'}
                  </button>
                </div>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
