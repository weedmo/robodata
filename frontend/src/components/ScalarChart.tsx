import { useEffect, useMemo, useState, useRef, memo } from 'react'
import client from '../api/client'
import { useThemeVersion } from '../hooks/useThemeVersion'

interface ScalarData {
  episode_index: number
  num_frames: number
  observations: Record<string, number[]>
  actions: Record<string, number[]>
  terminal_frames?: number[]
  terminal_timestamps?: number[]
}

interface ScalarChartProps {
  datasetPath: string
  episodeIndex: number | null
  currentFrame: number
  onTerminalFrames?: (frames: number[], timestamps: number[]) => void
}

type BandLevel = 'moderate' | 'severe'

interface RatioBand {
  start: number  // inclusive frame index
  end: number    // inclusive frame index
  level: BandLevel
}

// Tuned against labelled good/normal/bad episodes:
// - >0.30 for a sustained stretch distinguishes bad-grade joints (e.g. joint[13])
//   from good-grade noise; good episodes stay at 0% above this ratio.
// - 0.15–0.30 flags "worth inspecting" without over-firing on fast-motion lag.
// - MIN_SEVERE_RUN filters single-frame gripper transitions (action 0→1 step vs
//   physical gripper lag) which spike >0.30 but aren't a data-quality concern.
const MODERATE_RATIO = 0.15
const SEVERE_RATIO = 0.30
const MIN_SEVERE_RUN = 5

function classify(ratio: number): BandLevel | null {
  if (ratio > SEVERE_RATIO) return 'severe'
  if (ratio > MODERATE_RATIO) return 'moderate'
  return null
}

function rangeOf(series: number[]): number {
  if (series.length === 0) return 0
  let min = series[0]
  let max = series[0]
  for (let i = 1; i < series.length; i++) {
    const v = series[i]
    if (v < min) min = v
    if (v > max) max = v
  }
  return max - min
}

/**
 * Pairwise obs/action divergence bands for a single joint.
 * Returns merged runs of consecutive frames at the same band level.
 * Returns [] if either input is empty or combined range is 0.
 */
function computeBands(obs: number[], act: number[]): RatioBand[] {
  const len = Math.min(obs.length, act.length)
  if (len === 0) return []
  const range = Math.max(rangeOf(obs), rangeOf(act))
  if (range === 0) return []

  const bands: RatioBand[] = []
  let curLevel: BandLevel | null = null
  let curStart = 0

  for (let i = 0; i < len; i++) {
    const ratio = Math.abs(act[i] - obs[i]) / range
    const level = classify(ratio)
    if (level !== curLevel) {
      if (curLevel !== null) {
        bands.push({ start: curStart, end: i - 1, level: curLevel })
      }
      curLevel = level
      curStart = i
    }
  }
  if (curLevel !== null) {
    bands.push({ start: curStart, end: len - 1, level: curLevel })
  }

  // Downgrade short severe runs to moderate so transient gripper spikes
  // don't read as "data-quality problem". Then re-merge adjacent same-level runs.
  for (const b of bands) {
    if (b.level === 'severe' && b.end - b.start + 1 < MIN_SEVERE_RUN) {
      b.level = 'moderate'
    }
  }
  const merged: RatioBand[] = []
  for (const b of bands) {
    const last = merged[merged.length - 1]
    if (last && last.level === b.level && last.end + 1 === b.start) {
      last.end = b.end
    } else {
      merged.push({ ...b })
    }
  }
  return merged
}

/**
 * Reduce an observation/action key to its pair-matching identifier.
 * Handles two forms produced by /api/scalars/:idx:
 *   observation.state[0] <-> action[0]       → "[0]"
 *   observation.state.joint1 <-> action.joint1 → "joint1"
 */
function unifyKey(key: string): string {
  const idxMatch = /\[(\d+)\]$/.exec(key)
  if (idxMatch) return idxMatch[0]
  return key
    .replace(/^observation\.state\.?/, '')
    .replace(/^observation\./, '')
    .replace(/^action\.?/, '')
}

const MiniChart = memo(function MiniChart({ label, series, color, currentFrame, collapsed, themeVersion, bands }: {
  label: string
  series: number[]
  color: string
  currentFrame: number
  collapsed: boolean
  themeVersion: number
  bands?: RatioBand[]
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const stats = useMemo(() => {
    if (series.length === 0) return { min: 0, max: 0, range: 1 }
    let min = series[0]
    let max = series[0]
    for (let i = 1; i < series.length; i++) {
      const value = series[i]
      if (value < min) min = value
      if (value > max) max = value
    }
    return { min, max, range: max - min || 1 }
  }, [series])

  useEffect(() => {
    if (collapsed) return
    const canvas = canvasRef.current
    if (!canvas || series.length === 0) return

    const draw = () => {
      const ctx = canvas.getContext('2d')
      if (!ctx) return

      const dpr = window.devicePixelRatio || 1
      const w = canvas.clientWidth
      const h = canvas.clientHeight
      if (w === 0 || h === 0) return

      canvas.width = w * dpr
      canvas.height = h * dpr
      ctx.scale(dpr, dpr)

      const cs = getComputedStyle(document.documentElement)
      const bg = cs.getPropertyValue('--bg-deep').trim()
      const gridColor = cs.getPropertyValue('--border').trim()
      const resolvedColor = (() => {
        const m = /^var\((--[\w-]+)\)$/.exec(color.trim())
        if (!m) return color
        const v = cs.getPropertyValue(m[1]).trim()
        return v || color
      })()

      // Background
      ctx.fillStyle = bg
      ctx.fillRect(0, 0, w, h)

      // Divergence bands (paint moderate first so severe overlaps win)
      if (bands && bands.length > 0 && series.length > 1) {
        const denomBand = Math.max(series.length - 1, 1)
        const moderateFill = cs.getPropertyValue('--c-yellow').trim()
        const severeFill = cs.getPropertyValue('--c-red').trim()
        ctx.save()
        ctx.globalAlpha = 0.28
        for (const level of ['moderate', 'severe'] as const) {
          const fill = level === 'moderate' ? moderateFill : severeFill
          if (!fill) continue
          ctx.fillStyle = fill
          for (const b of bands) {
            if (b.level !== level) continue
            const x0 = (b.start / denomBand) * w
            const x1 = ((b.end + 1) / denomBand) * w
            ctx.fillRect(x0, 0, Math.max(x1 - x0, 1), h)
          }
        }
        ctx.restore()
      }

      // Grid lines
      ctx.strokeStyle = gridColor
      ctx.lineWidth = 1
      for (let i = 0; i < 4; i++) {
        const y = (h / 4) * i
        ctx.beginPath()
        ctx.moveTo(0, y)
        ctx.lineTo(w, y)
        ctx.stroke()
      }

      // Data line
      ctx.strokeStyle = resolvedColor
      ctx.lineWidth = 1.5
      ctx.beginPath()
      const denom = Math.max(series.length - 1, 1)
      for (let i = 0; i < series.length; i++) {
        const x = (i / denom) * w
        const y = h - ((series[i] - stats.min) / stats.range) * (h - 4) - 2
        if (i === 0) ctx.moveTo(x, y)
        else ctx.lineTo(x, y)
      }
      ctx.stroke()
    }

    draw()
    const ro = new ResizeObserver(draw)
    ro.observe(canvas)
    return () => ro.disconnect()
  }, [series, color, collapsed, themeVersion, bands, stats])

  const currentVal = currentFrame >= 0 && currentFrame < series.length
    ? series[currentFrame].toFixed(3)
    : '--'
  const showCursor = !collapsed && currentFrame >= 0 && currentFrame < series.length
  const cursorLeft = series.length > 1 ? `${(currentFrame / (series.length - 1)) * 100}%` : '0%'
  const cursorTop = showCursor
    ? `${100 - ((series[currentFrame] - stats.min) / stats.range) * 100}%`
    : '50%'

  return (
    <div style={chartStyles.chartItem}>
      <div style={chartStyles.chartHeader}>
        <span style={{ ...chartStyles.chartLabel, color }}>{label}</span>
        <span style={chartStyles.chartValue}>{currentVal}</span>
      </div>
      {!collapsed && (
        <div style={chartStyles.canvasWrap}>
          <canvas
            ref={canvasRef}
            style={chartStyles.canvas}
          />
          {showCursor && (
            <>
              <div style={{ ...chartStyles.cursorLine, left: cursorLeft }} />
              <div style={{ ...chartStyles.cursorDot, left: cursorLeft, top: cursorTop }} />
            </>
          )}
        </div>
      )}
    </div>
  )
})

export function ScalarChart({ datasetPath, episodeIndex, currentFrame, onTerminalFrames }: ScalarChartProps) {
  const [data, setData] = useState<ScalarData | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [obsCollapsed, setObsCollapsed] = useState(false)
  const [actCollapsed, setActCollapsed] = useState(false)
  const themeVersion = useThemeVersion()

  useEffect(() => {
    if (episodeIndex === null) {
      setData(null)
      setError(null)
      onTerminalFrames?.([], [])
      return
    }
    setLoading(true)
    setError(null)
    client.get<ScalarData>(`/scalars/${episodeIndex}`, {
      params: { dataset_path: datasetPath },
    })
      .then(res => {
        setData(res.data)
        onTerminalFrames?.(res.data.terminal_frames ?? [], res.data.terminal_timestamps ?? [])
      })
      .catch(err => {
        setData(null)
        setError(err?.message || 'Failed to load scalar data')
        onTerminalFrames?.([], [])
      })
      .finally(() => setLoading(false))
  }, [datasetPath, episodeIndex]) // eslint-disable-line react-hooks/exhaustive-deps

  const bandsByName = useMemo(() => {
    const map = new Map<string, RatioBand[]>()
    if (!data) return map
    const actByName = new Map<string, number[]>()
    for (const k of Object.keys(data.actions)) actByName.set(unifyKey(k), data.actions[k])
    for (const k of Object.keys(data.observations)) {
      const name = unifyKey(k)
      const act = actByName.get(name)
      if (!act) continue
      const bands = computeBands(data.observations[k], act)
      if (bands.length > 0) map.set(name, bands)
    }
    return map
  }, [data])

  if (episodeIndex === null) return null

  if (loading) {
    return (
      <div style={chartStyles.container}>
        <div style={chartStyles.loading}>Loading scalar data...</div>
      </div>
    )
  }

  if (error) {
    return (
      <div style={chartStyles.container}>
        <div style={chartStyles.error}>Scalar data unavailable</div>
      </div>
    )
  }

  if (!data) return null

  const obsKeys = Object.keys(data.observations)
  const actKeys = Object.keys(data.actions)

  if (obsKeys.length === 0 && actKeys.length === 0) return null

  return (
    <div style={chartStyles.container}>
      <div style={chartStyles.columns}>
        {obsKeys.length > 0 && (
          <div style={chartStyles.column}>
            <div
              role="button"
              tabIndex={0}
              style={chartStyles.sectionHeader}
              onClick={() => setObsCollapsed(!obsCollapsed)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault()
                  setObsCollapsed(!obsCollapsed)
                }
              }}
            >
              <span style={chartStyles.sectionTitle}>
                {obsCollapsed ? '\u25B6' : '\u25BC'} Observation.state
              </span>
              <span style={chartStyles.sectionCount}>{obsKeys.length}</span>
            </div>
            {obsKeys.map(key => {
              const name = key.replace('observation.', '').replace('state.', '')
              return (
                <MiniChart
                  key={key}
                  label={name}
                  series={data.observations[key]}
                  color="var(--c-blue)"
                  currentFrame={currentFrame}
                  collapsed={obsCollapsed}
                  themeVersion={themeVersion}
                  bands={bandsByName.get(unifyKey(key))}
                />
              )
            })}
          </div>
        )}

        {actKeys.length > 0 && (
          <div style={chartStyles.column}>
            <div
              role="button"
              tabIndex={0}
              style={chartStyles.sectionHeader}
              onClick={() => setActCollapsed(!actCollapsed)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault()
                  setActCollapsed(!actCollapsed)
                }
              }}
            >
              <span style={chartStyles.sectionTitle}>
                {actCollapsed ? '\u25B6' : '\u25BC'} Action
              </span>
              <span style={chartStyles.sectionCount}>{actKeys.length}</span>
            </div>
            {actKeys.map(key => {
              const name = key.replace('action.', '')
              return (
                <MiniChart
                  key={key}
                  label={name}
                  series={data.actions[key]}
                  color="var(--accent)"
                  currentFrame={currentFrame}
                  collapsed={actCollapsed}
                  themeVersion={themeVersion}
                  bands={bandsByName.get(unifyKey(key))}
                />
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}

const chartStyles: Record<string, React.CSSProperties> = {
  container: { display: 'flex', flexDirection: 'column', overflow: 'hidden', flexShrink: 1 },
  loading: { padding: '12px', fontSize: '12px', color: 'var(--text-muted)' as string },
  error: { padding: '12px', fontSize: '12px', color: 'var(--c-red)' as string },
  columns: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 0, borderBottom: '1px solid var(--border)' as string },
  column: { display: 'flex', flexDirection: 'column', minWidth: 0, borderRight: '1px solid var(--border)' as string },
  sectionHeader: {
    display: 'flex', justifyContent: 'space-between', alignItems: 'center',
    padding: '6px 10px', cursor: 'pointer',
    background: 'var(--panel)' as string,
    borderBottom: '1px solid var(--border2)' as string,
  },
  sectionTitle: { fontSize: '11px', fontWeight: 600, textTransform: 'uppercase' as const, letterSpacing: '0.06em', color: 'var(--text-muted)' as string, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' as const },
  sectionCount: { fontSize: '11px', color: 'var(--text-dim)' as string, fontFamily: 'var(--font-mono)' },
  chartItem: { padding: '3px 10px', borderBottom: '1px solid var(--border)', minWidth: 0 },
  chartHeader: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '2px', gap: '6px' },
  chartLabel: { fontSize: '11px', fontFamily: 'var(--font-mono)', fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' as const, minWidth: 0 },
  chartValue: { fontSize: '11px', fontFamily: 'var(--font-mono)', color: 'var(--text-muted)' as string, flexShrink: 0 },
  canvasWrap: { position: 'relative', width: '100%', height: '40px', overflow: 'hidden', borderRadius: '2px' },
  canvas: { width: '100%', height: '40px', borderRadius: '2px', display: 'block' },
  cursorLine: {
    position: 'absolute',
    top: 0,
    bottom: 0,
    width: '1px',
    borderLeft: '1px dashed var(--text)',
    opacity: 0.85,
    pointerEvents: 'none',
    transform: 'translateX(-0.5px)',
  },
  cursorDot: {
    position: 'absolute',
    width: '6px',
    height: '6px',
    borderRadius: '50%',
    background: 'currentColor',
    color: 'var(--text)',
    pointerEvents: 'none',
    transform: 'translate(-50%, -50%)',
  },
}
