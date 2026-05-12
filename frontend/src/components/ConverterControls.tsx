import { useEffect, useState } from 'react'
import type { ConverterState, DockerServiceStatus } from '../types'
import { CONVERTER_HOST_CONTROL_HINT } from './converterUx'
import { WorkerControlPill } from './WorkerControlPill'
import { enqueueConvertJob, fetchConvertJob, type ConvertJobProgress } from '../api/converter'
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
const AUTO_SCAN_DEDUPE_KEY = '__converter_auto_scan__'
const JOB_PROGRESS_POLL_MS = 5000

function shortRecording(recording: string): string {
  const parts = recording.split('/')
  return parts[parts.length - 1] || recording
}

function progressLabel(progress: ConvertJobProgress | null): string | null {
  if (!progress) return null
  const phase = progress.phase ?? 'running'
  const task = progress.cell_task ?? null
  const recording = progress.recording ?? null
  const taskPart = task
    ? progress.task_index && progress.task_total
      ? `${task} (${progress.task_index}/${progress.task_total})`
      : task
    : null

  if (phase === 'scanning') return '스캔 중'
  if (phase === 'finalizing' && task) return `마무리 중: ${task}`
  if (phase === 'complete') return '전체 변환 완료'
  if (recording) {
    const recPart = progress.recording_index && progress.recording_total
      ? `${shortRecording(recording)} (${progress.recording_index}/${progress.recording_total})`
      : shortRecording(recording)
    return taskPart ? `${taskPart} · ${recPart}` : recPart
  }
  if (taskPart) {
    return progress.pending_recordings
      ? `${taskPart} · pending ${progress.pending_recordings}`
      : taskPart
  }
  if (progress.last_converted_recording) {
    return `최근 완료: ${shortRecording(progress.last_converted_recording)}`
  }
  if (progress.last_failed_recording) {
    return `최근 실패: ${shortRecording(progress.last_failed_recording)}`
  }
  return null
}

export function ConverterControls({
  containerState,
  dockerAvailable,
  hostStopAvailable,
  dockerServices,
  onRefresh,
}: Props) {
  const [currentJobId, setCurrentJobId] = useState<number | null>(null)
  const [cancelling, setCancelling] = useState(false)
  const [autoStarting, setAutoStarting] = useState(false)
  const [notice, setNotice] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)
  const [jobProgress, setJobProgress] = useState<ConvertJobProgress | null>(null)

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

  useEffect(() => {
    if (currentJobId == null) {
      setJobProgress(null)
      return
    }
    let alive = true
    const tick = async () => {
      try {
        const job = await fetchConvertJob(currentJobId)
        if (!alive) return
        setJobProgress(job.progress ?? null)
        if (job.status !== 'running' && job.status !== 'cancel_requested') {
          setCurrentJobId(null)
        }
      } catch {
        if (alive) setJobProgress(null)
      }
    }
    void tick()
    const id = setInterval(tick, JOB_PROGRESS_POLL_MS)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [currentJobId])

  const queueAutoScan = async () => {
    setAutoStarting(true)
    try {
      const job = await enqueueConvertJob({}, AUTO_SCAN_DEDUPE_KEY)
      setNotice({ kind: 'ok', text: `전체 자동 변환 대기열 추가 #${job.id}` })
      onRefresh()
    } catch (err) {
      const message = err instanceof Error ? err.message : 'enqueue failed'
      setNotice({ kind: 'err', text: message })
    } finally {
      setAutoStarting(false)
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
          className="btn-secondary converter-auto-btn"
          disabled={autoStarting}
          title="전체 pending task를 스캔해서 변환 작업을 대기열에 추가합니다."
          onClick={queueAutoScan}
        >
          {autoStarting ? '추가 중...' : '전체 자동 변환'}
        </button>
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
      {notice && (
        <div
          className={`converter-control-notice converter-control-notice-${notice.kind}`}
          role="status"
          aria-live="polite"
        >
          {notice.text}
        </div>
      )}
      {currentJobId != null && (
        <div className="converter-current-progress" role="status" aria-live="polite">
          <span className="converter-current-progress-title">현재 변환</span>
          <span className="converter-current-progress-text">
            {progressLabel(jobProgress) ?? `job #${currentJobId} 진행 중`}
          </span>
        </div>
      )}
      <div className={`converter-status-badge ${STATE_CLASS[containerState]}`}>
        <span className="converter-status-dot" />
        {STATE_LABEL[containerState]}
      </div>
    </div>
  )
}
