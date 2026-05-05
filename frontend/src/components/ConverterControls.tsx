import { useState } from 'react'
import type { ConverterState, DockerServiceStatus } from '../types'
import { CONVERTER_HOST_CONTROL_HINT } from './converterUx'
import { WorkerControlPill } from './WorkerControlPill'
// Convert button enqueues via /api/jobs (see frontend/src/api/converter.ts).
// Re-exported so the rest of the app can pull it from this barrel without
// reaching into the api/ directory directly. Per-task Convert buttons in
// ConverterProgress.tsx already call enqueueConvertJob() to post jobs.
export { enqueueConvertJob } from '../api/converter'

interface Props {
  containerState: ConverterState
  dockerAvailable: boolean
  hostStopAvailable: boolean
  dockerServices: DockerServiceStatus[]
  onRefresh: () => void
}

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

const WORKER_ID = 'converter'

export function ConverterControls({
  containerState,
  dockerAvailable,
  hostStopAvailable,
  dockerServices,
  onRefresh,
}: Props) {
  const [currentJobId, setCurrentJobId] = useState<number | null>(null)
  const [cancelling, setCancelling] = useState(false)

  // Container lifecycle (start/stop/build) is host-managed, so the previous
  // legacy Stop button has been removed in favor of API-only worker control.
  // Surface only the canonical host-mode hint; props remain for compatibility
  // with the parent page until ConverterPage is refactored.
  void dockerAvailable
  void hostStopAvailable
  void onRefresh

  const cancelCurrent = async () => {
    if (currentJobId == null) return
    setCancelling(true)
    try {
      const res = await fetch(`/api/jobs/${currentJobId}/cancel`, {
        method: 'POST',
      })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        console.error('cancel failed:', body)
      }
    } finally {
      setCancelling(false)
    }
  }

  return (
    <div className="converter-controls">
      <div className="converter-host-lifecycle" role="note">
        <span className="converter-host-lifecycle-title">Host-managed converter</span>
        <span>{CONVERTER_HOST_CONTROL_HINT}</span>
      </div>
      {dockerServices.length > 0 && (
        <div className="docker-service-strip" aria-label="Docker service status">
          {dockerServices.map(service => (
            <span
              key={service.name}
              className={`docker-service-chip ${service.healthy ? 'is-up' : 'is-down'}`}
              title={service.status ?? service.state}
            >
              <span className="docker-service-dot" />
              {service.name}
            </span>
          ))}
        </div>
      )}
      <div className="converter-controls-buttons">
        <WorkerControlPill
          workerId={WORKER_ID}
          onInFlightJobChange={setCurrentJobId}
        />
        <button
          className="btn-secondary converter-stop-btn"
          disabled={currentJobId == null || cancelling}
          title={
            currentJobId == null
              ? '취소할 작업이 없습니다.'
              : '워커가 실행 중인 작업을 취소합니다.'
          }
          onClick={cancelCurrent}
        >
          {cancelling ? '취소 중...' : '현재 작업 취소'}
        </button>
      </div>
      <div className={`converter-status-badge ${STATE_CLASS[containerState]}`}>
        <span className="converter-status-dot" />
        {STATE_LABEL[containerState]}
      </div>
    </div>
  )
}
