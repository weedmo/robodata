import { useCallback, useEffect, useState } from 'react'
import {
  cancelConvertJob,
  enqueueConvertJob,
  fetchConvertJob,
  type ConvertJobProgress,
} from '../api/converter'

type Notice = { kind: 'ok' | 'err'; text: string } | null

interface UseConverterJobLifecycleOptions {
  autoScanDedupeKey: string
  pollMs: number
  onRefresh: () => void
}

interface UseConverterJobLifecycleResult {
  currentJobId: number | null
  cancelling: boolean
  autoStarting: boolean
  notice: Notice
  jobProgress: ConvertJobProgress | null
  setCurrentJobId: (jobId: number | null) => void
  cancelCurrent: () => Promise<void>
  queueAutoScan: () => Promise<void>
}

function isActiveJobStatus(status: string): boolean {
  return status === 'running' || status === 'cancel_requested'
}

export function useConverterJobLifecycle({
  autoScanDedupeKey,
  pollMs,
  onRefresh,
}: UseConverterJobLifecycleOptions): UseConverterJobLifecycleResult {
  const [currentJobId, setCurrentJobId] = useState<number | null>(null)
  const [cancelling, setCancelling] = useState(false)
  const [autoStarting, setAutoStarting] = useState(false)
  const [notice, setNotice] = useState<Notice>(null)
  const [jobProgress, setJobProgress] = useState<ConvertJobProgress | null>(null)

  const cancelCurrent = useCallback(async () => {
    if (currentJobId == null) return
    setCancelling(true)
    try {
      await cancelConvertJob(currentJobId)
    } catch (err) {
      console.error('cancel failed:', err)
    } finally {
      setCancelling(false)
    }
  }, [currentJobId])

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
        if (!isActiveJobStatus(job.status)) {
          setCurrentJobId(null)
        }
      } catch {
        if (alive) setJobProgress(null)
      }
    }
    void tick()
    const id = setInterval(tick, pollMs)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [currentJobId, pollMs])

  const queueAutoScan = useCallback(async () => {
    setAutoStarting(true)
    try {
      const job = await enqueueConvertJob({}, autoScanDedupeKey)
      setNotice({ kind: 'ok', text: `전체 자동 변환 대기열 추가 #${job.id}` })
      onRefresh()
    } catch (err) {
      const message = err instanceof Error ? err.message : 'enqueue failed'
      setNotice({ kind: 'err', text: message })
    } finally {
      setAutoStarting(false)
    }
  }, [autoScanDedupeKey, onRefresh])

  return {
    currentJobId,
    cancelling,
    autoStarting,
    notice,
    jobProgress,
    setCurrentJobId,
    cancelCurrent,
    queueAutoScan,
  }
}
