import { useState } from 'react'
import { listRawRecordings, visualizeRaw, type RawRecording } from '../api/rawViz'
import type { ConverterTaskProgress } from '../types'

interface Props {
  tasks: ConverterTaskProgress[]
}

// The embedded Rerun web viewer (served at /rerun/) must be told which gRPC
// proxy to connect to, otherwise it shows the welcome screen. The converter
// streams to the rerun service's proxy, published on the host at :9876.
const RERUN_PROXY_PORT = 9876
function rerunViewerSrc(): string {
  const proxy = `rerun+http://${window.location.hostname}:${RERUN_PROXY_PORT}/proxy`
  return `/rerun/?url=${encodeURIComponent(proxy)}`
}

// Raw rosbag curation: pick a task, list its recordings (1 mcap = 1 episode),
// and stream one into the shared Rerun viewer. Warnings surface in the viewer
// (never auto-bad) so a human makes the final good/bad call.
export function RawVisualizePanel({ tasks }: Props) {
  const [task, setTask] = useState('')
  const [recordings, setRecordings] = useState<RawRecording[]>([])
  const [loading, setLoading] = useState(false)
  const [active, setActive] = useState<string | null>(null)
  const [viewerOpen, setViewerOpen] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const loadRecordings = async (nextTask: string) => {
    setTask(nextTask)
    setRecordings([])
    setError(null)
    setMessage(null)
    if (!nextTask) return
    setLoading(true)
    try {
      setRecordings(await listRawRecordings(nextTask))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'failed to list recordings')
    } finally {
      setLoading(false)
    }
  }

  const visualize = async (recording: string) => {
    setActive(recording)
    setError(null)
    setMessage(null)
    try {
      const detail = await visualizeRaw(recording)
      setMessage(detail)
      setViewerOpen(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'visualization failed')
    } finally {
      setActive(null)
    }
  }

  return (
    <section className="raw-viz">
      <h3 className="raw-viz-title">Raw 시각화 (Rerun)</h3>

      <label className="raw-viz-task">
        Task
        <select value={task} onChange={(e) => void loadRecordings(e.target.value)}>
          <option value="">Task 선택…</option>
          {tasks.map((t) => (
            <option key={t.cell_task} value={t.cell_task}>
              {t.cell_task}
            </option>
          ))}
        </select>
      </label>

      {loading && <div className="raw-viz-status">불러오는 중…</div>}
      {error && <div className="raw-viz-error">{error}</div>}

      {!loading && task && recordings.length === 0 && !error && (
        <div className="raw-viz-status">recording 없음</div>
      )}

      {recordings.length > 0 && (
        <ul className="raw-viz-list">
          {recordings.map((r) => (
            <li key={r.recording} className="raw-viz-item">
              <span style={{ fontFamily: 'var(--font-mono)' }}>{r.serial}</span>
              <button
                type="button"
                disabled={active === r.recording}
                onClick={() => void visualize(r.recording)}
              >
                {active === r.recording ? '여는 중…' : 'Rerun에서 보기'}
              </button>
            </li>
          ))}
        </ul>
      )}

      {message && <div className="raw-viz-message">{message}</div>}

      {viewerOpen && (
        <iframe
          className="raw-viz-viewer"
          title="Rerun viewer"
          src={rerunViewerSrc()}
        />
      )}
    </section>
  )
}
