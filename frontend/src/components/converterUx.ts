export const CONVERTER_HOST_CONTROL_HINT =
  'Converter lifecycle is host-managed. Keep the host converter running, then use this page to queue one task at a time.'

export function getConverterModeHint(
  taskStartUnavailableReason: string | null,
): string {
  return taskStartUnavailableReason ?? CONVERTER_HOST_CONTROL_HINT
}

const HOST_CONTROL_REASON =
  'converter lifecycle is intentionally handled on the host.'

const HOST_LIFECYCLE_ACTION_REASON =
  'converter build/start is handled on the host.'

interface ConverterActionTitleOptions {
  dockerAvailable: boolean
  busy: boolean
}

interface HostStopTitleOptions {
  hostStopAvailable: boolean
  busy: boolean
}

interface TaskConvertTitleOptions {
  dockerAvailable: boolean
  taskStartAvailable: boolean
  taskStartUnavailableReason: string | null
  hasWork: boolean
}

interface ValidationTitleOptions {
  actionLabel: string
  dockerAvailable: boolean
  canValidate: boolean
  taskStartUnavailableReason: string | null
}

export function getConverterActionTitle(
  actionLabel: string,
  { busy }: ConverterActionTitleOptions,
): string | undefined {
  if (busy && actionLabel !== 'Build' && actionLabel !== 'Start' && actionLabel !== 'Stop') {
    return `${actionLabel} is temporarily unavailable while another converter action is in progress.`
  }
  return `${actionLabel} is disabled in the UI because ${HOST_LIFECYCLE_ACTION_REASON}`
}

export function getHostStopTitle(
  { hostStopAvailable, busy }: HostStopTitleOptions,
): string {
  if (busy) {
    return 'Stop request is temporarily unavailable while another converter action is in progress.'
  }
  if (!hostStopAvailable) {
    return 'Stop is only available when a host-managed converter heartbeat is active.'
  }
  return 'Request the host converter to stop gracefully at the next recording boundary.'
}

export function getTaskConvertTitle(
  {
    dockerAvailable,
    taskStartAvailable,
    taskStartUnavailableReason,
    hasWork,
  }: TaskConvertTitleOptions,
): string | undefined {
  if (!hasWork) {
    return 'No pending or failed recordings remain for this task.'
  }
  if (!taskStartAvailable) {
    if (taskStartUnavailableReason) {
      return taskStartUnavailableReason
    }
    if (!dockerAvailable) {
      return 'Convert is unavailable because the host converter is not running.'
    }
    return 'Convert is unavailable while the converter is running or starting.'
  }
  return undefined
}

export function getValidationTitle(
  {
    actionLabel,
    dockerAvailable,
    canValidate,
    taskStartUnavailableReason,
  }: ValidationTitleOptions,
): string | undefined {
  if (!canValidate && taskStartUnavailableReason) {
    return taskStartUnavailableReason
  }
  if (!dockerAvailable) {
    return `${actionLabel} is disabled in the UI because ${HOST_CONTROL_REASON}`
  }
  if (!canValidate) {
    return `${actionLabel} is unavailable while the converter is running or starting.`
  }
  return undefined
}
