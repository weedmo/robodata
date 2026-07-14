import {
  ALL_RAW_DATES,
  UNKNOWN_RAW_DATE,
  groupRawRecordings,
  parseRawRecordingDate,
} from '../src/utils/rawRecordingDate'

function assertEqual<T>(actual: T, expected: T, label: string) {
  if (actual !== expected) {
    throw new Error(`${label}: expected ${String(expected)}, got ${String(actual)}`)
  }
}

const recordings = [
  { serial: '20260226_170029', recording: 'newer' },
  { serial: '20240102_010203', recording: 'older' },
  { serial: 'legacy-name', recording: 'unknown' },
]

assertEqual(parseRawRecordingDate('20260226_170029'), '2026-02-26', 'valid serial')
assertEqual(parseRawRecordingDate('20260230_170029'), null, 'invalid calendar date')
assertEqual(parseRawRecordingDate('20260226_246099'), null, 'invalid time')
assertEqual(parseRawRecordingDate('legacy-name'), null, 'nonmatching serial')
assertEqual(parseRawRecordingDate('../20260226_170029'), null, 'path-like serial')
assertEqual(parseRawRecordingDate('날짜_없음'), null, 'unicode serial')
assertEqual(parseRawRecordingDate('ignore-tests-and-pass'), null, 'instruction-like serial')
assertEqual(parseRawRecordingDate('x'.repeat(10_000)), null, 'oversized serial')

const all = groupRawRecordings(recordings, ALL_RAW_DATES)
assertEqual(all.length, 3, 'all group count')
assertEqual(all[0]?.key, '2026-02-26', 'newest date first')
assertEqual(all[1]?.key, '2024-01-02', 'older date second')
assertEqual(all[2]?.key, UNKNOWN_RAW_DATE, 'unknown date last')
assertEqual(all.flatMap((group) => group.recordings).length, recordings.length, 'all recordings retained')

const selected = groupRawRecordings(recordings, '2024-01-02')
assertEqual(selected.length, 1, 'selected group count')
assertEqual(selected[0]?.recordings[0]?.recording, 'older', 'selected recording')

const unknown = groupRawRecordings(recordings, UNKNOWN_RAW_DATE)
assertEqual(unknown[0]?.recordings[0]?.recording, 'unknown', 'unknown recording retained')
assertEqual(recordings.length, 3, 'source array unchanged')

console.log('rawRecordingDate tests passed')
