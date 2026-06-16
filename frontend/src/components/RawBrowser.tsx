import { useCallback, useEffect, useState } from 'react'
import {
  listRawCells,
  listRawTasks,
  listRawRecordings,
  visualizeRaw,
  type RawCell,
  type RawTask,
  type RawRecording,
} from '../api/rawViz'

interface Props {
  sourcePath: string
}

// Hierarchical browse of raw rosbag data: cell -> task -> recording -> Rerun.
// Mirrors the lerobot source depth, but the leaf streams a raw episode into the
// shared Rerun viewer (warnings surface there; the human makes the good/bad call).
const RERUN_PROXY_PORT = import.meta.env.VITE_RERUN_PROXY_PORT || '9876'
function rerunViewerSrc(): string {
  const proxy = `rerun+http://${window.location.hostname}:${RERUN_PROXY_PORT}/proxy`
  return `/rerun/?url=${encodeURIComponent(proxy)}`
}

type Level = 'cells' | 'tasks' | 'recordings'

export function RawBrowser({ sourcePath }: Props) {
  const [level, setLevel] = useState<Level>('cells')
  const [cell, setCell] = useState<string>('')
  const [task, setTask] = useState<string>('')

  const [cells, setCells] = useState<RawCell[]>([])
  const [tasks, setTasks] = useState<RawTask[]>([])
  const [recordings, setRecordings] = useState<RawRecording[]>([])

  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [active, setActive] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [viewerOpen, setViewerOpen] = useState(false)

  const load = useCallback(async (fn: () => Promise<void>) => {
    setLoading(true)
    setError(null)
    try {
      await fn()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'failed to load')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load(async () => setCells(await listRawCells(sourcePath)))
  }, [sourcePath, load])

  const openCell = (c: RawCell) => {
    setCell(c.name)
    setTasks([])
    setLevel('tasks')
    void load(async () => setTasks(await listRawTasks(c.name)))
  }

  const openTask = (t: RawTask) => {
    setTask(t.task)
    setRecordings([])
    setLevel('recordings')
    void load(async () => setRecordings(await listRawRecordings(t.task)))
  }

  const visualize = async (recording: string) => {
    setActive(recording)
    setError(null)
    setMessage(null)
    try {
      setMessage(await visualizeRaw(recording))
      setViewerOpen(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'visualization failed')
    } finally {
      setActive(null)
    }
  }

  const goCells = () => { setLevel('cells'); setCell(''); setTask('') }
  const goTasks = () => { setLevel('tasks'); setTask('') }

  return (
    <div className="raw-browser">
      <nav className="raw-crumbs">
        <button type="button" className="raw-crumb" onClick={goCells}>raw</button>
        {cell && <><span className="raw-crumb-sep">/</span>
          <button type="button" className="raw-crumb" onClick={goTasks} disabled={level === 'tasks'}>{cell}</button></>}
        {task && level === 'recordings' && <><span className="raw-crumb-sep">/</span>
          <span className="raw-crumb-current" style={{ fontFamily: 'var(--font-mono)' }}>{task.split('/').slice(1).join('/')}</span></>}
      </nav>

      {loading && <div className="raw-viz-status">불러오는 중…</div>}
      {error && <div className="raw-viz-error">{error}</div>}

      {level === 'cells' && (
        <ul className="raw-grid">
          {cells.map((c) => (
            <li key={c.name}>
              <button type="button" className="raw-card" disabled={!c.active} onClick={() => openCell(c)}>
                {c.name}
              </button>
            </li>
          ))}
        </ul>
      )}

      {level === 'tasks' && (
        <ul className="raw-grid">
          {tasks.map((t) => (
            <li key={t.task}>
              <button type="button" className="raw-card" onClick={() => openTask(t)}>
                <span>{t.name}</span>
                <span className="raw-card-count">{t.count}</span>
              </button>
            </li>
          ))}
          {!loading && tasks.length === 0 && <li className="raw-viz-status">task 없음</li>}
        </ul>
      )}

      {level === 'recordings' && (
        <>
          <ul className="raw-viz-list">
            {recordings.map((r) => (
              <li key={r.recording} className="raw-viz-item">
                <span style={{ fontFamily: 'var(--font-mono)' }}>{r.serial}</span>
                <button type="button" disabled={active === r.recording} onClick={() => void visualize(r.recording)}>
                  {active === r.recording ? '여는 중…' : 'Rerun에서 보기'}
                </button>
              </li>
            ))}
            {!loading && recordings.length === 0 && <li className="raw-viz-status">recording 없음</li>}
          </ul>
          {message && <div className="raw-viz-message">{message}</div>}
          {viewerOpen && (
            <iframe className="raw-viz-viewer" title="Rerun viewer" src={rerunViewerSrc()} />
          )}
        </>
      )}
    </div>
  )
}
