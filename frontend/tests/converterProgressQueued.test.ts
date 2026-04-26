import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

function assertIncludes(actual: string, expected: string) {
  if (!actual.includes(expected)) {
    throw new Error(`Expected source to include: ${expected}`)
  }
}

const testDir = dirname(fileURLToPath(import.meta.url))
const src = readFileSync(
  join(testDir, '../src/components/ConverterProgress.tsx'),
  'utf8',
)

// startTask enqueues via /api/jobs and tracks the queued task locally so the
// UI shows a "Queued" state until live progress events arrive.
assertIncludes(src, 'enqueueConvertJob(')
assertIncludes(src, 'setQueued(prev =>')

// UI surfaces the queued state so the click is not a silent no-op.
assertIncludes(src, 'queued.has(t.cell_task)')
assertIncludes(src, "? 'Queued'")
assertIncludes(src, '|| isQueuedThis')

// Queued entries clear when the host converter picks up the task.
assertIncludes(src, "phase === 'converting' || phase === 'finalizing' || phase === 'done'")

// Status-poll-derived active signal (needed when the WebSocket log stream
// is not reachable in host-control mode).
assertIncludes(src, 'PROGRESS_ACTIVE_WINDOW_MS')
assertIncludes(src, 'doneSnapshotRef')
assertIncludes(src, 'progressAtRef')
assertIncludes(src, 'isProgressActive(t.cell_task)')
assertIncludes(src, 'const isHostActive = activeCellTask === t.cell_task')
assertIncludes(src, 'const isActive = isLiveActive || isProgressingNow')
assertIncludes(src, "? 'Running'")

console.log('converterProgressQueued: queued-state wiring present')
