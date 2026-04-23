import { useState } from 'react'
import type { ConverterState } from '../types'
import { CONVERTER_HOST_CONTROL_HINT, getConverterActionTitle } from './converterUx'

interface Props {
  containerState: ConverterState
  dockerAvailable: boolean
  onRefresh: () => void
}

const API = '/api/converter'

const STATE_LABEL: Record<ConverterState, string> = {
  running: 'Running',
  stopped: 'Stopped',
  building: 'Building',
  error: 'Error',
  unknown: 'Unknown',
}

const STATE_CLASS: Record<ConverterState, string> = {
  running: 'converter-status-running',
  stopped: 'converter-status-stopped',
  building: 'converter-status-building',
  error: 'converter-status-error',
  unknown: 'converter-status-stopped',
}

export function ConverterControls({ containerState, dockerAvailable, onRefresh }: Props) {
  const [loading, setLoading] = useState<string | null>(null)

  const act = async (action: 'build' | 'start' | 'stop') => {
    setLoading(action)
    try {
      const res = await fetch(`${API}/${action}`, { method: 'POST' })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        console.error(`${action} failed:`, body)
      }
      onRefresh()
    } finally {
      setLoading(null)
    }
  }

  const disabled = !dockerAvailable || loading !== null
  const isRunning = containerState === 'running'
  const isBuilding = containerState === 'building'

  return (
    <div className="converter-controls">
      <div className="converter-controls-buttons">
        <button
          className="btn-secondary"
          disabled={disabled || isRunning || isBuilding}
          title={getConverterActionTitle('Build', {
            dockerAvailable,
            busy: loading !== null || isRunning || isBuilding,
          })}
          onClick={() => act('build')}
        >
          {loading === 'build' ? 'Building...' : 'Build'}
        </button>
        <button
          className="btn-primary"
          disabled={disabled || isRunning || isBuilding}
          title={getConverterActionTitle('Start', {
            dockerAvailable,
            busy: loading !== null || isRunning || isBuilding,
          })}
          onClick={() => act('start')}
        >
          {loading === 'start' ? 'Starting...' : 'Start'}
        </button>
        <button
          className="btn-secondary converter-stop-btn"
          disabled={disabled || (!isRunning && !isBuilding)}
          title={getConverterActionTitle('Stop', {
            dockerAvailable,
            busy: loading !== null,
          })}
          onClick={() => act('stop')}
        >
          {loading === 'stop' ? 'Stopping...' : 'Stop'}
        </button>
      </div>
      <div className={`converter-status-badge ${STATE_CLASS[containerState]}`}>
        <span className="converter-status-dot" />
        {STATE_LABEL[containerState]}
      </div>
      {!dockerAvailable && (
        <span className="converter-docker-warn" role="note">{CONVERTER_HOST_CONTROL_HINT}</span>
      )}
    </div>
  )
}
