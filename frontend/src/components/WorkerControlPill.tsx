import { useEffect, useState } from 'react'

type WorkerState = 'running' | 'paused' | 'draining' | 'stopped'

type Worker = {
  worker_id: string
  desired_state: WorkerState
  actual_state: WorkerState | null
  last_beat_at: string | null
  in_flight_job_id: number | null
}

interface Props {
  workerId: string
  onInFlightJobChange?: (jobId: number | null) => void
}

const STALE_THRESHOLD_MS = 30_000
const POLL_INTERVAL_MS = 5000

export function WorkerControlPill({ workerId, onInFlightJobChange }: Props) {
  const [worker, setWorker] = useState<Worker | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let alive = true
    const tick = async () => {
      try {
        const r = await fetch(`/api/workers/${workerId}`)
        if (alive && r.ok) {
          const w = (await r.json()) as Worker
          setWorker(w)
          onInFlightJobChange?.(w.in_flight_job_id ?? null)
        }
      } catch {
        // network blips are tolerated; next tick will retry
      }
    }
    void tick()
    const id = setInterval(tick, POLL_INTERVAL_MS)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [workerId, onInFlightJobChange])

  if (!worker) return null

  const stale =
    worker.last_beat_at == null ||
    Date.now() - new Date(worker.last_beat_at).getTime() > STALE_THRESHOLD_MS

  const setState = async (next: WorkerState) => {
    setBusy(true)
    try {
      const r = await fetch(`/api/workers/${workerId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ desired_state: next }),
      })
      if (r.ok) {
        const w = (await r.json()) as Worker
        setWorker(w)
        onInFlightJobChange?.(w.in_flight_job_id ?? null)
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="worker-pill">
      <span style={{ fontFamily: 'var(--font-mono)' }}>
        {worker.actual_state ?? '–'}
      </span>
      {worker.desired_state !== worker.actual_state && (
        <span className="muted">→ {worker.desired_state}</span>
      )}
      {stale && <span className="badge-error">stale</span>}
      {worker.desired_state === 'running' && (
        <button
          className="btn-secondary"
          disabled={busy}
          onClick={() => setState('paused')}
        >
          Pause
        </button>
      )}
      {worker.desired_state === 'paused' && (
        <button
          className="btn-secondary"
          disabled={busy}
          onClick={() => setState('running')}
        >
          Resume
        </button>
      )}
    </div>
  )
}
