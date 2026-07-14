import { useCallback, useEffect, useRef, useState } from 'react'
import client from '../../api/client'
import type { Episode } from '../../types/index'
import { formatEpisodeRanges, GRADE_OPTIONS, gradeColor } from './shared'
import { JobProgress, isCompleteStatus, useJobPoller } from './jobStatus'
import { trimStyles as s } from './styles'

type TargetMode = 'create' | 'merge'
type Target = { mode: TargetMode; path: string }

interface BrowseDirEntry {
  name: string
  path: string
  is_lerobot_dataset: boolean
}

interface BrowseDirsResponse {
  path: string
  parent: string | null
  roots: string[]
  entries: BrowseDirEntry[]
}

interface SummaryResponse {
  path: string
  total_episodes: number
  robot_type: string | null
  fps: number
  features_count: number
}

function formatSuggestedName(sourceDatasetName: string): string {
  const now = new Date()
  const yyyy = now.getFullYear()
  const mm = String(now.getMonth() + 1).padStart(2, '0')
  const dd = String(now.getDate()).padStart(2, '0')
  return `${sourceDatasetName}__out_${yyyy}${mm}${dd}`
}

function uniqueName(base: string, existing: Set<string>): string {
  if (!existing.has(base)) return base
  let i = 2
  while (existing.has(`${base}_${i}`)) i += 1
  return `${base}_${i}`
}

function joinChildPath(parent: string, name: string): string {
  return parent === '/' ? `/${name}` : `${parent}/${name}`
}

function DestinationPicker({
  sourceDatasetName,
  value,
  onChange,
  disabled,
}: {
  sourceDatasetName: string
  value: Target | null
  onChange: (t: Target | null) => void
  disabled?: boolean
}) {
  const [currentDir, setCurrentDir] = useState<string | null>(null)
  const [parent, setParent] = useState<string | null>(null)
  const [entries, setEntries] = useState<BrowseDirEntry[]>([])
  const [createOpen, setCreateOpen] = useState(false)
  const [newDatasetName, setNewDatasetName] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const browseRequestRef = useRef(0)

  const fetchDir = useCallback(async (path: string | null) => {
    const requestId = browseRequestRef.current + 1
    browseRequestRef.current = requestId
    setLoading(true)
    setError(null)
    setActionError(null)
    try {
      const url = path
        ? `/datasets/browse-dirs?path=${encodeURIComponent(path)}`
        : '/datasets/browse-dirs'
      const resp = await client.get<BrowseDirsResponse>(url)
      if (requestId !== browseRequestRef.current) return
      setCurrentDir(resp.data.path)
      setParent(resp.data.parent)
      setEntries(resp.data.entries)
    } catch (err: unknown) {
      if (requestId !== browseRequestRef.current) return
      const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? 'Failed to load directory'
      setError(msg)
    } finally {
      if (requestId !== browseRequestRef.current) return
      setLoading(false)
    }
  }, [])

  useEffect(() => { void fetchDir(null) }, [fetchDir])

  const existingNames = new Set(entries.map(entry => entry.name))
  const suggestedName = uniqueName(formatSuggestedName(sourceDatasetName), existingNames)
  const trimmedDatasetName = newDatasetName.trim()
  const hasPathSeparators = /[\\/]/.test(trimmedDatasetName)
  const isReservedDatasetName = trimmedDatasetName === '.' || trimmedDatasetName === '..'
  const invalidDatasetName = trimmedDatasetName.length > 0 && (hasPathSeparators || isReservedDatasetName)
  const hasDuplicateName = trimmedDatasetName.length > 0 && existingNames.has(trimmedDatasetName)
  const resolvedDatasetName = trimmedDatasetName.length > 0
    ? uniqueName(trimmedDatasetName, existingNames)
    : ''
  const canConfirmCreate = Boolean(currentDir) && trimmedDatasetName.length > 0 && !invalidDatasetName && !disabled

  const breadcrumb = currentDir ? currentDir.split('/').filter(Boolean) : []

  const openCreate = () => {
    setCreateOpen(true)
    setNewDatasetName(suggestedName)
    setActionError(null)
  }

  const confirmCreate = () => {
    if (!currentDir) {
      setActionError('Choose a destination directory first')
      return
    }
    if (!trimmedDatasetName) {
      setActionError('Enter a dataset name')
      return
    }
    if (invalidDatasetName) {
      setActionError('Enter a single dataset name, not a path')
      return
    }

    onChange({ mode: 'create', path: joinChildPath(currentDir, resolvedDatasetName) })
    setCreateOpen(false)
    setActionError(null)
  }

  return (
    <div style={s.pickerBox}>
      <div style={s.pickerBreadcrumb}>
        <span style={s.pickerPathRoot}>/</span>
        {breadcrumb.map((seg, i) => (
          <span key={i} style={s.pickerPathSeg}>
            {seg}
            {i < breadcrumb.length - 1 && <span style={s.pickerPathSep}>/</span>}
          </span>
        ))}
      </div>

      <div style={s.pickerList}>
        {loading && <div style={s.pickerHint}>Loading…</div>}
        {!loading && parent && (
          <button
            style={s.pickerEntry}
            onClick={() => void fetchDir(parent)}
            disabled={disabled}
            type="button"
          >
            <span style={s.pickerIcon}>↑</span>
            <span>..</span>
          </button>
        )}
        {!loading && entries.length === 0 && !parent && (
          <div style={s.pickerHint}>No subdirectories</div>
        )}
        {!loading && entries.map(entry => (
          <button
            key={entry.path}
            style={{
              ...s.pickerEntry,
              ...(entry.is_lerobot_dataset && value?.mode === 'merge' && value.path === entry.path ? s.pickerEntrySelected : {}),
            }}
            onClick={() => {
              if (entry.is_lerobot_dataset) {
                onChange({ mode: 'merge', path: entry.path })
                setCreateOpen(false)
                setActionError(null)
                return
              }
              void fetchDir(entry.path)
            }}
            disabled={disabled}
            type="button"
          >
            <span style={s.pickerIcon}>{entry.is_lerobot_dataset ? '◆' : '▸'}</span>
            <span>{entry.name}</span>
          </button>
        ))}
        {error && <div style={s.errorText}>{error}</div>}
      </div>

      <div style={s.pickerCreateRow}>
        {!createOpen ? (
          <button
            style={s.pickerCreateToggleBtn}
            onClick={openCreate}
            disabled={disabled || !currentDir}
            type="button"
          >
            Create new dataset here
          </button>
        ) : (
          <>
            <span style={s.pickerNewLabel}>New dataset name</span>
            <input
              style={s.textInput}
              type="text"
              value={newDatasetName}
              onChange={e => {
                setNewDatasetName(e.target.value)
                setActionError(null)
              }}
              disabled={disabled}
            />
            {trimmedDatasetName.length === 0 ? (
              <div style={s.pickerCreateHint}>Enter a dataset name inside the current directory.</div>
            ) : invalidDatasetName ? (
              <div style={s.errorText}>Use a single dataset name without path separators, `.` or `..`.</div>
            ) : hasDuplicateName ? (
              <div style={s.pickerCreateHint}>
                {joinChildPath(currentDir ?? '/', trimmedDatasetName)} already exists here, so {joinChildPath(currentDir ?? '/', resolvedDatasetName)} will be used instead.
              </div>
            ) : currentDir ? (
              <div style={s.pickerCreateHint}>{joinChildPath(currentDir, trimmedDatasetName)}</div>
            ) : null}
            <div style={s.pickerCreateActions}>
              <button
                style={{ ...s.pickerCreateConfirmBtn, opacity: canConfirmCreate ? 1 : 0.6 }}
                onClick={confirmCreate}
                disabled={!canConfirmCreate}
                type="button"
              >
                Select Create Target
              </button>
              <button
                style={s.refreshBtn}
                onClick={() => {
                  setCreateOpen(false)
                  setActionError(null)
                }}
                disabled={disabled}
                type="button"
              >
                Cancel
              </button>
            </div>
          </>
        )}
      </div>

      {actionError && <div style={s.errorText}>{actionError}</div>}
    </div>
  )
}

function TargetSummary({ target }: { target: Target }) {
  const [summary, setSummary] = useState<SummaryResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (target.mode === 'create') {
      setSummary(null)
      setLoading(false)
      setError(null)
      return
    }

    let cancelled = false

    const fetchSummary = async () => {
      setLoading(true)
      setError(null)
      setSummary(null)
      try {
        const resp = await client.get<SummaryResponse>(`/datasets/summary?path=${encodeURIComponent(target.path)}`)
        if (!cancelled) {
          setSummary(resp.data)
        }
      } catch (err: unknown) {
        if (cancelled) return
        const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? 'Failed to load dataset summary'
        setSummary(null)
        setError(msg)
      } finally {
        if (!cancelled) {
          setLoading(false)
        }
      }
    }

    void fetchSummary()

    return () => {
      cancelled = true
    }
  }, [target])

  if (target.mode === 'create') {
    return (
      <div style={s.targetSummaryBox}>
        <div style={s.targetSummaryHead}>Create new Out dataset</div>
        <div style={s.targetSummaryPath}>{target.path}</div>
        <div style={s.targetSummaryCaption}>Selected episodes will be written into a new dataset at this path.</div>
      </div>
    )
  }

  return (
    <div style={s.targetSummaryBox}>
      <div style={s.targetSummaryHead}>Merge into existing dataset</div>
      <div style={s.targetSummaryPath}>{target.path}</div>
      {loading && <div style={s.targetSummaryCaption}>Loading dataset summary...</div>}
      {error && <div style={s.errorText}>{error}</div>}
      {!loading && !error && summary && (
        <>
          <div style={s.targetSummaryMetrics}>
            <div style={s.targetMetric}>
              <span style={s.targetMetricLabel}>Episodes</span>
              <span style={s.targetMetricValue}>{summary.total_episodes}</span>
            </div>
            <div style={s.targetMetric}>
              <span style={s.targetMetricLabel}>Robot</span>
              <span style={s.targetMetricValue}>{summary.robot_type ?? 'Unknown'}</span>
            </div>
            <div style={s.targetMetric}>
              <span style={s.targetMetricLabel}>FPS</span>
              <span style={s.targetMetricValue}>{summary.fps}</span>
            </div>
            <div style={s.targetMetric}>
              <span style={s.targetMetricLabel}>Features</span>
              <span style={s.targetMetricValue}>{summary.features_count}</span>
            </div>
          </div>
          <div style={s.targetSummaryCaption}>Existing dataset metadata loaded from `meta/info.json`.</div>
        </>
      )}
    </div>
  )
}

export function OutWorkflow({
  datasetPath,
  episodes,
}: {
  datasetPath: string | null
  episodes: Episode[]
}) {
  const [selectedGrades, setSelectedGrades] = useState<Set<string>>(new Set())
  const [selectedTags, setSelectedTags] = useState<Set<string>>(new Set())
  const [target, setTarget] = useState<Target | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const { jobStatus, polling, startPolling, reset } = useJobPoller()

  useEffect(() => {
    setSelectedGrades(new Set())
    setSelectedTags(new Set())
    setTarget(null)
    setSubmitting(false)
    setSubmitError(null)
    reset()
  }, [datasetPath, reset])

  const allTags = Array.from(new Set(episodes.flatMap(e => e.tags ?? []))).sort()
  const matchingEpisodes = episodes
    .filter(e => selectedGrades.size === 0 || selectedGrades.has(e.grade ?? 'Ungraded'))
    .filter(e => selectedTags.size === 0 || (e.tags ?? []).some(t => selectedTags.has(t)))

  const toggleGrade = (grade: string) => {
    setSelectedGrades(prev => {
      const next = new Set(prev)
      if (next.has(grade)) next.delete(grade)
      else next.add(grade)
      return next
    })
  }

  const toggleTag = (tag: string) => {
    setSelectedTags(prev => {
      const next = new Set(prev)
      if (next.has(tag)) next.delete(tag)
      else next.add(tag)
      return next
    })
  }

  const handleSubmit = async () => {
    if (!datasetPath) return
    if (matchingEpisodes.length === 0) {
      setSubmitError('No episodes match the selected grade and tag filters')
      return
    }
    if (!target) {
      setSubmitError('Choose where the Out dataset should be created or merged')
      return
    }

    setSubmitting(true)
    setSubmitError(null)
    reset()

    try {
      const resp = await client.post<{ job_id: string; operation: string; status: string }>('/datasets/split-into', {
        source_path: datasetPath,
        episode_ids: matchingEpisodes.map(e => e.episode_index).sort((a, b) => a - b),
        destination_path: target.path,
      })
      startPolling(resp.data.job_id)
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? 'Out run failed'
      setSubmitError(msg)
    } finally {
      setSubmitting(false)
    }
  }

  if (!datasetPath) {
    return <div style={s.emptyState}>Load a dataset first to prepare an Out dataset.</div>
  }

  const datasetSegments = datasetPath.split('/').filter(Boolean)
  const sourceDatasetName = datasetSegments[datasetSegments.length - 1] ?? 'dataset'
  const syncComplete = isCompleteStatus(jobStatus?.status)
  const submitDisabled = submitting || polling || !target || matchingEpisodes.length === 0

  return (
    <div style={s.tabContent}>
      <div style={s.fieldLabel}>Filter by Grade</div>
      <div style={s.chipRow}>
        {GRADE_OPTIONS.map(grade => {
          const active = selectedGrades.has(grade)
          const color = gradeColor(grade)
          return (
            <button
              key={grade}
              style={{
                ...s.chip,
                borderColor: active ? color : 'var(--border3)',
                color: active ? color : 'var(--text-dim)',
                background: active ? `${color}18` : 'transparent',
              }}
              onClick={() => toggleGrade(grade)}
              type="button"
            >
              {grade}
            </button>
          )
        })}
      </div>

      <div style={s.fieldLabel}>Filter by Tag</div>
      {allTags.length === 0 ? (
        <div style={s.empty}>No tags found in this dataset.</div>
      ) : (
        <div style={s.chipRow}>
          {allTags.map(tag => {
            const active = selectedTags.has(tag)
            return (
              <button
                key={tag}
                style={{
                  ...s.chip,
                  borderColor: active ? 'var(--interactive)' : 'var(--border3)',
                  color: active ? 'var(--interactive)' : 'var(--text-dim)',
                  background: active ? 'var(--interactive-dim)' : 'transparent',
                }}
                onClick={() => toggleTag(tag)}
                type="button"
              >
                {tag}
              </button>
            )
          })}
        </div>
      )}

      <div style={s.matchPreview}>
        <span style={{ color: matchingEpisodes.length > 0 ? 'var(--interactive)' : 'var(--c-red)' }}>
          {matchingEpisodes.length} episode{matchingEpisodes.length !== 1 ? 's' : ''} selected for Out
        </span>
        {matchingEpisodes.length > 0 ? (
          <>
            <div style={s.matchRanges}>
              {formatEpisodeRanges(matchingEpisodes.map(e => e.episode_index))}
            </div>
            <span style={s.targetSummaryCaption}>
              {selectedGrades.size === 0 && selectedTags.size === 0
                ? 'No filters selected, so all episodes will be included.'
                : 'Episodes must match every active grade and tag filter.'}
            </span>
          </>
        ) : (
          <span style={s.errorText}>
            No episodes match the current filters. Clear one or more filters to enable Run Out.
          </span>
        )}
      </div>

      <div style={s.fieldLabel}>Destination</div>
      <DestinationPicker
        sourceDatasetName={sourceDatasetName}
        value={target}
        onChange={setTarget}
        disabled={submitting || polling}
      />
      {target && <TargetSummary target={target} />}

      {submitError && <div style={s.errorText}>{submitError}</div>}

      {syncComplete && jobStatus?.summary && (
        <div style={s.matchPreview}>
          <span style={{ color: 'var(--c-green)' }}>
            {jobStatus.summary.created} copied, {jobStatus.summary.skipped_duplicates} skipped as duplicates
          </span>
          <span style={{ color: 'var(--text-muted)' }}>
            Mode: {jobStatus.summary.mode}
          </span>
        </div>
      )}

      <button
        style={{ ...s.actionBtn, opacity: submitDisabled ? 0.6 : 1 }}
        onClick={handleSubmit}
        disabled={submitDisabled}
      >
        {submitting ? 'Submitting...' : 'Run Out'}
      </button>

      <JobProgress jobStatus={jobStatus} polling={polling} />
    </div>
  )
}
