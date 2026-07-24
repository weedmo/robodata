import {
  CONVERTER_HOST_CONTROL_HINT,
  getConverterModeHint,
  getHostStopTitle,
  getConverterActionTitle,
  getTaskConvertTitle,
  getValidationTitle,
} from '../src/components/converterUx'

function assertEqual(actual: unknown, expected: unknown) {
  if (actual !== expected) {
    throw new Error(`Expected ${String(expected)}, got ${String(actual)}`)
  }
}

function assertIncludes(actual: string | undefined, expected: string) {
  if (!actual || !actual.includes(expected)) {
    throw new Error(`Expected "${actual}" to include "${expected}"`)
  }
}

assertEqual(
  CONVERTER_HOST_CONTROL_HINT,
  'Converter lifecycle is host-managed. Keep the host converter running, then use this page to queue one task at a time.',
)

assertEqual(
  getConverterModeHint('Conversion is disabled by operator configuration'),
  'Conversion is disabled by operator configuration',
)

assertEqual(
  getConverterModeHint(null),
  CONVERTER_HOST_CONTROL_HINT,
)

assertEqual(
  getConverterActionTitle('Build', { dockerAvailable: true, busy: false }),
  'Build is disabled in the UI because converter build/start is handled on the host.',
)

assertIncludes(
  getConverterActionTitle('Start', { dockerAvailable: false, busy: false }),
  'host',
)

assertEqual(
  getHostStopTitle({ hostStopAvailable: true, busy: false }),
  'Request the host converter to stop gracefully at the next recording boundary.',
)

assertEqual(
  getHostStopTitle({ hostStopAvailable: false, busy: false }),
  'Stop is only available when a host-managed converter heartbeat is active.',
)

assertEqual(
  getTaskConvertTitle({
    dockerAvailable: false,
    taskStartAvailable: false,
    taskStartUnavailableReason: 'Conversion is disabled by operator configuration',
    hasWork: true,
  }),
  'Conversion is disabled by operator configuration',
)

assertEqual(
  getTaskConvertTitle({
    dockerAvailable: false,
    taskStartAvailable: true,
    taskStartUnavailableReason: null,
    hasWork: true,
  }),
  undefined,
)

assertEqual(
  getTaskConvertTitle({
    dockerAvailable: true,
    taskStartAvailable: true,
    taskStartUnavailableReason: null,
    hasWork: false,
  }),
  'No pending or failed recordings remain for this task.',
)

assertEqual(
  getValidationTitle({
    actionLabel: 'Quick Check',
    dockerAvailable: false,
    canValidate: false,
    taskStartUnavailableReason: null,
  }),
  'Quick Check is disabled in the UI because converter lifecycle is intentionally handled on the host.',
)

assertEqual(
  getConverterActionTitle('Build', { dockerAvailable: true, busy: true }),
  'Build is disabled in the UI because converter build/start is handled on the host.',
)

assertEqual(
  getConverterActionTitle('Stop', { dockerAvailable: true, busy: false }),
  'Stop is disabled in the UI because converter build/start is handled on the host.',
)

assertEqual(
  getTaskConvertTitle({
    dockerAvailable: true,
    taskStartAvailable: false,
    taskStartUnavailableReason: null,
    hasWork: true,
  }),
  'Convert is unavailable while the converter is running or starting.',
)

assertEqual(
  getValidationTitle({
    actionLabel: 'Full Check',
    dockerAvailable: true,
    canValidate: false,
    taskStartUnavailableReason: null,
  }),
  'Full Check is unavailable while the converter is running or starting.',
)

assertEqual(
  getValidationTitle({
    actionLabel: 'Full Check',
    dockerAvailable: true,
    canValidate: true,
    taskStartUnavailableReason: null,
  }),
  undefined,
)

assertEqual(
  getValidationTitle({
    actionLabel: 'Full Check',
    dockerAvailable: true,
    canValidate: false,
    taskStartUnavailableReason: 'Conversion is disabled by operator configuration',
  }),
  'Conversion is disabled by operator configuration',
)
