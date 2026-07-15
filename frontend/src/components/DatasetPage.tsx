import { useState, useCallback, useEffect, useRef, useMemo } from 'react'
import { EpisodeList } from './EpisodeList'
import { EpisodeEditor } from './EpisodeEditor'
import { VideoPlayer, type VideoPlayerHandle } from './VideoPlayer'
import { ScalarChart } from './ScalarChart'
import { TrimPanel } from './TrimPanel'
import { useDataset } from '../hooks/useDataset'
import { useEpisodes } from '../hooks/useEpisodes'
import { OverviewTab } from './OverviewTab'
import { FieldsTab } from './FieldsTab'
import { GradeReasonModal } from './GradeReasonModal'
import { TaskEditor } from './TaskEditor'
import type { CurateFilter, DatasetTab, Episode } from '../types'

interface DatasetPageProps {
  datasetPath: string
  datasetName: string
  tab: DatasetTab
  filter?: CurateFilter
  onSetTab: (tab: DatasetTab, filter?: CurateFilter) => void
}

const GRADE_KEYS: Record<string, string> = { '1': 'good', '2': 'normal', '3': 'bad' }

function formatDuration(totalSeconds: number): string {
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const secs = Math.floor(totalSeconds % 60)
  if (hours > 0) return `${hours}h ${minutes}m ${secs}s`
  if (minutes > 0) return `${minutes}m ${secs}s`
  return `${secs}s`
}

export function DatasetPage({ datasetPath, datasetName: _datasetName, tab, filter, onSetTab }: DatasetPageProps) {
  const { dataset, loading: datasetLoading, error: datasetError, loadDataset } = useDataset()
  const { episodes, loading: epLoading, error: epError, fetchEpisodes, updateEpisode } = useEpisodes(datasetPath)
  const [selectedEpisode, setSelectedEpisode] = useState<Episode | null>(null)
  const [currentFrame, setCurrentFrame] = useState(0)
  const [terminalFrames, setTerminalFrames] = useState<number[]>([])
  const [terminalTimestamps, setTerminalTimestamps] = useState<number[]>([])
  const [rightTab, setRightTab] = useState<'details' | 'trim'>('details')
  const [rightWidth, setRightWidth] = useState<number>(() => {
    const saved = localStorage.getItem('curate-right-width')
    const n = saved ? parseInt(saved, 10) : 220
    return Number.isFinite(n) ? Math.max(220, Math.min(800, n)) : 220
  })
  const [reasonModal, setReasonModal] = useState<{
    grade: 'normal' | 'bad'
    initialReason: string
    pendingTags: string[]
  } | null>(null)
  const videoRef = useRef<VideoPlayerHandle>(null)
  const resizingRef = useRef(false)

  const handleResizeStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    resizingRef.current = true
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
    const onMove = (ev: MouseEvent) => {
      if (!resizingRef.current) return
      const next = window.innerWidth - ev.clientX
      const clamped = Math.max(220, Math.min(Math.floor(window.innerWidth * 0.6), next))
      setRightWidth(clamped)
    }
    const onUp = () => {
      resizingRef.current = false
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      setRightWidth(w => {
        localStorage.setItem('curate-right-width', String(w))
        return w
      })
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }, [])

  // Load dataset when path changes
  useEffect(() => {
    let cancelled = false
    async function init() {
      try {
        await loadDataset(datasetPath)
        if (!cancelled) {
          await fetchEpisodes()
        }
      } catch {
        // DatasetPage renders the dataset error state instead of continuing with stale content.
      }
    }
    void init()
    return () => { cancelled = true }
  }, [datasetPath, loadDataset, fetchEpisodes])

  useEffect(() => {
    setSelectedEpisode(null)
    setCurrentFrame(0)
    setTerminalFrames([])
    setTerminalTimestamps([])
  }, [datasetPath])

  useEffect(() => {
    setSelectedEpisode(prev => {
      if (!prev) return prev
      return episodes.find(ep => ep.episode_index === prev.episode_index) ?? null
    })
  }, [episodes])

  const datasetReady = dataset?.path === datasetPath
  const isRawDataset = datasetReady && dataset?.robot_type === 'raw'

  useEffect(() => {
    if (isRawDataset) setRightTab('details')
  }, [isRawDataset])

  const fps = dataset?.fps ?? 30

  const curateEpisodes = useMemo(() => {
    let result = episodes
    if (filter?.lengthRange) {
      const [min, max] = filter.lengthRange
      result = result.filter(e => e.length >= min && e.length < max)
    }
    if (filter?.tag) {
      result = result.filter(e => e.tags.includes(filter.tag!))
    }
    return result
  }, [episodes, filter])

  const filterChip = useMemo(() => {
    if (filter?.lengthRange) {
      const [min, max] = filter.lengthRange
      return {
        label: `Length: ${formatDuration(min / fps)} ~ ${formatDuration(max / fps)}`,
        onClear: () => onSetTab('curate'),
      }
    }
    if (filter?.tag) {
      return {
        label: `Tag: ${filter.tag}`,
        onClear: () => onSetTab('curate'),
      }
    }
    return null
  }, [filter, fps, onSetTab])

  const initialGradeFilter = useMemo(() => {
    switch (filter?.grade) {
      case 'all':
      case 'good':
      case 'normal':
      case 'bad':
      case 'ungraded':
        return filter.grade
      default:
        return undefined
    }
  }, [filter?.grade])


  const handleSaveEpisode = useCallback(
    async (
      index: number,
      grade: string | null,
      tags: string[],
      reason: string | null = null,
    ) => {
      await updateEpisode(index, grade, tags, reason)
      if (grade) {
        const currentIdx = curateEpisodes.findIndex(e => e.episode_index === index)
        const ungradedInView = curateEpisodes.filter(e => !e.grade)
        const nextUngraded = ungradedInView.find(e => {
          const i = curateEpisodes.indexOf(e)
          return i > currentIdx
        }) ?? ungradedInView.find(e => {
          const i = curateEpisodes.indexOf(e)
          return i < currentIdx
        })
        if (nextUngraded) {
          setSelectedEpisode(nextUngraded)
          return
        }
      }
      setSelectedEpisode(prev =>
        prev?.episode_index === index ? { ...prev, grade, tags, reason } : prev,
      )
    },
    [updateEpisode, curateEpisodes],
  )

  const requestGrade = useCallback(
    (grade: 'good' | 'normal' | 'bad') => {
      if (!selectedEpisode) return
        if (grade === 'good') {
        // good clears any prior reason (server enforces this too)
        void handleSaveEpisode(selectedEpisode.episode_index, grade, selectedEpisode.tags, null)
        return
      }
      setReasonModal({
        grade,
        initialReason: selectedEpisode.reason ?? '',
        pendingTags: selectedEpisode.tags,
      })
    },
    [selectedEpisode, handleSaveEpisode],
  )

  const navigateEpisode = useCallback((direction: -1 | 1) => {
    if (!selectedEpisode || curateEpisodes.length === 0) return
    const idx = curateEpisodes.findIndex(e => e.episode_index === selectedEpisode.episode_index)
    const next = curateEpisodes[idx + direction]
    if (next) setSelectedEpisode(next)
  }, [selectedEpisode, curateEpisodes])

  const quickGrade = useCallback(
    (key: string) => {
      const grade = GRADE_KEYS[key] as 'good' | 'normal' | 'bad' | undefined
      if (grade) requestGrade(grade)
    },
    [requestGrade],
  )

  // Keyboard shortcuts
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return
      if (reasonModal) return  // Modal is open: let the textarea consume keys.
      switch (e.key) {
        case 'ArrowUp':
        case 'k':
          e.preventDefault(); navigateEpisode(-1); break
        case 'ArrowDown':
        case 'j':
          e.preventDefault(); navigateEpisode(1); break
        case 'ArrowLeft':
          e.preventDefault(); videoRef.current?.stepFrame(-1); break
        case 'ArrowRight':
          e.preventDefault(); videoRef.current?.stepFrame(1); break
        case ' ':
          e.preventDefault(); videoRef.current?.togglePlay(); break
        case 'q':
        case 'Q':
          e.preventDefault(); videoRef.current?.cycleSpeed(1); break
        case 'w':
        case 'W':
          e.preventDefault(); videoRef.current?.cycleSpeed(-1); break
        case '1':
        case '2':
        case '3':
          e.preventDefault(); quickGrade(e.key); break
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [navigateEpisode, quickGrade, reasonModal])

  if (datasetError && !datasetLoading && !datasetReady) {
    return (
      <div className="dataset-page">
        <div className="dataset-status">
          <div className="dataset-status-copy error">{datasetError}</div>
        </div>
      </div>
    )
  }

  if (!datasetReady) {
    return (
      <div className="dataset-page">
        <div className="dataset-status">
          <div className={`dataset-status-copy${datasetLoading ? ' loading-pulse' : ''}`}>
            Loading dataset...
          </div>
        </div>
      </div>
    )
  }

  if (tab === 'overview') {
    return (
      <div className="dataset-page">
        <OverviewTab
          datasetPath={datasetPath}
          fps={dataset?.fps ?? 30}
          episodes={episodes}
          onNavigateCurate={(f) => onSetTab('curate', f)}
          onBulkGradeApplied={() => fetchEpisodes()}
        />
      </div>
    )
  }

  if (tab === 'fields') {
    return (
      <div className="dataset-page">
        <FieldsTab datasetPath={datasetPath} readOnly={isRawDataset} />
      </div>
    )
  }

  return (
    <div className="dataset-page">
      <div className="curate-layout">
        {/* Left: episode list */}
        <div className="episode-sidebar">
          <EpisodeList
            episodes={curateEpisodes}
            loading={epLoading}
            error={epError}
            onEpisodeSelect={setSelectedEpisode}
            selectedIndex={selectedEpisode?.episode_index ?? null}
            initialGradeFilter={initialGradeFilter}
            filterChip={filterChip}
          />
        </div>

        {/* Center: video + grade */}
        <div className="curate-center">
          <VideoPlayer
            datasetKey={dataset?.dataset_key ?? null}
            ref={videoRef}
            episodeIndex={selectedEpisode?.episode_index ?? null}
            rawRecording={isRawDataset ? selectedEpisode?.raw_recording ?? null : null}
            fps={dataset?.fps ?? 30}
            episodeLengthFrames={selectedEpisode?.length ?? null}
            onFrameChange={setCurrentFrame}
            terminalFrames={terminalFrames}
          />

          {/* Terminal frames */}
          {selectedEpisode && terminalFrames.length > 0 && (
            <div className="terminal-bar">
              <span className="terminal-bar-label">Terminal ({terminalFrames.length}):</span>
              {terminalFrames.map((f, i) => {
                const ts = terminalTimestamps[i]
                const label = ts != null ? `${ts.toFixed(2)}s` : `f${f}`
                return (
                  <button
                    key={f}
                    className={`terminal-frame-chip${currentFrame === f ? ' active' : ''}`}
                    onClick={() => ts != null
                      ? videoRef.current?.seekToTimestamp(ts)
                      : videoRef.current?.seekToFrame(f)
                    }
                  >
                    {label}
                  </button>
                )
              })}
            </div>
          )}

          {/* Grade bar */}
          {selectedEpisode && (
            <div className="grade-bar">
              {(['good', 'normal', 'bad'] as const).map(g => (
                <button
                  key={g}
                  className={`grade-btn${selectedEpisode.grade === g ? ' active' : ''}`}
                  onClick={() => requestGrade(g)}
                  style={{
                    color: selectedEpisode.grade === g ? (g === 'good' ? 'var(--c-green)' : g === 'normal' ? 'var(--c-yellow)' : 'var(--c-red)') : undefined,
                    borderBottomColor: selectedEpisode.grade === g ? (g === 'good' ? 'var(--c-green)' : g === 'normal' ? 'var(--c-yellow)' : 'var(--c-red)') : undefined,
                  }}
                >
                  {g}
                </button>
              ))}
              <div className="grade-kbd-hint">
                <kbd>1</kbd><kbd>2</kbd><kbd>3</kbd>
              </div>
            </div>
          )}
          {selectedEpisode && selectedEpisode.reason && (
            <div className="grade-reason-display">
              Reason: {selectedEpisode.reason}
            </div>
          )}
        </div>

        {/* Resizer handle */}
        <div
          className="curate-resizer"
          role="separator"
          aria-orientation="vertical"
          onMouseDown={handleResizeStart}
          onDoubleClick={() => {
            setRightWidth(220)
            localStorage.setItem('curate-right-width', '220')
          }}
          title="Drag to resize (double-click to reset)"
        />

        {/* Right: details / split-merge */}
        <div className="curate-right" style={{ width: rightWidth }}>
          <div className="right-tabs">
            <button
              className={`right-tab${rightTab === 'details' ? ' active' : ''}`}
              onClick={() => setRightTab('details')}
            >
              Details
            </button>
            <button
              className={`right-tab${rightTab === 'trim' ? ' active' : ''}`}
              onClick={() => setRightTab('trim')}
              disabled={isRawDataset}
              title={isRawDataset ? 'Raw trim is read-only in v1' : undefined}
            >
              Trim
            </button>
          </div>

          {rightTab === 'details' && (
            <div style={{ flex: 1, overflowY: 'auto' }}>
              <EpisodeEditor episode={selectedEpisode} onSave={handleSaveEpisode} />
              <TaskEditor
                datasetPath={datasetPath}
                episode={selectedEpisode}
                readOnly={isRawDataset}
                onInstructionUpdated={async (result) => {
                  await fetchEpisodes()
                  setSelectedEpisode(result.episode)
                }}
              />
              {isRawDataset ? (
                <div style={{ padding: 12, fontSize: 12, color: 'var(--text-muted)' }}>
                  Raw scalar extraction is unavailable in v1.
                </div>
              ) : (
                <ScalarChart
                  datasetPath={datasetPath}
                  episodeIndex={selectedEpisode?.episode_index ?? null}
                  currentFrame={currentFrame}
                  onTerminalFrames={(frames, timestamps) => {
                    setTerminalFrames(frames)
                    setTerminalTimestamps(timestamps)
                  }}
                />
              )}
            </div>
          )}
          {rightTab === 'trim' && !isRawDataset && (
            <TrimPanel
              datasetPath={dataset?.path ?? null}
              episodes={episodes}
            />
          )}
        </div>
      </div>
      <GradeReasonModal
        open={reasonModal !== null}
        grade={reasonModal?.grade ?? 'bad'}
        initialReason={reasonModal?.initialReason}
        onSave={(reason) => {
          if (!selectedEpisode || !reasonModal) return
          const m = reasonModal
          setReasonModal(null)
          void handleSaveEpisode(
            selectedEpisode.episode_index,
            m.grade,
            m.pendingTags,
            reason,
          )
        }}
        onCancel={() => setReasonModal(null)}
      />
    </div>
  )
}
