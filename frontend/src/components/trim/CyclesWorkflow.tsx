import { useCallback, useEffect, useRef, useState } from 'react'
import client from '../../api/client'
import { JobProgress, isCompleteStatus, useJobPoller } from './jobStatus'
import { trimStyles as s } from './styles'

interface StampStatus {
  stamped: boolean
  is_terminal_count_sample: number
}

function submitLabel(submitting: boolean, stamped: boolean | undefined): string {
  if (submitting) {
    return 'Submitting...'
  }
  if (stamped) {
    return 'Overwrite Cycle Markers'
  }
  return 'Stamp Cycles'
}

export function CyclesWorkflow({ datasetPath }: { datasetPath: string | null }) {
  const [status, setStatus] = useState<StampStatus | null>(null)
  const [statusLoading, setStatusLoading] = useState(false)
  const [statusError, setStatusError] = useState<string | null>(null)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const { jobStatus, polling, startPolling, reset } = useJobPoller()
  const datasetPathRef = useRef<string | null>(datasetPath)
  const asyncScopeRef = useRef(0)

  const refreshStatus = useCallback(async () => {
    if (!datasetPath) {
      setStatus(null)
      setStatusLoading(false)
      setStatusError(null)
      return
    }

    const scopeId = asyncScopeRef.current
    const requestPath = datasetPath
    setStatusLoading(true)
    setStatusError(null)

    try {
      const resp = await client.get<StampStatus>('/datasets/stamp-cycles/status', {
        params: { path: datasetPath },
      })
      if (scopeId !== asyncScopeRef.current || datasetPathRef.current !== requestPath) return
      setStatus(resp.data)
    } catch (err: unknown) {
      if (scopeId !== asyncScopeRef.current || datasetPathRef.current !== requestPath) return
      const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? 'Failed to read stamp status'
      setStatusError(msg)
      setStatus(null)
    } finally {
      if (scopeId !== asyncScopeRef.current || datasetPathRef.current !== requestPath) return
      setStatusLoading(false)
    }
  }, [datasetPath])

  useEffect(() => {
    datasetPathRef.current = datasetPath
    asyncScopeRef.current += 1
    setStatus(null)
    setStatusLoading(false)
    setStatusError(null)
    setConfirmOpen(false)
    setSubmitting(false)
    setSubmitError(null)
    reset()
    if (datasetPath) {
      void refreshStatus()
    }
  }, [datasetPath, refreshStatus, reset])

  useEffect(() => {
    if (isCompleteStatus(jobStatus?.status)) {
      setConfirmOpen(false)
      void refreshStatus()
    }
  }, [jobStatus?.status, refreshStatus])

  const submit = useCallback(async (overwrite: boolean) => {
    if (!datasetPath) return

    const scopeId = asyncScopeRef.current
    const requestPath = datasetPath
    setSubmitting(true)
    setSubmitError(null)
    reset()

    try {
      const resp = await client.post<{ job_id: string; operation: string; status: string }>(
        '/datasets/stamp-cycles',
        { source_path: datasetPath, overwrite },
      )
      if (scopeId !== asyncScopeRef.current || datasetPathRef.current !== requestPath) return
      startPolling(resp.data.job_id)
    } catch (err: unknown) {
      if (scopeId !== asyncScopeRef.current || datasetPathRef.current !== requestPath) return
      const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? 'Stamp failed'
      setSubmitError(msg)
    } finally {
      if (scopeId !== asyncScopeRef.current || datasetPathRef.current !== requestPath) return
      setSubmitting(false)
    }
  }, [datasetPath, reset, startPolling])

  const handlePrimaryClick = () => {
    if (!status || statusLoading || statusError) return

    if (status?.stamped) {
      setConfirmOpen(true)
      return
    }

    void submit(false)
  }

  const canSubmit = Boolean(status) && !statusLoading && !statusError && !submitting && !polling

  if (!datasetPath) {
    return <div style={s.emptyState}>Load a dataset first to stamp cycle markers.</div>
  }

  return (
    <div style={s.tabContent}>
      <div style={s.matchPreview}>
        {statusLoading && <span style={{ color: 'var(--text-dim)' }}>Checking current stamp status...</span>}
        {statusError && <span style={s.errorText}>{statusError}</span>}
        {!statusLoading && !statusError && status && (
          status.stamped ? (
            <span style={{ color: 'var(--c-yellow)' }}>
              Already stamped. Sampled first parquet shows {status.is_terminal_count_sample} `is_terminal` flags. Overwriting will rewrite the parquet files in place.
            </span>
          ) : (
            <span style={{ color: 'var(--text-muted)' }}>
              No cycle markers detected yet. Stamping rewrites the dataset parquet files in place.
            </span>
          )
        )}
        {!statusLoading && !statusError && !status && (
          <span style={{ color: 'var(--text-dim)' }}>
            Stamp status is not available yet. Retry the status check before running this action.
          </span>
        )}
      </div>

      {submitError && <div style={s.errorText}>{submitError}</div>}

      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <button
          style={{ ...s.actionBtn, opacity: canSubmit ? 1 : 0.6 }}
          onClick={handlePrimaryClick}
          disabled={!canSubmit}
        >
          {submitLabel(submitting, status?.stamped)}
        </button>

        {(statusError || !status) && !statusLoading && (
          <button
            style={{ ...s.refreshBtn, padding: '6px 12px' }}
            onClick={() => { void refreshStatus() }}
            disabled={submitting || polling}
          >
            Retry Status Check
          </button>
        )}
      </div>

      <JobProgress jobStatus={jobStatus} polling={polling} />

      {confirmOpen && (
        <div style={s.matchPreview}>
          <span style={{ color: 'var(--c-yellow)' }}>
            This dataset already has cycle markers. Overwrite will replace the existing `is_terminal` and `is_last` columns in place.
          </span>
          <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
            <button
              style={{ ...s.actionBtn, background: 'var(--c-red)' }}
              onClick={() => {
                setConfirmOpen(false)
                void submit(true)
              }}
              disabled={submitting || polling}
            >
              Overwrite In Place
            </button>
            <button
              style={{ ...s.refreshBtn, padding: '6px 12px' }}
              onClick={() => setConfirmOpen(false)}
              disabled={submitting || polling}
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
