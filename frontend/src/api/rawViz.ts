// API for raw rosbag curation visualization (streams into the shared Rerun
// viewer at /rerun/, same one the lerobot path uses).

// The embedded Rerun web viewer must be told which gRPC proxy to connect to,
// else it shows the welcome screen. The converter streams to the rerun
// service's proxy, published on the host. `renderer=webgl` forces the WebGL2
// backend instead of letting the viewer try WebGPU first: on machines without
// WebGPU the fallback can fail with "failed to create wgpu surface ... canvas
// already in use", so we skip the WebGPU attempt entirely.
const RERUN_PROXY_PORT = import.meta.env.VITE_RERUN_PROXY_PORT || '9876'
export function rerunViewerSrc(): string {
  const proxy = `rerun+http://${window.location.hostname}:${RERUN_PROXY_PORT}/proxy`
  return `/rerun/?url=${encodeURIComponent(proxy)}&renderer=webgl`
}

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
