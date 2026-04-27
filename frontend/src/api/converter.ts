export type ConvertJobResponse = { id: number; status: string; type: string }

export async function enqueueConvertJob(
  payload: Record<string, unknown>,
): Promise<ConvertJobResponse> {
  const r = await fetch('/api/jobs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      type: 'convert',
      payload,
      dedupe_key: typeof payload.cell_task === 'string'
        ? payload.cell_task
        : typeof payload.cell === 'string'
          ? payload.cell
          : undefined,
    }),
  })
  if (!r.ok) {
    const body = await r.json().catch(() => ({}))
    throw new Error(body.error ?? `enqueue failed: ${r.status}`)
  }
  return r.json()
}
