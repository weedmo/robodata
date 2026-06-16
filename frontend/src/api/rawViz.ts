// API for raw rosbag curation visualization (streams into the shared Rerun
// viewer at /rerun/, same one the lerobot path uses).

export type RawRecording = {
  serial: string
  recording: string
  task_name: string
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
