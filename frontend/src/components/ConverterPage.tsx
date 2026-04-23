import { useEffect, useRef, useState } from 'react'
import { ConverterControls } from './ConverterControls'
import { ConverterProgress } from './ConverterProgress'
import { ConverterLogs } from './ConverterLogs'
import { ConverterOomBanner } from './ConverterOomBanner'
import type { ConverterStatus, LogEvent } from '../types'

interface Props {
  status: ConverterStatus
  onRefresh: () => void
}

const MAX_EVENTS = 200

export function ConverterPage({ status, onRefresh }: Props) {
  const [logsOpen, setLogsOpen] = useState(false)
  const [events, setEvents] = useState<LogEvent[]>([])
  const wsRef = useRef<WebSocket | null>(null)

  useEffect(() => {
    if (status.container_state !== 'running') return
    const id = setInterval(onRefresh, 10000)
    return () => clearInterval(id)
  }, [status.container_state, onRefresh])

  useEffect(() => {
    if (status.container_state !== 'running') {
      setEvents([])
      return
    }

    let ws: WebSocket | null = null
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null
    let attempt = 0
    let cancelled = false

    const connect = () => {
      if (cancelled) return
      setEvents([])
      const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      ws = new WebSocket(`${proto}//${window.location.host}/api/converter/logs`)
      wsRef.current = ws

      ws.onmessage = (evt) => {
        try {
          const event: LogEvent = JSON.parse(evt.data)
          setEvents(prev => {
            const next = [...prev, event]
            return next.length > MAX_EVENTS ? next.slice(-MAX_EVENTS) : next
          })
        } catch {
          // skip
        }
      }

      ws.onopen = () => { attempt = 0 }
      ws.onerror = () => {}
      ws.onclose = () => {
        if (cancelled) return
        attempt++
        const delay = Math.min(1000 * 2 ** attempt, 10000)
        reconnectTimer = setTimeout(connect, delay)
      }
    }

    connect()

    return () => {
      cancelled = true
      if (reconnectTimer) clearTimeout(reconnectTimer)
      ws?.close()
      wsRef.current = null
    }
  }, [status.container_state])

  return (
    <div className="converter-page">
      {status.oom_killed && status.container_state === 'stopped' && (
        <ConverterOomBanner finishedAt={status.finished_at} />
      )}
      <ConverterControls
        containerState={status.container_state}
        dockerAvailable={status.docker_available}
        onRefresh={onRefresh}
      />
      <div className="converter-body">
        <ConverterProgress
          tasks={status.tasks}
          containerState={status.container_state}
          dockerAvailable={status.docker_available}
          events={events}
          onRefresh={onRefresh}
        />
      </div>
      <ConverterLogs
        containerState={status.container_state}
        events={events}
        open={logsOpen}
        onToggle={() => setLogsOpen(v => !v)}
      />
    </div>
  )
}
