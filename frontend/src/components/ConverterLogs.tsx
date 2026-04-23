import { useEffect, useRef, useState } from 'react'
import type { ConverterState, LogEvent } from '../types'

interface Props {
  containerState: ConverterState
  events: LogEvent[]
  open: boolean
  onToggle: () => void
}

function formatTime(ts: string) {
  return ts.split(' ')[1] || ts
}

function formatDuration(sec: number) {
  if (sec < 60) return `${sec.toFixed(1)}s`
  const m = Math.floor(sec / 60)
  const s = (sec % 60).toFixed(0)
  return `${m}m${s}s`
}

function recordingName(recording: string) {
  const parts = recording.split('/')
  return parts[parts.length - 1]
}

function recordingTask(recording: string) {
  const parts = recording.split('/')
  return parts.slice(0, -1).join('/')
}

function eventTeaser(event: LogEvent): string {
  switch (event.type) {
    case 'converted':
      return `Converted ${recordingName(event.recording!)}`
    case 'failed':
      return `Failed ${recordingName(event.recording!)}${event.reason ? ` — ${event.reason}` : ''}`
    case 'converting':
      return `Processing ${event.task}`
    case 'finalizing':
      return `Finalizing ${event.task}`
    case 'finalized':
      return `Finalized ${event.task}`
    case 'scan':
      return `Scanned ${event.tasks} tasks, ${event.pending} pending`
    case 'warning':
      return event.message ?? 'Warning'
    case 'error':
      return event.message ?? 'Error'
    default:
      return event.message ?? ''
  }
}

function EventRow({ event }: { event: LogEvent }) {
  const time = (
    <span className="log-time" style={{ fontFamily: 'var(--font-mono)' }}>
      {formatTime(event.ts)}
    </span>
  )

  switch (event.type) {
    case 'converted':
      return (
        <div className="log-event log-converted">
          {time}
          <span className="log-badge log-badge-ok">OK</span>
          <span className="log-task">{recordingTask(event.recording!)}</span>
          <span className="log-recording" style={{ fontFamily: 'var(--font-mono)' }}>
            {recordingName(event.recording!)}
          </span>
          <span className="log-meta">
            {event.frames} frames
            {typeof event.duration === 'number' ? ` · ${formatDuration(event.duration)}` : ''}
          </span>
        </div>
      )

    case 'failed':
      return (
        <div className="log-event log-failed">
          {time}
          <span className="log-badge log-badge-fail" style={{ fontFamily: 'var(--font-mono)' }}>
            {event.error_code}
          </span>
          <span className="log-task">{recordingTask(event.recording!)}</span>
          <span className="log-recording" style={{ fontFamily: 'var(--font-mono)' }}>
            {recordingName(event.recording!)}
          </span>
          <span className="log-reason">{event.reason}</span>
        </div>
      )

    case 'converting':
      return (
        <div className="log-event log-converting">
          {time}
          <span className="log-badge log-badge-active">START</span>
          <span className="log-task">{event.task}</span>
        </div>
      )

    case 'finalizing':
      return (
        <div className="log-event log-finalizing">
          {time}
          <span className="log-badge log-badge-finalizing">FIN</span>
          <span className="log-task">{event.task}</span>
        </div>
      )

    case 'finalized':
      return (
        <div className="log-event log-finalized">
          {time}
          <span className="log-badge log-badge-ok">OK</span>
          <span className="log-task">{event.task}</span>
        </div>
      )

    case 'scan':
      return (
        <div className="log-event log-scan">
          {time}
          <span className="log-badge log-badge-scan">SCAN</span>
          <span className="log-meta">{event.tasks} tasks · {event.pending} pending</span>
        </div>
      )

    case 'warning':
      return (
        <div className="log-event log-warn">
          {time}
          <span className="log-badge log-badge-warn">WARN</span>
          <span className="log-message">{event.message}</span>
        </div>
      )

    case 'error':
      return (
        <div className="log-event log-error">
          {time}
          <span className="log-badge log-badge-fail">ERR</span>
          <span className="log-message">{event.message}</span>
        </div>
      )

    default:
      return (
        <div className="log-event log-info">
          {time}
          <span className="log-message">{event.message}</span>
        </div>
      )
  }
}

export function ConverterLogs({ containerState, events, open, onToggle }: Props) {
  const [autoScroll, setAutoScroll] = useState(true)
  const bottomRef = useRef<HTMLDivElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (autoScroll && open) {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [events, autoScroll, open])

  const handleScroll = () => {
    const el = containerRef.current
    if (!el) return
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40
    setAutoScroll(atBottom)
  }

  const counts = events.reduce(
    (acc, e) => {
      if (e.type === 'converted') acc.ok++
      else if (e.type === 'failed') acc.fail++
      return acc
    },
    { ok: 0, fail: 0 },
  )

  const lastEvent = events[events.length - 1]

  return (
    <div className={`cvl-wrapper${open ? ' cvl-open' : ''}`}>
      <button className="cvl-toggle" onClick={onToggle}>
        <span className="cvl-toggle-label">Activity</span>
        {events.length > 0 && (
          <span className="cvl-toggle-counts">
            {counts.ok > 0 && (
              <span className="cvl-count cvl-count-green">{counts.ok}</span>
            )}
            {counts.fail > 0 && (
              <span className="cvl-count cvl-count-red">{counts.fail}</span>
            )}
          </span>
        )}
        {!open && lastEvent && (
          <span className="cvl-teaser">{eventTeaser(lastEvent)}</span>
        )}
        <span className="cvl-chevron">{open ? '▾' : '▸'}</span>
      </button>

      {open && (
        <div className="cvl-panel">
          {!autoScroll && (
            <button
              className="cvl-scroll-btn"
              onClick={() => {
                setAutoScroll(true)
                bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
              }}
            >
              Scroll to bottom
            </button>
          )}
          <div
            className="cvl-feed"
            ref={containerRef}
            onScroll={handleScroll}
          >
            {events.length === 0 ? (
              <div className="cvl-empty">
                {containerState === 'running' ? 'Connecting...' : 'Start converter to see activity'}
              </div>
            ) : (
              events.map((event, i) => <EventRow key={i} event={event} />)
            )}
            <div ref={bottomRef} />
          </div>
        </div>
      )}
    </div>
  )
}
