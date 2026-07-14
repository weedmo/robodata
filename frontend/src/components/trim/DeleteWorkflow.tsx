import { useState } from 'react'
import client from '../../api/client'
import type { Episode } from '../../types/index'
import { formatEpisodeRanges, GRADE_OPTIONS, gradeColor, type SplitMode } from './shared'
import { JobProgress, useJobPoller } from './jobStatus'
import { trimStyles as s } from './styles'

export function DeleteWorkflow({
  datasetPath,
  episodes,
}: {
  datasetPath: string | null
  episodes: Episode[]
}) {
  const [splitMode, setSplitMode] = useState<SplitMode>('grade')
  const [selectedGrades, setSelectedGrades] = useState<Set<string>>(new Set())
  const [selectedTags, setSelectedTags] = useState<Set<string>>(new Set())
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const { jobStatus, polling, startPolling, reset } = useJobPoller()

  const allTags = Array.from(new Set(episodes.flatMap(e => e.tags ?? []))).sort()

  const matchingEpisodes = splitMode === 'grade'
    ? episodes.filter(e => selectedGrades.has(e.grade ?? 'Ungraded'))
    : episodes.filter(e => (e.tags ?? []).some(t => selectedTags.has(t)))

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
    if (matchingEpisodes.length === 0) { setSubmitError('No episodes match the selected filter'); return }
    if (matchingEpisodes.length === episodes.length) { setSubmitError('Cannot delete all episodes'); return }

    setSubmitting(true)
    setSubmitError(null)
    reset()

    try {
      const episodeIds = matchingEpisodes.map(e => e.episode_index).sort((a, b) => a - b)
      const resp = await client.post<{ job_id: string; operation: string; status: string }>('/datasets/delete', {
        source_path: datasetPath,
        episode_ids: episodeIds,
      })
      startPolling(resp.data.job_id)
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? 'Delete failed'
      setSubmitError(msg)
    } finally {
      setSubmitting(false)
    }
  }

  if (!datasetPath) {
    return <div style={s.emptyState}>Load a dataset first to delete episodes.</div>
  }

  return (
    <div style={s.tabContent}>
      <div style={s.fieldLabel}>Delete By</div>
      <div style={s.modeToggle}>
        {(['grade', 'tag'] as SplitMode[]).map(mode => (
          <button
            key={mode}
            style={{ ...s.modeBtn, ...(splitMode === mode ? s.modeBtnActive : {}) }}
            onClick={() => setSplitMode(mode)}
          >
            {mode === 'grade' ? 'Grade' : 'Tag'}
          </button>
        ))}
      </div>

      {splitMode === 'grade' && (
        <>
          <div style={s.fieldLabel}>Select grades to delete</div>
          <div style={s.chipRow}>
            {GRADE_OPTIONS.map(grade => {
              const active = selectedGrades.has(grade)
              const color = gradeColor(grade)
              return (
                <button
                  key={grade}
                  style={{
                    ...s.chip,
                    borderColor: active ? color : '#333',
                    color: active ? color : '#666',
                    background: active ? `${color}18` : 'transparent',
                  }}
                  onClick={() => toggleGrade(grade)}
                >
                  {grade}
                </button>
              )
            })}
          </div>
        </>
      )}

      {splitMode === 'tag' && (
        <>
          <div style={s.fieldLabel}>Select tags to delete</div>
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
                      borderColor: active ? 'var(--c-red)' : 'var(--border3)',
                      color: active ? 'var(--c-red)' : 'var(--text-dim)',
                      background: active ? 'var(--c-red-dim)' : 'transparent',
                    }}
                    onClick={() => toggleTag(tag)}
                  >
                    {tag}
                  </button>
                )
              })}
            </div>
          )}
        </>
      )}

      <div style={s.matchPreview}>
        <span style={{ color: matchingEpisodes.length > 0 ? 'var(--c-red)' : 'var(--text-dim)' }}>
          {matchingEpisodes.length} episode{matchingEpisodes.length !== 1 ? 's' : ''} will be deleted
        </span>
        {matchingEpisodes.length > 0 && (
          <div style={s.matchRanges}>
            {formatEpisodeRanges(matchingEpisodes.map(e => e.episode_index))}
          </div>
        )}
        {matchingEpisodes.length > 0 && (
          <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
            {episodes.length - matchingEpisodes.length} episode{episodes.length - matchingEpisodes.length !== 1 ? 's' : ''} will remain
          </span>
        )}
      </div>

      {submitError && <div style={s.errorText}>{submitError}</div>}

      <button
        style={{ ...s.actionBtn, background: 'var(--c-red)', opacity: submitting || polling ? 0.6 : 1 }}
        onClick={handleSubmit}
        disabled={submitting || polling}
      >
        {submitting ? 'Submitting...' : 'Delete Episodes'}
      </button>

      <JobProgress jobStatus={jobStatus} polling={polling} />
    </div>
  )
}
