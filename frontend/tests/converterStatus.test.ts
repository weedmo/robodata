import { normalizeConverterStatus } from '../src/converterStatus'

function assertEqual(actual: unknown, expected: unknown, label: string) {
  if (actual !== expected) {
    throw new Error(`${label}: expected ${String(expected)}, got ${String(actual)}`)
  }
}

const missingArrays = normalizeConverterStatus({
  container_state: 'stopped',
  docker_available: false,
  summary: 'legacy payload',
})

assertEqual(Array.isArray(missingArrays.tasks), true, 'tasks fallback')
assertEqual(missingArrays.tasks.length, 0, 'tasks length fallback')
assertEqual(Array.isArray(missingArrays.docker_services), true, 'docker services fallback')
assertEqual(missingArrays.docker_services.length, 0, 'docker services length fallback')

const partialTask = normalizeConverterStatus({
  container_state: 'running',
  docker_available: true,
  task_start_available: true,
  task_start_unavailable_reason: 'operator hold',
  tasks: [
    {
      cell_task: 'cell001/task_a',
      total: 5,
      done: 2,
    },
  ],
})

assertEqual(partialTask.tasks.length, 1, 'partial task preserved')
assertEqual(
  partialTask.task_start_unavailable_reason,
  'operator hold',
  'task unavailable reason preserved',
)
assertEqual(partialTask.tasks[0].pending, 0, 'task pending fallback')
assertEqual(partialTask.tasks[0].validation.quick.status, 'not_run', 'quick validation fallback')
assertEqual(partialTask.tasks[0].validation.full.summary, '', 'full validation summary fallback')

console.log('converterStatus: legacy and partial payloads normalize safely')
