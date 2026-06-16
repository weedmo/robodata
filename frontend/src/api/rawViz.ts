// API for raw rosbag curation visualization (streams into the shared Rerun
// viewer at /rerun/, same one the lerobot path uses).

export type RawCell = {
  name: string
  path: string
  active: boolean
}

export type RawTask = {
  task: string
  name: string
  count: number
}

export type RawRecording = {
  serial: string
  recording: string
  task_name: string
}

export async function listRawCells(sourcePath: string): Promise<RawCell[]> {
  const r = await fetch(`/api/cells?root=${encodeURIComponent(sourcePath)}`)
  if (!r.ok) {
    const body = await r.json().catch(() => ({}))
    throw new Error(body.detail || `failed to list cells (${r.status})`)
  }
  const data = await r.json()
  return (data as Array<{ name: string; path: string; active: boolean }>).map((c) => ({
    name: c.name,
    path: c.path,
    active: c.active,
  }))
}

export async function listRawTasks(cell: string): Promise<RawTask[]> {
  const r = await fetch(`/api/converter/tasks?cell=${encodeURIComponent(cell)}`)
  if (!r.ok) {
    const body = await r.json().catch(() => ({}))
    throw new Error(body.detail || `failed to list tasks (${r.status})`)
  }
  const data = await r.json()
  return (data.tasks ?? []) as RawTask[]
}

export async function listRawRecordings(task: string): Promise<RawRecording[]> {
  const r = await fetch(`/api/converter/recordings?task=${encodeURIComponent(task)}`)
  if (!r.ok) {
    const body = await r.json().catch(() => ({}))
    throw new Error(body.detail || `failed to list recordings (${r.status})`)
  }
  const data = await r.json()
  return (data.recordings ?? []) as RawRecording[]
}

export async function visualizeRaw(recording: string): Promise<string> {
  const r = await fetch(`/api/rerun/visualize-raw?recording=${encodeURIComponent(recording)}`, {
    method: 'POST',
  })
  const body = await r.json().catch(() => ({}))
  if (!r.ok) {
    throw new Error(body.detail || `visualization failed (${r.status})`)
  }
  return (body.detail as string) ?? 'ok'
}
