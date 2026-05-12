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
      dedupe_key: dedupeKey ?? (typeof payload.cell_task === 'string'
        ? payload.cell_task
        : typeof payload.cell === 'string'
          ? payload.cell
          : undefined),
    }),
  })
  if (!r.ok) {
    const body = await r.json().catch(() => ({}))
    throw new Error(body.error ?? `enqueue failed: ${r.status}`)
  }
  return r.json()
}

export async function fetchConvertJob(jobId: number): Promise<ConvertJobResponse> {
  const r = await fetch(`/api/jobs/${jobId}`)
  if (!r.ok) {
    const body = await r.json().catch(() => ({}))
    throw new Error(body.error ?? `fetch job failed: ${r.status}`)
  }
  return r.json()
}
