export const CONVERTER_HOST_CONTROL_HINT =
  'Converter lifecycle is host-managed. Keep the host converter running, then use this page to queue one task at a time.'

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
  hasWork: boolean
}

interface ValidationTitleOptions {
  actionLabel: string
  dockerAvailable: boolean
  canValidate: boolean
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
  { dockerAvailable, taskStartAvailable, hasWork }: TaskConvertTitleOptions,
): string | undefined {
  if (!hasWork) {
    return 'No pending or failed recordings remain for this task.'
  }
  if (!taskStartAvailable) {
    if (!dockerAvailable) {
      return 'Convert requires the host converter to be running. Start it with main.sh first.'
    }
    return 'Convert is unavailable while the converter is running or starting.'
  }
  return undefined
}

export function getValidationTitle(
  { actionLabel, dockerAvailable, canValidate }: ValidationTitleOptions,
): string | undefined {
  if (!dockerAvailable) {
    return `${actionLabel} is disabled in the UI because ${HOST_CONTROL_REASON}`
  }
  if (!canValidate) {
    return `${actionLabel} is unavailable while the converter is running or starting.`
  }
  return undefined
}
