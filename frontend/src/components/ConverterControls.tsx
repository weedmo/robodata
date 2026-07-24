import type { ConverterState, DockerServiceStatus } from '../types'
import { getConverterModeHint } from './converterUx'
import { WorkerControlPill } from './WorkerControlPill'
import type { ConvertJobProgress } from '../api/converter'
import { useConverterJobLifecycle } from '../hooks/useConverterJobLifecycle'
// Convert button enqueues via /api/jobs (see frontend/src/api/converter.ts).
// Re-exported so the rest of the app can pull it from this barrel without
// reaching into the api/ directory directly. Per-task Convert buttons in
// ConverterProgress.tsx already call enqueueConvertJob() to post jobs.
// Job polling/cancel coordination moved to useConverterJobLifecycle
// (fetchConvertJob and the /api/jobs/ cancel endpoint stay behind that hook).
export { enqueueConvertJob } from '../api/converter'

interface Props {
  containerState: ConverterState
  dockerAvailable: boolean
  hostStopAvailable: boolean
  taskStartAvailable: boolean
  taskStartUnavailableReason: string | null
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

function taskProgressLabel(progress: ConvertJobProgress, task: string): string {
  if (progress.task_index && progress.task_total) {
    return `${task} (${progress.task_index}/${progress.task_total})`
  }
  return task
}

function recordingProgressLabel(progress: ConvertJobProgress, recording: string): string {
  const shortName = shortRecording(recording)
  if (progress.recording_index && progress.recording_total) {
    return `${shortName} (${progress.recording_index}/${progress.recording_total})`
  }
  return shortName
}

function progressLabel(progress: ConvertJobProgress | null): string | null {
  if (!progress) return null
  const phase = progress.phase ?? 'running'
  const task = progress.cell_task ?? null
  const recording = progress.recording ?? null
  const taskPart = task ? taskProgressLabel(progress, task) : null

  if (phase === 'scanning') return '스캔 중'
  if (phase === 'finalizing' && task) return `마무리 중: ${task}`
  if (phase === 'complete') return '전체 변환 완료'
  if (recording) {
    const recPart = recordingProgressLabel(progress, recording)
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
  taskStartAvailable,
  taskStartUnavailableReason,
  dockerServices,
  onRefresh,
}: Props) {
  const {
    currentJobId,
    cancelling,
    autoStarting,
    notice,
    jobProgress,
    setCurrentJobId,
    cancelCurrent,
    queueAutoScan,
  } = useConverterJobLifecycle({
    autoScanDedupeKey: AUTO_SCAN_DEDUPE_KEY,
    pollMs: JOB_PROGRESS_POLL_MS,
    onRefresh,
  })

  // Container lifecycle (start/stop/build) is host-managed, so the previous
  // legacy Stop button has been removed in favor of API-only worker control.
  // Surface only the canonical host-mode hint; props remain for compatibility
  // with the parent page until ConverterPage is refactored.
  void dockerAvailable
  void hostStopAvailable
  const converterModeHint = getConverterModeHint(taskStartUnavailableReason)

  return (
    <div className="converter-controls">
      <div className="converter-host-lifecycle" role="note">
        <span className="converter-host-lifecycle-title">
          {taskStartUnavailableReason ? 'Conversion disabled' : 'Host-managed converter'}
        </span>
        <span>{converterModeHint}</span>
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
          disabled={autoStarting || !taskStartAvailable}
          title={
            taskStartAvailable
              ? '전체 pending task를 스캔해서 변환 작업을 대기열에 추가합니다.'
              : '운영자 복구 안전잠금으로 변환 작업이 비활성화되어 있습니다.'
          }
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
