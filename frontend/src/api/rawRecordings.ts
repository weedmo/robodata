export type RawRecording = {
  serial: string
  recording: string
  task_name: string
}

export async function listRawRecordings(task: string): Promise<RawRecording[]> {
  const response = await fetch(`/api/converter/recordings?task=${encodeURIComponent(task)}`)
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(body.detail || `failed to list recordings (${response.status})`)
  }
  const data = await response.json()
  return (data.recordings ?? []) as RawRecording[]
}
