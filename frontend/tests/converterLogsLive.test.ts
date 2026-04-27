import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

function assertIncludes(actual: string, expected: string) {
  if (!actual.includes(expected)) {
    throw new Error(`Expected source to include: ${expected}`)
  }
}

const testDir = dirname(fileURLToPath(import.meta.url))
const logsSource = readFileSync(join(testDir, '../src/components/ConverterLogs.tsx'), 'utf8')
const pageSource = readFileSync(join(testDir, '../src/components/ConverterPage.tsx'), 'utf8')
const css = readFileSync(join(testDir, '../src/App.css'), 'utf8')

assertIncludes(logsSource, "case 'recording_start':")
assertIncludes(logsSource, 'Recording ${recordingName(event.recording!)} (${event.index}/${event.total})')
assertIncludes(logsSource, 'log-badge log-badge-active')
assertIncludes(logsSource, '>REC<')
assertIncludes(logsSource, 'streaming: boolean')
assertIncludes(logsSource, '{streaming && (')
assertIncludes(logsSource, 'cvl-live-indicator')
assertIncludes(logsSource, 'aria-label="live"')
assertIncludes(logsSource, 'cvl-live-dot')
assertIncludes(logsSource, 'LIVE')

assertIncludes(css, '.cvl-live-indicator')
assertIncludes(css, '.cvl-live-dot')
assertIncludes(css, '.cvl-live-dot { animation: none; }')

assertIncludes(pageSource, "status.task_start_available || status.container_state === 'running'")
assertIncludes(pageSource, 'streaming={canStreamLogs}')

console.log('converterLogsLive: recording row and live indicator wiring present')
