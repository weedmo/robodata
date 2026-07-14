import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const dir = dirname(fileURLToPath(import.meta.url))
const ctrlSrc = readFileSync(
  join(dir, '../src/components/ConverterControls.tsx'), 'utf8',
)
const apiSrc = readFileSync(
  join(dir, '../src/api/converter.ts'), 'utf8',
)
const lifecycleSrc = readFileSync(
  join(dir, '../src/hooks/useConverterJobLifecycle.ts'), 'utf8',
)

function must(s, needle, label) {
  if (!s.includes(needle)) throw new Error(`[${label}] missing: ${needle}`)
}
function mustNot(s, needle, label) {
  if (s.includes(needle)) throw new Error(`[${label}] still present: ${needle}`)
}

must(apiSrc, "'/api/jobs'", 'api enqueue path')
must(apiSrc, '`/api/jobs/${jobId}`', 'api fetches running job progress')
must(apiSrc, "type: 'convert'", 'api enqueue type literal')
must(apiSrc, 'dedupeKey ??', 'api accepts explicit dedupe key')
must(apiSrc, "typeof payload.cell_task === 'string'", 'api dedupes by cell_task first')
must(ctrlSrc, 'enqueueConvertJob', 'controls calls enqueueConvertJob')
must(ctrlSrc, 'AUTO_SCAN_DEDUPE_KEY', 'controls dedupes full auto scan')
must(ctrlSrc, 'useConverterJobLifecycle', 'controls delegates job lifecycle')
must(lifecycleSrc, 'fetchConvertJob', 'lifecycle polls running job progress')
must(lifecycleSrc, 'clearInterval(id)', 'lifecycle clears polling interval')
must(ctrlSrc, '현재 변환', 'controls labels current conversion')
must(ctrlSrc, '전체 자동 변환', 'controls exposes full auto conversion')
mustNot(ctrlSrc, 'host_runtime', 'no NAS file references in UI')
mustNot(ctrlSrc, 'CURATION_CONVERTER_CONTROL_MODE', 'no env name references in UI')

console.log('converterButtonEnqueue: Convert button posts to /api/jobs')
