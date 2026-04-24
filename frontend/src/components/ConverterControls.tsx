import { useState } from 'react'
import type { ConverterState } from '../types'
import {
  CONVERTER_HOST_CONTROL_HINT,
  getHostStopTitle,
} from './converterUx'

interface Props {
  containerState: ConverterState
  dockerAvailable: boolean
  hostStopAvailable: boolean
  onRefresh: () => void
}

const API = '/api/converter'
const HOST_RUNBOOK = 'Build/start from the host with main.sh, leave the converter running, then click Convert on a task.'

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

export function ConverterControls({
  containerState,
  dockerAvailable,
  hostStopAvailable,
  onRefresh,
}: Props) {
  const [loading, setLoading] = useState<string | null>(null)

  const requestStop = async () => {
    setLoading('stop')
    try {
      const res = await fetch(`${API}/stop`, { method: 'POST' })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        console.error('stop failed:', body)
      }
      onRefresh()
    } finally {
      setLoading(null)
    }
  }

  const stopDisabled = loading !== null || !hostStopAvailable
  const stopTitle = getHostStopTitle({
    hostStopAvailable,
    busy: loading !== null,
  })
  const statusNote = dockerAvailable
    ? HOST_RUNBOOK
    : CONVERTER_HOST_CONTROL_HINT

  return (
    <div className="converter-controls">
      <div className="converter-host-lifecycle" role="note">
        <span className="converter-host-lifecycle-title">Host-managed converter</span>
        <span>{statusNote}</span>
      </div>
      <div className="converter-controls-buttons">
        <button
          className="btn-secondary converter-stop-btn"
          disabled={stopDisabled}
          title={stopTitle}
          onClick={requestStop}
        >
          {loading === 'stop' ? 'Requesting...' : 'Request Stop'}
        </button>
      </div>
      <div className={`converter-status-badge ${STATE_CLASS[containerState]}`}>
        <span className="converter-status-dot" />
        {STATE_LABEL[containerState]}
      </div>
    </div>
  )
}
