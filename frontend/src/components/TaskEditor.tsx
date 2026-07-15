import { useCallback, useEffect, useState } from 'react'
import { useEpisodeInstruction, InstructionRequestError } from '../hooks/useEpisodeInstruction'
import { useTasks } from '../hooks/useTasks'
import type { Episode, InstructionEditMode, InstructionPreview, InstructionUpdateResult } from '../types'

interface TaskEditorProps {
  datasetPath: string
  episode: Episode | null
  readOnly?: boolean
  onInstructionUpdated: (result: InstructionUpdateResult) => Promise<void> | void
}

const actionLabel: Record<InstructionPreview['action'], string> = {
  no_op: 'No dataset change is needed.',
  reuse: 'This episode will be reassigned to the existing matching instruction.',
  create: 'A new task will be created and assigned to this episode.',
  shared: 'The current shared task instruction will be renamed.',
}

export function TaskEditor({ datasetPath, episode, readOnly = false, onInstructionUpdated }: TaskEditorProps) {
  const { tasks, fetchTasks } = useTasks(datasetPath)
  const { preview: requestPreview, commit, previewing, saving } = useEpisodeInstruction(datasetPath)
  const [instruction, setInstruction] = useState('')
  const [mode, setMode] = useState<InstructionEditMode>('episode')
  const [preview, setPreview] = useState<InstructionPreview | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  useEffect(() => {
    void fetchTasks()
  }, [fetchTasks])

  useEffect(() => {
    if (!episode) return
    const task = tasks.find(item => item.task_index === episode.task_index)
    setInstruction(task?.task_instruction ?? episode.task_instruction)
    setMode('episode')
    setPreview(null)
    setError(null)
    setNotice(null)
  }, [episode, tasks])

  const resetPreview = useCallback(() => {
    setPreview(null)
    setError(null)
    setNotice(null)
  }, [])

  const runPreview = useCallback(async (staleRetry = false) => {
    if (!episode || readOnly) return
    setError(null)
    setNotice(null)
    try {
      const next = await requestPreview(episode.episode_index, instruction, mode)
      setPreview(next)
      if (staleRetry) setNotice('Dataset changed while saving. This is a fresh preview; review it before saving again.')
    } catch (err) {
      setPreview(null)
      setError(err instanceof Error ? err.message : 'Could not preview this change')
    }
  }, [episode, instruction, mode, readOnly, requestPreview])

  const handleSave = useCallback(async () => {
    if (!episode || !preview || readOnly) return
    setError(null)
    setNotice(null)
    try {
      const result = await commit(episode.episode_index, instruction, mode, preview.fingerprint)
      await Promise.all([Promise.resolve(onInstructionUpdated(result)), fetchTasks()])
      setPreview(null)
      setNotice(mode === 'shared' ? `Updated the shared task for ${result.affected_episode_count} episode(s).` : 'Updated this episode instruction.')
    } catch (err) {
      if (err instanceof InstructionRequestError && err.code === 'instruction_preview_stale') {
        setPreview(null)
        await runPreview(true)
        return
      }
      setError(err instanceof Error ? err.message : 'Could not save this instruction')
    }
  }, [commit, episode, fetchTasks, instruction, mode, onInstructionUpdated, preview, readOnly, runPreview])

  if (!episode) {
    return <div style={styles.container}><div style={styles.title}>Instruction</div><div style={styles.empty}>Select an episode to view its instruction</div></div>
  }

  return (
    <section style={styles.container} aria-label="Episode instruction editor">
      <div style={styles.titleRow}>
        <span style={styles.title}>Instruction</span>
        <span style={styles.taskIndex}>Task #{episode.task_index}</span>
      </div>
      <textarea
        style={styles.textarea}
        value={instruction}
        onChange={event => { setInstruction(event.target.value); resetPreview() }}
        rows={4}
        disabled={readOnly}
        placeholder="Task instruction..."
      />
      {readOnly ? (
        <div style={styles.readOnly}>Raw datasets are read-only; instruction editing requires a LeRobot v3 dataset.</div>
      ) : (
        <>
          <div style={styles.modeGroup}>
            <label style={styles.modeLabel}>
              <input type="radio" checked={mode === 'episode'} onChange={() => { setMode('episode'); resetPreview() }} />
              Only this episode
            </label>
            <label style={styles.modeLabel}>
              <input type="radio" checked={mode === 'shared'} onChange={() => { setMode('shared'); resetPreview() }} />
              Change shared task
            </label>
          </div>
          <div style={styles.help}>
            {mode === 'episode'
              ? 'Default: reuse a matching task or create one, then reassign only this episode.'
              : 'Advanced: every episode using this task will receive the new instruction.'}
          </div>
          {preview && (
            <div style={styles.preview}>
              <div>{actionLabel[preview.action]}</div>
              <div style={styles.affected}>Affected episodes: {preview.affected_episode_count}</div>
              {mode === 'shared' && <div style={styles.confirmation}>Saving confirms this shared change for all affected episodes.</div>}
            </div>
          )}
          {error && <div style={styles.error}>{error}</div>}
          {notice && <div style={styles.notice}>{notice}</div>}
          {!preview ? (
            <button style={{ ...styles.previewButton, opacity: previewing ? 0.6 : 1 }} onClick={() => void runPreview()} disabled={previewing}>
              {previewing ? 'Previewing...' : 'Preview changes'}
            </button>
          ) : (
            <button style={{ ...styles.saveButton, opacity: saving ? 0.6 : 1 }} onClick={() => void handleSave()} disabled={saving}>
              {saving ? 'Saving...' : mode === 'shared' ? 'Confirm shared change' : 'Save only this episode'}
            </button>
          )}
        </>
      )}
    </section>
  )
}

const styles: Record<string, React.CSSProperties> = {
  container: { padding: '12px', borderBottom: '1px solid var(--border2)' },
  titleRow: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' },
  title: { fontSize: '12px', fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.08em', color: 'var(--text-muted)' },
  taskIndex: { color: 'var(--text-dim)', fontSize: '12px' },
  empty: { color: 'var(--text-dim)', fontSize: '13px', padding: '8px 0' },
  textarea: { width: '100%', boxSizing: 'border-box', background: 'var(--border2)', border: '1px solid var(--border3)', borderRadius: '4px', color: 'var(--text)', padding: '8px', fontSize: '13px', resize: 'vertical', fontFamily: 'inherit', outline: 'none', marginBottom: '8px' },
  modeGroup: { display: 'grid', gap: '5px', marginBottom: '6px' },
  modeLabel: { display: 'flex', gap: '6px', alignItems: 'center', color: 'var(--text)', fontSize: '12px', cursor: 'pointer' },
  help: { color: 'var(--text-muted)', fontSize: '11px', lineHeight: 1.35, marginBottom: '9px' },
  preview: { background: 'var(--border2)', border: '1px solid var(--border3)', borderRadius: '4px', color: 'var(--text)', fontSize: '12px', lineHeight: 1.4, padding: '8px', marginBottom: '8px' },
  affected: { fontWeight: 600, marginTop: '4px' },
  confirmation: { color: 'var(--c-yellow)', marginTop: '4px' },
  readOnly: { color: 'var(--text-muted)', fontSize: '12px', lineHeight: 1.4 },
  error: { color: 'var(--c-red)', fontSize: '12px', marginBottom: '8px' },
  notice: { color: 'var(--c-green)', fontSize: '12px', marginBottom: '8px' },
  previewButton: { width: '100%', background: 'var(--border3)', border: 'none', borderRadius: '4px', color: 'var(--text)', padding: '7px', fontSize: '13px', cursor: 'pointer' },
  saveButton: { width: '100%', background: '#3a5a3a', border: 'none', borderRadius: '4px', color: 'var(--c-green)', padding: '7px', fontSize: '13px', cursor: 'pointer' },
}
