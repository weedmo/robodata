import {
  CONVERTER_HOST_CONTROL_HINT,
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
  'Converter control is handled on the host. Use main.sh to build, start, or stop jobs.',
)

assertIncludes(
  getConverterActionTitle('Start', { dockerAvailable: false, busy: false }),
  'main.sh',
)

assertEqual(
  getTaskConvertTitle({
    dockerAvailable: false,
    canStart: false,
    hasPending: true,
  }),
  'Convert is disabled in the UI because Docker control is intentionally handled by host main.sh.',
)

assertEqual(
  getTaskConvertTitle({
    dockerAvailable: true,
    canStart: true,
    hasPending: false,
  }),
  'No pending recordings remain for this task.',
)

assertEqual(
  getValidationTitle({
    actionLabel: 'Quick Check',
    dockerAvailable: false,
    canValidate: false,
  }),
  'Quick Check is disabled in the UI because Docker control is intentionally handled by host main.sh.',
)

assertEqual(
  getConverterActionTitle('Build', { dockerAvailable: true, busy: true }),
  'Build is temporarily unavailable while another converter action is in progress.',
)

assertEqual(
  getConverterActionTitle('Stop', { dockerAvailable: true, busy: false }),
  undefined,
)

assertEqual(
  getTaskConvertTitle({
    dockerAvailable: true,
    canStart: false,
    hasPending: true,
  }),
  'Convert is unavailable while the converter is running or starting.',
)

assertEqual(
  getValidationTitle({
    actionLabel: 'Full Check',
    dockerAvailable: true,
    canValidate: false,
  }),
  'Full Check is unavailable while the converter is running or starting.',
)

assertEqual(
  getValidationTitle({
    actionLabel: 'Full Check',
    dockerAvailable: true,
    canValidate: true,
  }),
  undefined,
)
