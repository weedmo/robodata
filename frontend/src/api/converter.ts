export type ConvertJobProgress = {
  phase?: string
  cell_task?: string | null
  recording?: string | null
  recording_index?: number | null
  recording_total?: number | null
  task_index?: number | null
  task_total?: number | null
  pending_recordings?: number | null
  last_converted_recording?: string | null
  last_failed_recording?: string | null
  last_error_code?: string | null
}

export type ConvertJobResponse = {
  id: number
  status: string
  type: string
  progress?: ConvertJobProgress
}

export type RetryFailedResponse = {
  retry_count: number
  job: ConvertJobResponse
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (value === null || typeof value !== 'object') {
    return null
  }
  return value as Record<string, unknown>
}

function autoDedupeKey(payload: Record<string, unknown>): string | undefined {
  if (typeof payload.cell_task === 'string') {
    return payload.cell_task
  }
  if (typeof payload.cell === 'string') {
    return payload.cell
  }
  return undefined
}

async function converterErrorMessage(response: Response, fallback: string): Promise<string> {
  const body = asRecord(await response.json().catch(() => null))
  const detail = asRecord(body?.detail)
  const detailError = detail?.error
  const topError = body?.error
  const detailText = body?.detail
  if (typeof detailError === 'string') return detailError
  if (typeof topError === 'string') return topError
  if (typeof detailText === 'string') return detailText
  return fallback
}

async function expectJson<T>(response: Response, fallback: string): Promise<T> {
  if (!response.ok) {
    throw new Error(await converterErrorMessage(response, fallback))
  }
  return response.json()
}

export async function enqueueConvertJob(
  payload: Record<string, unknown>,
  dedupeKey?: string,
): Promise<ConvertJobResponse> {
  const r = await fetch('/api/jobs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      type: 'convert',
      payload,
      dedupe_key: dedupeKey ?? autoDedupeKey(payload),
    }),
  })
  return expectJson<ConvertJobResponse>(r, `enqueue failed: ${r.status}`)
}

export async function retryFailedConvertJob(cellTask: string): Promise<RetryFailedResponse> {
  const r = await fetch('/api/converter/retry-failed', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cell_task: cellTask }),
  })
  return expectJson<RetryFailedResponse>(r, `retry failed: ${r.status}`)
}

export async function fetchConvertJob(jobId: number): Promise<ConvertJobResponse> {
  const r = await fetch(`/api/jobs/${jobId}`)
  return expectJson<ConvertJobResponse>(r, `fetch job failed: ${r.status}`)
}

export async function cancelConvertJob(jobId: number): Promise<void> {
  const r = await fetch(`/api/jobs/${jobId}/cancel`, {
    method: 'POST',
  })
  if (!r.ok) {
    throw new Error(await converterErrorMessage(r, `cancel failed: ${r.status}`))
  }
}
