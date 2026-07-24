import type {
  ConverterState,
  ConverterStatus,
  ConverterTaskProgress,
  ConverterValidationPayload,
  DockerServiceStatus,
  ValidationStatus,
} from './types'

const CONVERTER_STATES = new Set<ConverterState>([
  'running',
  'stopped',
  'building',
  'error',
  'unknown',
])

const VALIDATION_STATES = new Set<ValidationStatus>([
  'not_run',
  'running',
  'passed',
  'failed',
  'partial',
])

export const EMPTY_CONVERTER_STATUS: ConverterStatus = {
  container_state: 'unknown',
  docker_available: false,
  task_start_available: false,
  task_start_unavailable_reason: null,
  exit_code: null,
  oom_killed: false,
  finished_at: null,
  tasks: [],
  summary: '',
  active_cell_task: null,
  docker_services: [],
}

function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' ? value as Record<string, unknown> : {}
}

function asNumber(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

function asString(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function asNullableString(value: unknown): string | null {
  return typeof value === 'string' ? value : null
}

function asValidationStatus(value: unknown): ValidationStatus {
  return typeof value === 'string' && VALIDATION_STATES.has(value as ValidationStatus)
    ? value as ValidationStatus
    : 'not_run'
}

function normalizeValidation(value: unknown): ConverterValidationPayload {
  const validation = asRecord(value)
  const quick = asRecord(validation.quick)
  const full = asRecord(validation.full)
  return {
    quick: {
      status: asValidationStatus(quick.status),
      summary: asString(quick.summary),
      checked_at: asNullableString(quick.checked_at),
    },
    full: {
      status: asValidationStatus(full.status),
      summary: asString(full.summary),
      checked_at: asNullableString(full.checked_at),
    },
  }
}

function normalizeTask(value: unknown): ConverterTaskProgress | null {
  const task = asRecord(value)
  const cellTask = asString(task.cell_task)
  if (!cellTask) return null
  return {
    cell_task: cellTask,
    total: asNumber(task.total),
    done: asNumber(task.done),
    pending: asNumber(task.pending),
    failed: asNumber(task.failed),
    retry: asNumber(task.retry),
    last_updated: asNullableString(task.last_updated),
    validation: normalizeValidation(task.validation),
  }
}

function normalizeDockerService(value: unknown): DockerServiceStatus | null {
  const service = asRecord(value)
  const name = asString(service.name)
  if (!name) return null
  return {
    name,
    state: asString(service.state),
    healthy: service.healthy === true,
    status: asNullableString(service.status),
  }
}

export function normalizeConverterStatus(value: unknown): ConverterStatus {
  const status = asRecord(value)
  const state = status.container_state
  const tasks = Array.isArray(status.tasks)
    ? status.tasks.map(normalizeTask).filter((task): task is ConverterTaskProgress => task !== null)
    : []
  const dockerServices = Array.isArray(status.docker_services)
    ? status.docker_services
      .map(normalizeDockerService)
      .filter((service): service is DockerServiceStatus => service !== null)
    : []

  return {
    container_state: typeof state === 'string' && CONVERTER_STATES.has(state as ConverterState)
      ? state as ConverterState
      : 'unknown',
    docker_available: status.docker_available === true,
    task_start_available: status.task_start_available === true,
    task_start_unavailable_reason: asNullableString(
      status.task_start_unavailable_reason,
    ),
    exit_code: typeof status.exit_code === 'number' ? status.exit_code : null,
    oom_killed: status.oom_killed === true,
    finished_at: asNullableString(status.finished_at),
    tasks,
    summary: asString(status.summary),
    active_cell_task: asNullableString(status.active_cell_task),
    docker_services: dockerServices,
  }
}
