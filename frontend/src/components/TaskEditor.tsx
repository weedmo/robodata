import { useState, useEffect } from 'react'
import { useTasks } from '../hooks/useTasks'
import type { Episode } from '../types'

interface TaskEditorProps {
  datasetPath: string
  episode: Episode | null
}

export function TaskEditor({ datasetPath, episode }: TaskEditorProps) {
  const { tasks, fetchTasks, updateTask } = useTasks(datasetPath)
  const [instruction, setInstruction] = useState('')
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    void fetchTasks()
  }, [fetchTasks])

  useEffect(() => {
    if (episode) {
      const task = tasks.find(t => t.task_index === episode.task_index)
      setInstruction(task?.task_instruction ?? episode.task_instruction)
      setSaveError(null)
      setSaved(false)
    }
  }, [episode, tasks])

  const handleSave = async () => {
    if (!episode) return
    setSaving(true)
    setSaveError(null)
    setSaved(false)
    try {
      await updateTask(episode.task_index, instruction)
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  if (!episode) {
    return (
      <div style={styles.container}>
        <div style={styles.title}>Task</div>
        <div style={styles.empty}>Select an episode to view task</div>
      </div>
    )
  }

  return (
    <div style={styles.container}>
      <div style={styles.titleRow}>
        <span style={styles.title}>Task #{episode.task_index}</span>
      </div>

      <textarea
        style={styles.textarea}
        value={instruction}
        onChange={e => setInstruction(e.target.value)}
        rows={4}
        placeholder="Task instruction..."
      />

      {saveError && <div style={styles.error}>{saveError}</div>}

      <button
        style={{ ...styles.saveButton, opacity: saving ? 0.6 : 1 }}
        onClick={handleSave}
        disabled={saving}
      >
        {saving ? 'Saving...' : saved ? 'Saved!' : 'Save Task'}
      </button>
    </div>
  )
}

const styles: Record<string, React.CSSProperties> = {
  container: {
    padding: '12px',
  },
  titleRow: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: '8px',
  },
  title: {
    fontSize: '12px',
    fontWeight: 600,
    textTransform: 'uppercase',
    letterSpacing: '0.08em',
    color: 'var(--text-muted)',
  },
  empty: {
    color: 'var(--text-dim)',
    fontSize: '13px',
    padding: '8px 0',
  },
  textarea: {
    width: '100%',
    background: 'var(--border2)',
    border: '1px solid var(--border3)',
    borderRadius: '4px',
    color: 'var(--text)',
    padding: '8px',
    fontSize: '13px',
    resize: 'vertical',
    fontFamily: 'inherit',
    outline: 'none',
    marginBottom: '8px',
  },
  error: {
    color: 'var(--c-red)',
    fontSize: '12px',
    marginBottom: '8px',
  },
  saveButton: {
    width: '100%',
    background: '#3a5a3a',
    border: 'none',
    borderRadius: '4px',
    color: 'var(--c-green)',
    padding: '7px',
    fontSize: '13px',
    cursor: 'pointer',
  },
}
