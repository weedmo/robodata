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

function must(s, needle, label) {
  if (!s.includes(needle)) throw new Error(`[${label}] missing: ${needle}`)
}
function mustNot(s, needle, label) {
  if (s.includes(needle)) throw new Error(`[${label}] still present: ${needle}`)
}

must(apiSrc, "'/api/jobs'", 'api enqueue path')
must(apiSrc, "type: 'convert'", 'api enqueue type literal')
must(ctrlSrc, 'enqueueConvertJob', 'controls calls enqueueConvertJob')
mustNot(ctrlSrc, 'host_runtime', 'no NAS file references in UI')
mustNot(ctrlSrc, 'CURATION_CONVERTER_CONTROL_MODE', 'no env name references in UI')

console.log('converterButtonEnqueue: Convert button posts to /api/jobs')
