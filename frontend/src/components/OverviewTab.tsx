import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts'
import type { BarRectangleItem } from 'recharts/types/cartesian/Bar'
import { useDistribution } from '../hooks/useDistribution'
import client from '../api/client'
import { GradeReasonModal } from './GradeReasonModal'
import type { CurateFilter, DistributionResult, Episode, GradeFilter } from '../types'
import {
  bulkTargetsForCard,
  episodesForGradeKey,
  bulkOpBannerMessage,
  type BulkTargetGrade,
} from './overviewBulkGrade'

interface ContextMenuState {
  x: number
  y: number
  source: 'bar' | 'grade-card'
  field: string
  label: string
}

interface OverviewTabProps {
  datasetPath: string
  fps: number
  episodes: Episode[]
  onNavigateCurate: (filter: CurateFilter) => void
  onBulkGradeApplied: () => Promise<void>
}

interface BulkEpisodeState {
  grade: string | null
  reason: string | null
}

interface LastBulkOp {
  field: string
  targetGrade: BulkTargetGrade
  episodeIndices: number[]
  prevByIdx: Record<number, BulkEpisodeState>
}

const CHART_COLORS = ['#89b4fa', '#a6e3a1', '#f9e2af', '#f38ba8', '#cba6f7', '#ff9830']
const AUTO_FIELDS = new Set(['grade', 'tags', 'length', 'task_instruction', 'collection_date'])

const FIELD_LABELS: Record<string, string> = {
  grade: 'Grade',
  tags: 'Tags',
  length: 'Episode Length',
  task_instruction: 'Task Instruction',
  collection_date: 'Collection Date',
}

export function OverviewTab({ datasetPath, fps, episodes, onNavigateCurate, onBulkGradeApplied }: OverviewTabProps) {
  const { fields, charts, loading, error, fetchFields, addChart, removeChart } = useDistribution()
  const [selectedFields, setSelectedFields] = useState<Set<string>>(new Set())
  const [chartIntensity, setChartIntensity] = useState(1)
  const [contextMenu, setContextMenu] = useState<ContextMenuState | null>(null)
  const [bulkReasonModal, setBulkReasonModal] = useState<{
    episodeIndices: number[]
    targetGrade: 'normal' | 'bad'
    field: string
    label: string
  } | null>(null)
  const [lastBulkOp, setLastBulkOp] = useState<LastBulkOp | null>(null)
  const [isUndoing, setIsUndoing] = useState(false)
  const [undoError, setUndoError] = useState<string | null>(null)
  const initializedRef = useRef(false)

  // Close context menu on click anywhere
  useEffect(() => {
    if (!contextMenu) return
    const close = () => setContextMenu(null)
    window.addEventListener('click', close)
    return () => window.removeEventListener('click', close)
  }, [contextMenu])

  const openBulkBarModal = useCallback((menu: ContextMenuState) => {
    const indices: number[] = []
    if (menu.field === 'length') {
      const parts = menu.label.split('-').map(Number)
      if (parts.length === 2 && parts.every(n => !isNaN(n))) {
        for (const ep of episodes) {
          if (ep.length >= parts[0] && ep.length < parts[1]) indices.push(ep.episode_index)
        }
      }
    } else if (menu.field === 'tags') {
      for (const ep of episodes) {
        if (ep.tags.includes(menu.label)) indices.push(ep.episode_index)
      }
    }
    if (indices.length === 0) return
    setBulkReasonModal({
      episodeIndices: indices,
      targetGrade: 'bad',
      field: menu.field,
      label: menu.label,
    })
    setContextMenu(null)
  }, [episodes])

  const openBulkGradeCardMenu = useCallback(
    (currentKey: string, count: number, x: number, y: number) => {
      if (count <= 0) return
      const indices = episodesForGradeKey(episodes, currentKey)
      if (indices.length === 0) return
      setContextMenu({
        source: 'grade-card',
        field: 'grade',
        label: currentKey,
        x,
        y,
      })
    },
    [episodes],
  )

  const applyBulkGrade = useCallback(
    (targetGrade: BulkTargetGrade) => {
      const menu = contextMenu
      if (!menu || menu.source !== 'grade-card') return
      const indices = episodesForGradeKey(episodes, menu.label)
      if (indices.length === 0) {
        setContextMenu(null)
        return
      }
      setContextMenu(null)
      if (targetGrade === 'good') {
        // good은 reason 모달 없이 즉시 호출. submitBulkGrade는 모달 경로 전용이므로
        // good 분기는 인라인으로 처리.
        void (async () => {
          const prevByIdx: Record<number, BulkEpisodeState> = {}
          for (const episodeIndex of indices) {
            const episode = episodes.find(ep => ep.episode_index === episodeIndex)
            if (!episode) continue
            prevByIdx[episodeIndex] = { grade: episode.grade, reason: episode.reason }
          }
          await client.post('/episodes/bulk-grade', {
            episode_indices: indices,
            grade: 'good',
            reason: null,
          })
          setUndoError(null)
          setLastBulkOp({
            field: 'grade',
            targetGrade: 'good',
            episodeIndices: indices,
            prevByIdx,
          })
          const refreshResults = await Promise.allSettled([
            onBulkGradeApplied(),
            addChart(datasetPath, 'grade', 'auto'),
          ])
          const failedRefreshCount = refreshResults.filter(r => r.status === 'rejected').length
          if (failedRefreshCount > 0) {
            setUndoError(`good 처리는 완료됐지만 화면 갱신에 실패했습니다 (${failedRefreshCount}건) · 다시 시도하세요`)
          }
        })()
        return
      }
      // normal/bad는 reason 모달 경유
      setBulkReasonModal({
        episodeIndices: indices,
        targetGrade,
        field: 'grade',
        label: menu.label,
      })
    },
    [contextMenu, episodes, datasetPath, addChart, onBulkGradeApplied],
  )

  const submitBulkGrade = useCallback(
    async (targetGrade: BulkTargetGrade, reason: string | null) => {
      const m = bulkReasonModal
      if (!m) return
      const prevByIdx: Record<number, BulkEpisodeState> = {}
      for (const episodeIndex of m.episodeIndices) {
        const episode = episodes.find(ep => ep.episode_index === episodeIndex)
        if (!episode) continue
        prevByIdx[episodeIndex] = {
          grade: episode.grade,
          reason: episode.reason,
        }
      }

      setBulkReasonModal(null)
      await client.post('/episodes/bulk-grade', {
        episode_indices: m.episodeIndices,
        grade: targetGrade,
        reason,
      })
      setUndoError(null)
      setLastBulkOp({
        field: m.field,
        targetGrade,
        episodeIndices: m.episodeIndices,
        prevByIdx,
      })

      const refreshFields: Promise<unknown>[] = [
        onBulkGradeApplied(),
        addChart(datasetPath, 'grade', 'auto'),
      ]
      if (m.field === 'length' || m.field === 'tags') {
        refreshFields.push(addChart(datasetPath, m.field, m.field === 'length' ? 'histogram' : 'auto'))
      }
      const refreshResults = await Promise.allSettled(refreshFields)
      const failedRefreshCount = refreshResults.filter(r => r.status === 'rejected').length
      if (failedRefreshCount > 0) {
        setUndoError(`${targetGrade} 처리는 완료됐지만 화면 갱신에 실패했습니다 (${failedRefreshCount}건) · 다시 시도하세요`)
      }
    },
    [bulkReasonModal, episodes, datasetPath, addChart, onBulkGradeApplied],
  )

  const undoLastBulkGrade = useCallback(async () => {
    if (!lastBulkOp || isUndoing) return

    const grouped = new Map<string, { grade: string; reason: string | null; episodeIndices: number[] }>()
    const ungradedEpisodeIndices: number[] = []

    for (const episodeIndex of lastBulkOp.episodeIndices) {
      const prev = lastBulkOp.prevByIdx[episodeIndex]
      if (!prev) continue
      if (prev.grade == null) {
        ungradedEpisodeIndices.push(episodeIndex)
        continue
      }

      const key = JSON.stringify([prev.grade, prev.reason])
      const existing = grouped.get(key)
      if (existing) {
        existing.episodeIndices.push(episodeIndex)
        continue
      }

      grouped.set(key, {
        grade: prev.grade,
        reason: prev.reason,
        episodeIndices: [episodeIndex],
      })
    }

    setIsUndoing(true)
    setUndoError(null)

    const restoreResults = await Promise.allSettled([
      ...Array.from(grouped.values()).map(group => (
        client.post('/episodes/bulk-grade', {
          episode_indices: group.episodeIndices,
          grade: group.grade,
          reason: group.reason,
        })
      )),
      ...ungradedEpisodeIndices.map(episodeIndex => (
        client.patch(`/episodes/${episodeIndex}`, {
          grade: null,
          reason: null,
        })
      )),
    ])
    const failedRestoreCount = restoreResults.filter(result => result.status === 'rejected').length

    const refreshResults = await Promise.allSettled([
      onBulkGradeApplied(),
      ...(lastBulkOp.field !== 'grade'
        ? [addChart(datasetPath, lastBulkOp.field, lastBulkOp.field === 'length' ? 'histogram' : 'auto')]
        : []),
      addChart(datasetPath, 'grade', 'auto'),
    ])
    const failedRefreshCount = refreshResults.filter(result => result.status === 'rejected').length

    if (failedRestoreCount === 0 && failedRefreshCount === 0) {
      setLastBulkOp(null)
      setUndoError(null)
    } else if (failedRestoreCount === 0) {
      setUndoError(`되돌리기는 완료됐지만 화면 갱신에 실패했습니다 (${failedRefreshCount}건) · 다시 시도하세요`)
    } else if (failedRefreshCount === 0) {
      setUndoError(`되돌리기 일부 실패 (${failedRestoreCount}건) · 다시 시도하세요`)
    } else {
      setUndoError(`되돌리기 일부 실패 및 화면 갱신 실패 (${failedRestoreCount}/${failedRefreshCount}건) · 다시 시도하세요`)
    }
    setIsUndoing(false)
  }, [lastBulkOp, isUndoing, datasetPath, addChart, onBulkGradeApplied])

  useEffect(() => {
    initializedRef.current = false
    setSelectedFields(new Set())
    setLastBulkOp(null)
    setUndoError(null)
    setIsUndoing(false)
  }, [datasetPath])

  useEffect(() => {
    void fetchFields(datasetPath)
  }, [datasetPath, fetchFields])

  useEffect(() => {
    if (fields.length > 0 && !initializedRef.current) {
      initializedRef.current = true
      const autoFields = fields.filter(f => AUTO_FIELDS.has(f.name))
      if (autoFields.length > 0) {
        const names = new Set(autoFields.map(f => f.name))
        setSelectedFields(names)
        autoFields.forEach(f => {
          void addChart(datasetPath, f.name, f.name === 'length' ? 'histogram' : 'auto')
        })
      }
    }
  }, [fields, datasetPath, addChart])

  const toggleField = (fieldName: string) => {
    setSelectedFields(prev => {
      const next = new Set(prev)
      if (next.has(fieldName)) {
        next.delete(fieldName)
        removeChart(fieldName)
      } else {
        next.add(fieldName)
        void addChart(datasetPath, fieldName)
      }
      return next
    })
  }

  const gradeChart = charts.find(c => c.field === 'grade')
  const otherCharts = charts.filter(c => c.field !== 'grade')

  return (
    <div className="overview-layout">
      <div className="overview-fields-panel">
        <div className="fields-panel-section">
          <div className="fields-panel-section-header">
            <span>Fields</span>
            <span style={{ color: 'var(--text-dim)', fontSize: 9 }}>{fields.length}</span>
          </div>
          {fields.map(f => (
            <label key={f.name} className={`field-checkbox${selectedFields.has(f.name) ? ' checked' : ''}`}>
              <input type="checkbox" checked={selectedFields.has(f.name)} onChange={() => toggleField(f.name)} />
              <span>{FIELD_LABELS[f.name] ?? f.name}</span>
            </label>
          ))}
        </div>
        <div className="fields-panel-section">
          <div className="fields-panel-section-header">
            <span>Chart Intensity</span>
          </div>
          <div style={{ padding: '6px 12px 10px' }}>
            <input
              type="range"
              min={0.1}
              max={2}
              step={0.05}
              value={chartIntensity}
              onChange={e => setChartIntensity(Number(e.target.value))}
              style={{ width: '100%', accentColor: 'var(--accent)' }}
            />
          </div>
        </div>
      </div>

      <div className="overview-charts">
        {lastBulkOp && (
          <div className="overview-undo-banner">
            <span>{bulkOpBannerMessage(lastBulkOp.targetGrade, lastBulkOp.episodeIndices.length)}</span>
            <span aria-hidden="true">·</span>
            <button
              type="button"
              className="overview-undo-banner-button"
              onClick={() => void undoLastBulkGrade()}
              disabled={isUndoing}
            >
              {isUndoing ? '되돌리는 중...' : '되돌리기'}
            </button>
          </div>
        )}
        {lastBulkOp && undoError && <div className="overview-undo-error">{undoError}</div>}
        {gradeChart && (
          <GradeSummary
            chart={gradeChart}
            fps={fps}
            episodes={episodes}
            onNavigateCurate={onNavigateCurate}
            onCardContextMenu={openBulkGradeCardMenu}
          />
        )}

        {loading && <div className="loading-pulse" style={{ fontSize: 11, color: 'var(--text-muted)' }}>Computing...</div>}
        {error && <div style={{ fontSize: 11, color: 'var(--c-red)' }}>{error}</div>}

        {otherCharts.map((chart, idx) => {
          let onBarClick: ((label: string) => void) | undefined
          if (chart.field === 'length') {
            onBarClick = (label: string) => {
              const parts = label.split('-').map(Number)
              if (parts.length === 2 && parts.every(n => !isNaN(n))) {
                onNavigateCurate({ lengthRange: [parts[0], parts[1]] })
              }
            }
          } else if (chart.field === 'tags') {
            onBarClick = (label: string) => {
              if (label !== '(no tags)') onNavigateCurate({ tag: label })
            }
          }
          return (
            <ChartPanel
              key={chart.field}
              chart={chart}
              color={CHART_COLORS[idx % CHART_COLORS.length]}
              fps={chart.field === 'length' ? fps : undefined}
              onBarClick={onBarClick}
              onBarContextMenu={(chart.field === 'length' || chart.field === 'tags')
                ? (label, x, y) => setContextMenu({ source: 'bar', label, field: chart.field, x, y })
                : undefined}
              intensity={chartIntensity}
              episodes={(chart.field === 'length' || chart.field === 'tags') ? episodes : undefined}
            />
          )
        })}

        {charts.length === 0 && !loading && (
          <div style={{ padding: 20, textAlign: 'center', color: 'var(--text-muted)', fontSize: 12 }}>
            Select fields from the left panel
          </div>
        )}
      </div>

      {contextMenu && (
        <div
          style={{
            position: 'fixed',
            left: contextMenu.x,
            top: contextMenu.y,
            background: 'var(--panel2)',
            border: '1px solid var(--border2)',
            borderRadius: 6,
            padding: '4px 0',
            zIndex: 1000,
            boxShadow: '0 4px 12px rgba(0,0,0,0.4)',
            minWidth: 140,
          }}
          onClick={e => e.stopPropagation()}
        >
          {contextMenu.source === 'bar' && (
            <button
              style={{
                display: 'block',
                width: '100%',
                padding: '6px 12px',
                background: 'none',
                border: 'none',
                color: 'var(--c-red)',
                fontSize: 12,
                textAlign: 'left',
                cursor: 'pointer',
              }}
              onMouseEnter={e => (e.currentTarget.style.background = 'var(--c-red-dim)')}
              onMouseLeave={e => (e.currentTarget.style.background = 'none')}
              onClick={() => openBulkBarModal(contextMenu)}
            >
              Mark as Bad
            </button>
          )}
          {contextMenu.source === 'grade-card' && (
            <>
              {bulkTargetsForCard(contextMenu.label).map(target => {
                const color =
                  target === 'good'   ? 'var(--c-green)'  :
                  target === 'normal' ? 'var(--c-yellow)' :
                                        'var(--c-red)'
                const hover =
                  target === 'good'   ? 'var(--c-green-dim)'  :
                  target === 'normal' ? 'var(--c-yellow-dim)' :
                                        'var(--c-red-dim)'
                const labelText =
                  target === 'good'   ? 'Mark all as Good'   :
                  target === 'normal' ? 'Mark all as Normal' :
                                        'Mark all as Bad'
                return (
                  <button
                    key={target}
                    style={{
                      display: 'block',
                      width: '100%',
                      padding: '6px 12px',
                      background: 'none',
                      border: 'none',
                      color,
                      fontSize: 12,
                      textAlign: 'left',
                      cursor: 'pointer',
                    }}
                    onMouseEnter={e => (e.currentTarget.style.background = hover)}
                    onMouseLeave={e => (e.currentTarget.style.background = 'none')}
                    onClick={() => applyBulkGrade(target)}
                  >
                    {labelText}
                  </button>
                )
              })}
            </>
          )}
        </div>
      )}

      <GradeReasonModal
        open={bulkReasonModal !== null}
        grade={bulkReasonModal?.targetGrade ?? 'bad'}
        initialReason=""
        episodeCount={bulkReasonModal?.episodeIndices.length}
        onSave={(reason) => void submitBulkGrade(bulkReasonModal!.targetGrade, reason)}
        onCancel={() => setBulkReasonModal(null)}
      />
    </div>
  )
}

/* ── Grade summary ────────────────────────────── */

function formatDuration(totalSeconds: number): string {
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const secs = Math.floor(totalSeconds % 60)
  if (hours > 0) return `${hours}h ${minutes}m ${secs}s`
  if (minutes > 0) return `${minutes}m ${secs}s`
  return `${secs}s`
}

function formatCompactDuration(totalSeconds: number): string {
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const secs = Math.floor(totalSeconds % 60)
  if (hours > 0) return `${hours}h${minutes}m`
  if (minutes > 0) return `${minutes}m${secs}s`
  return `${secs}s`
}

function GradeSummary({ chart, fps, episodes, onNavigateCurate, onCardContextMenu }: {
  chart: DistributionResult
  fps: number
  episodes: Episode[]
  onNavigateCurate: (filter: CurateFilter) => void
  onCardContextMenu: (currentKey: string, count: number, x: number, y: number) => void
}) {
  const total = chart.total
  const gradeMap: Record<string, number> = {}
  for (const bin of chart.bins) {
    gradeMap[bin.label] = bin.count
  }

  // Calculate per-grade duration from actual episodes
  const gradeDurations = useMemo(() => {
    const durations: Record<string, number> = { good: 0, normal: 0, bad: 0, '(ungraded)': 0, total: 0 }
    for (const ep of episodes) {
      const seconds = fps > 0 ? ep.length / fps : 0
      const key = ep.grade && ep.grade in durations ? ep.grade : '(ungraded)'
      durations[key] += seconds
      durations.total += seconds
    }
    return durations
  }, [episodes, fps])

  const items: { label: string; key: string; filterKey: GradeFilter; color: string }[] = [
    { label: 'Good', key: 'good', filterKey: 'good', color: 'var(--c-green)' },
    { label: 'Normal', key: 'normal', filterKey: 'normal', color: 'var(--c-yellow)' },
    { label: 'Bad', key: 'bad', filterKey: 'bad', color: 'var(--c-red)' },
    { label: 'Ungraded', key: '(ungraded)', filterKey: 'ungraded', color: 'var(--text-dim)' },
  ]

  return (
    <div style={{ background: 'var(--panel)', border: '1px solid var(--border)', borderRadius: 8 }}>
      <div style={{ fontSize: 10, color: 'var(--text-dim)', textTransform: 'uppercase', letterSpacing: '0.06em', padding: '12px 14px 6px' }}>
        Grade Summary
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 8, padding: '0 14px 10px' }}>
        {items.map(item => {
          const count = gradeMap[item.key] ?? 0
          const pct = total > 0 ? Math.round((count / total) * 100) : 0
          const dur = gradeDurations[item.key] ?? 0
          return (
            <div key={item.key} style={{
              background: 'var(--panel2)',
              border: `1px solid ${count > 0 ? item.color : 'var(--border)'}`,
              borderRadius: 8,
              padding: '12px 10px',
              textAlign: 'center',
              cursor: 'pointer',
              transition: 'transform 0.15s, border-color 0.15s',
            }}
              onClick={() => onNavigateCurate({ grade: item.filterKey })}
              onContextMenu={(e) => {
                if (count <= 0) return
                e.preventDefault()
                onCardContextMenu(item.key, count, e.clientX, e.clientY)
              }}
              onMouseEnter={e => {
                (e.currentTarget as HTMLElement).style.transform = 'scale(1.02)'
                ;(e.currentTarget as HTMLElement).style.borderColor = item.color
              }}
              onMouseLeave={e => {
                (e.currentTarget as HTMLElement).style.transform = 'scale(1)'
                ;(e.currentTarget as HTMLElement).style.borderColor = count > 0 ? item.color : 'var(--border)'
              }}
            >
              <div style={{ fontSize: 24, fontWeight: 700, color: item.color, lineHeight: 1, fontVariantNumeric: 'tabular-nums' }}>
                {count}
              </div>
              <div style={{ fontSize: 10, color: item.color, marginTop: 4, fontWeight: 600 }}>
                {item.label}
              </div>
              <div style={{ fontSize: 9, color: 'var(--text-dim)', marginTop: 2 }}>
                {pct}%
              </div>
              <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 6, borderTop: '1px solid var(--border)', paddingTop: 6 }}>
                <span style={{ fontWeight: 600, color: item.color }}>{formatDuration(dur)}</span>
              </div>
            </div>
          )
        })}
      </div>
      <div style={{ display: 'flex', height: 4, margin: '0 14px 12px', borderRadius: 2, overflow: 'hidden', gap: 1 }}>
        {items.map(item => {
          const count = gradeMap[item.key] ?? 0
          const pct = total > 0 ? (count / total) * 100 : 0
          if (pct === 0) return null
          return <div key={item.key} style={{ width: `${pct}%`, background: item.color, borderRadius: 1 }} />
        })}
      </div>
      {/* Total summary */}
      <div style={{ display: 'flex', justifyContent: 'flex-end', padding: '0 14px 12px', fontSize: 12, color: 'var(--text-muted)' }}>
        <span>Total: <strong style={{ color: 'var(--text)' }}>{formatDuration(gradeDurations.total)}</strong></span>
      </div>
    </div>
  )
}

/* ── Wrapped axis tick ────────────────────────── */

function WrappedTick({ x, y, payload, formatter }: {
  x?: number; y?: number; payload?: { value: string }
  formatter?: (label: string) => string
}) {
  if (!payload) return null
  const label = formatter ? formatter(payload.value) : payload.value
  const MAX_CHARS = 10
  if (label.length <= MAX_CHARS) {
    return (
      <text x={x} y={y} dy={12} textAnchor="middle" fontSize={11} fill="#999">
        {label}
      </text>
    )
  }
  // Split near the middle at a word boundary or hyphen
  const mid = Math.ceil(label.length / 2)
  let splitIdx = label.lastIndexOf(' ', mid)
  if (splitIdx <= 0) splitIdx = label.lastIndexOf('-', mid)
  if (splitIdx <= 0) splitIdx = mid
  const line1 = label.slice(0, splitIdx).trim()
  const line2 = label.slice(splitIdx).replace(/^[-\s]/, '').trim()
  return (
    <text x={x} y={y} textAnchor="middle" fontSize={10} fill="#999">
      <tspan x={x} dy={10}>{line1}</tspan>
      <tspan x={x} dy={13}>{line2}</tspan>
    </text>
  )
}

/* ── Grade tooltip ───────────────────────────── */

const GRADE_DOTS: { key: string; label: string; color: string }[] = [
  { key: 'good', label: 'Good', color: 'var(--c-green)' },
  { key: 'normal', label: 'Normal', color: 'var(--c-yellow)' },
  { key: 'bad', label: 'Bad', color: 'var(--c-red)' },
]

function GradeTooltip({ active, payload, label, field, episodes, formatLabel }: {
  active?: boolean
  payload?: { value: number }[]
  label?: string
  field: string
  episodes?: Episode[]
  formatLabel: (label: string) => string
}) {
  if (!active || !payload?.length || label == null) return null
  const count = payload[0].value
  const displayLabel = formatLabel(String(label))

  // Compute grade breakdown if episodes available
  let grades: { key: string; label: string; color: string; count: number }[] | null = null
  if (episodes && (field === 'length' || field === 'tags')) {
    let matched: Episode[]
    if (field === 'length') {
      const parts = String(label).split('-').map(Number)
      if (parts.length === 2 && parts.every(n => !isNaN(n))) {
        matched = episodes.filter(e => e.length >= parts[0] && e.length < parts[1])
      } else {
        matched = []
      }
    } else {
      matched = episodes.filter(e => e.tags.includes(String(label)))
    }
    grades = GRADE_DOTS.map(g => ({
      ...g,
      count: matched.filter(e => e.grade === g.key).length,
    }))
  }

  return (
    <div style={{
      background: '#161616',
      border: '1px solid #2a2a2a',
      borderRadius: 4,
      padding: '6px 10px',
      fontSize: 11,
      color: '#d9d9d9',
    }}>
      <div style={{ marginBottom: grades ? 4 : 0 }}>
        <span style={{ color: '#999' }}>{displayLabel}</span>
        <span style={{ marginLeft: 8, fontWeight: 600 }}>{count}</span>
      </div>
      {grades && (
        <div style={{ display: 'flex', gap: 8 }}>
          {grades.map(g => g.count > 0 && (
            <span key={g.key} style={{ color: g.color, fontSize: 10 }}>
              {g.label} {g.count}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

/* ── Chart panel ──────────────────────────────── */

function ChartPanel({ chart, color, fps, onBarClick, onBarContextMenu, intensity = 1, episodes }: {
  chart: DistributionResult
  color: string
  fps?: number
  onBarClick?: (label: string) => void
  onBarContextMenu?: (label: string, x: number, y: number) => void
  intensity?: number
  episodes?: Episode[]
}) {
  const gradientId = `gradient-${chart.field}`
  const hoveredLabelRef = useRef<string | null>(null)
  type ChartHoverState = { activeLabel?: string | number } | null | undefined

  const formatLabel = (label: string) => {
    if (!fps || chart.field !== 'length') return label
    const parts = label.split('-').map(Number)
    if (parts.length !== 2 || parts.some(isNaN)) return label
    return `${formatCompactDuration(parts[0] / fps)}-${formatCompactDuration(parts[1] / fps)}`
  }

  return (
    <div className="chart-panel">
      <div className="chart-panel-header">
        <span className="chart-panel-title">{FIELD_LABELS[chart.field] ?? chart.field}</span>
        <span style={{ fontSize: 9, color: 'var(--text-dim)' }}>{chart.total}</span>
      </div>
      <div className="chart-panel-body" onContextMenu={onBarContextMenu ? (e) => {
        if (hoveredLabelRef.current) {
          e.preventDefault()
          onBarContextMenu(hoveredLabelRef.current, e.clientX, e.clientY)
        }
      } : undefined}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart
            data={chart.bins}
            margin={{ top: 4, right: 4, bottom: 0, left: 0 }}
            onMouseMove={(state: ChartHoverState) => {
              hoveredLabelRef.current = typeof state?.activeLabel === 'string' ? state.activeLabel : null
            }}
            onMouseLeave={() => { hoveredLabelRef.current = null }}
          >
            <defs>
              <linearGradient id={gradientId} x1="0" y1="1" x2="0" y2="0">
                <stop offset="0%" stopColor={color} stopOpacity={intensity * 0.5} />
                <stop offset="100%" stopColor={color} stopOpacity={intensity * 0.16} />
              </linearGradient>
            </defs>
            <XAxis
              dataKey="label"
              tick={<WrappedTick formatter={formatLabel} />}
              axisLine={{ stroke: '#222' }}
              tickLine={false}
              height={40}
            />
            <YAxis
              tick={{ fontSize: 11, fill: '#999' }}
              axisLine={false}
              tickLine={false}
              width={30}
            />
            <Tooltip
              content={<GradeTooltip field={chart.field} episodes={episodes} formatLabel={formatLabel} />}
              cursor={false}
            />
            <Bar
              dataKey="count"
              fill={`url(#${gradientId})`}
              stroke={color}
              strokeOpacity={intensity * 0.8}
              radius={[2, 2, 0, 0]}
              cursor={onBarClick ? 'pointer' : undefined}
              onClick={onBarClick ? (data: BarRectangleItem) => {
                const label = data.payload?.label
                if (typeof label === 'string') onBarClick(label)
              } : undefined}
              activeBar={onBarClick ? { strokeOpacity: 0.8 } : undefined}
            />
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}
