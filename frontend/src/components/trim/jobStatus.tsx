import { useCallback, useEffect, useRef, useState } from 'react'
import client from '../../api/client'
import { trimStyles as s } from './styles'

type SyncSummary = {
  mode: string
  created: number
  skipped_duplicates: number
}

export interface JobStatus {
  job_id: string
  operation: string
  status: string
  created_at: string
  completed_at: string | null
  error: string | null
  result_path: string | null
  summary?: SyncSummary | null
}

export function isCompleteStatus(status: string | undefined): boolean {
  return status === 'complete' || status === 'completed'
}

function isTerminalStatus(status: string): boolean {
  return isCompleteStatus(status) || status === 'failed' || status === 'cancelled'
}

function statusColor(status: string): string {
  if (isCompleteStatus(status)) {
    return 'var(--c-green)'
  }
  if (status === 'failed') {
    return 'var(--c-red)'
  }
  return 'var(--interactive)'
}

function statusTextColor(status: string): string {
  if (isCompleteStatus(status)) {
    return 'var(--c-green)'
  }
  if (status === 'failed') {
    return 'var(--c-red)'
  }
  return 'var(--text-muted)'
}

export function useJobPoller() {
  const [jobStatus, setJobStatus] = useState<JobStatus | null>(null)
  const [polling, setPolling] = useState(false)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const generationRef = useRef(0)

  const clearPollingTimer = useCallback(() => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current)
      timerRef.current = null
    }
  }, [])

  const startPolling = useCallback((jobId: string) => {
    clearPollingTimer()
    generationRef.current += 1
    const generation = generationRef.current
    setPolling(true)
    setJobStatus(null)

    const poll = async () => {
      try {
        const resp = await client.get<JobStatus>(`/datasets/ops/status/${jobId}`)
        if (generation !== generationRef.current) return
        const s = resp.data
        setJobStatus(s)
        if (isTerminalStatus(s.status)) {
          clearPollingTimer()
          setPolling(false)
          return
        }
        timerRef.current = setTimeout(poll, 1000)
      } catch {
        if (generation !== generationRef.current) return
        clearPollingTimer()
        setPolling(false)
      }
    }

    timerRef.current = setTimeout(poll, 1000)
  }, [clearPollingTimer])

  const reset = useCallback(() => {
    generationRef.current += 1
    clearPollingTimer()
    setJobStatus(null)
    setPolling(false)
  }, [clearPollingTimer])

  useEffect(() => () => {
    generationRef.current += 1
    clearPollingTimer()
  }, [clearPollingTimer])

  return { jobStatus, polling, startPolling, reset }
}

export function JobProgress({ jobStatus, polling }: { jobStatus: JobStatus | null; polling: boolean }) {
  if (!jobStatus && !polling) return null

  if (polling && !jobStatus) {
    return <div style={s.statusBox}>Running...</div>
  }

  if (!jobStatus) return null

  const isOk = isCompleteStatus(jobStatus.status)
  const isFail = jobStatus.status === 'failed'
  const borderColor = statusColor(jobStatus.status)
  const textColor = statusTextColor(jobStatus.status)

  return (
    <div style={{ ...s.statusBox, borderColor }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <span style={{ color: textColor, fontSize: 12, fontWeight: 600 }}>
          {jobStatus.status.toUpperCase()}
        </span>
        {polling && <span style={s.spinner}>⟳</span>}
      </div>
      {isOk && jobStatus.result_path && (
        <div style={s.resultPath}>
          Result: <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--c-green)' }}>{jobStatus.result_path}</span>
        </div>
      )}
      {isFail && jobStatus.error && (
        <div style={s.errorText}>{jobStatus.error}</div>
      )}
    </div>
  )
}
