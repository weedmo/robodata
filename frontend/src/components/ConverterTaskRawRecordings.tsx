import { useCallback, useRef, useState } from 'react'
import {
  listRawRecordings,
  type RawRecording,
} from '../api/rawRecordings'
import { RawRecordingList } from './RawRecordingList'

interface Props {
  task: string
}

export function ConverterTaskRawRecordings({ task }: Props) {
  const [recordings, setRecordings] = useState<RawRecording[]>([])
  const [loaded, setLoaded] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const requestRef = useRef(0)

  const loadRecordings = useCallback(async () => {
    if (loaded || loading) return
    const requestId = ++requestRef.current
    setLoading(true)
    setError(null)
    try {
      const next = await listRawRecordings(task)
      if (requestRef.current !== requestId) return
      setRecordings(next)
      setLoaded(true)
    } catch (err) {
      if (requestRef.current !== requestId) return
      setError(err instanceof Error ? err.message : 'failed to list recordings')
    } finally {
      if (requestRef.current === requestId) setLoading(false)
    }
  }, [loaded, loading, task])

  return (
    <details
      className="cvp-card-raw"
      onToggle={(event) => {
        if (event.currentTarget.open) void loadRecordings()
      }}
    >
      <summary>Raw data</summary>
      <div className="cvp-card-raw-body">
        {loading && <div className="raw-recording-status">불러오는 중…</div>}
        {error && (
          <div className="raw-recording-error">
            <span>{error}</span>
            {!loaded && (
              <button type="button" className="btn-secondary" onClick={() => void loadRecordings()}>
                다시 시도
              </button>
            )}
          </div>
        )}
        {!loading && loaded && recordings.length === 0 && (
          <div className="raw-recording-status">recording 없음</div>
        )}
        {recordings.length > 0 && (
          <RawRecordingList recordings={recordings} />
        )}
      </div>
    </details>
  )
}
