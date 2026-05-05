import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const dir = dirname(fileURLToPath(import.meta.url))
const pillSrc = readFileSync(
  join(dir, '../src/components/WorkerControlPill.tsx'), 'utf8',
)
const ctrlSrc = readFileSync(
  join(dir, '../src/components/ConverterControls.tsx'), 'utf8',
)

function must(s, needle, label) {
  if (!s.includes(needle)) throw new Error(`[${label}] missing: ${needle}`)
}

must(pillSrc, "fetch(`/api/workers/", 'pill reads workers')
must(pillSrc, "PATCH", 'pill PATCHes desired_state')
must(pillSrc, "stale", 'pill renders stale badge')
must(ctrlSrc, 'WorkerControlPill', 'controls mounts pill')
must(ctrlSrc, '/api/jobs/', 'controls posts cancel via jobs endpoint')
must(ctrlSrc, 'docker-service-strip', 'controls renders docker service status')
must(ctrlSrc, 'dockerServices.map', 'controls maps docker services')

console.log('workerControlPill: pause/resume + cancel split surface present')
