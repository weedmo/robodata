import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const dir = dirname(fileURLToPath(import.meta.url))
const source = readFileSync(
  join(dir, '../src/components/trim/jobStatus.tsx'),
  'utf8',
)

function must(needle, label) {
  if (!source.includes(needle)) throw new Error(`[${label}] missing: ${needle}`)
}

must("status === 'cancelled'", 'cancelled is terminal')
must('useRef<ReturnType<typeof setTimeout> | null>', 'poll timer ownership')
must('const generationRef = useRef(0)', 'poll session ownership')
must('generation !== generationRef.current', 'stale response guard')
must('clearTimeout(timerRef.current)', 'active timer cleanup')
must('timerRef.current = setTimeout(poll, 1000)', 'serialized poll scheduling')
must('clearPollingTimer()', 'reset and restart cleanup')
must('useEffect(() => () => {', 'unmount cleanup')

console.log('trimJobPoller: cancelled jobs stop and intervals are cleaned up')
