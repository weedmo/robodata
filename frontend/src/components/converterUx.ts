export const CONVERTER_HOST_CONTROL_HINT =
  'Converter control is handled on the host. Use main.sh to build, start, or stop jobs.'

const HOST_CONTROL_REASON =
  'Docker control is intentionally handled by host main.sh.'

interface ConverterActionTitleOptions {
  dockerAvailable: boolean
  busy: boolean
}

interface TaskConvertTitleOptions {
  dockerAvailable: boolean
  canStart: boolean
  hasPending: boolean
}

interface ValidationTitleOptions {
  actionLabel: string
  dockerAvailable: boolean
  canValidate: boolean
}

export function getConverterActionTitle(
  actionLabel: string,
  { dockerAvailable, busy }: ConverterActionTitleOptions,
): string | undefined {
  if (!dockerAvailable) {
    return `${actionLabel} is disabled in the UI because ${HOST_CONTROL_REASON}`
  }
  if (busy) {
    return `${actionLabel} is temporarily unavailable while another converter action is in progress.`
  }
  return undefined
}

export function getTaskConvertTitle(
  { dockerAvailable, canStart, hasPending }: TaskConvertTitleOptions,
): string | undefined {
  if (!dockerAvailable) {
    return `Convert is disabled in the UI because ${HOST_CONTROL_REASON}`
  }
  if (!hasPending) {
    return 'No pending recordings remain for this task.'
  }
  if (!canStart) {
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
