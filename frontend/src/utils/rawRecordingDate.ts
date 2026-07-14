export const ALL_RAW_DATES = '__all__'
export const UNKNOWN_RAW_DATE = '__unknown__'
export const UNKNOWN_RAW_DATE_LABEL = '날짜 없음'

export interface SerialRecording {
  serial: string
}

export interface RawRecordingDateGroup<T extends SerialRecording> {
  key: string
  label: string
  recordings: T[]
}

export function parseRawRecordingDate(serial: string): string | null {
  const match = /^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})$/.exec(serial)
  if (!match) return null

  const [, yearText, monthText, dayText, hourText, minuteText, secondText] = match
  const year = Number(yearText)
  const month = Number(monthText)
  const day = Number(dayText)
  const hour = Number(hourText)
  const minute = Number(minuteText)
  const second = Number(secondText)
  if (year < 1000) return null

  const parsed = new Date(Date.UTC(year, month - 1, day, hour, minute, second))
  if (
    parsed.getUTCFullYear() !== year
    || parsed.getUTCMonth() !== month - 1
    || parsed.getUTCDate() !== day
    || parsed.getUTCHours() !== hour
    || parsed.getUTCMinutes() !== minute
    || parsed.getUTCSeconds() !== second
  ) {
    return null
  }

  return `${yearText}-${monthText}-${dayText}`
}

export function rawRecordingDateKey(serial: string): string {
  return parseRawRecordingDate(serial) ?? UNKNOWN_RAW_DATE
}

export function rawRecordingDateLabel(key: string): string {
  return key === UNKNOWN_RAW_DATE ? UNKNOWN_RAW_DATE_LABEL : key
}

export function groupRawRecordings<T extends SerialRecording>(
  recordings: readonly T[],
  selectedDate: string = ALL_RAW_DATES,
): RawRecordingDateGroup<T>[] {
  const grouped = new Map<string, T[]>()

  for (const recording of recordings) {
    const key = rawRecordingDateKey(recording.serial)
    if (selectedDate !== ALL_RAW_DATES && key !== selectedDate) continue
    const group = grouped.get(key)
    if (group) group.push(recording)
    else grouped.set(key, [recording])
  }

  return Array.from(grouped.entries())
    .sort(([left], [right]) => {
      if (left === UNKNOWN_RAW_DATE) return 1
      if (right === UNKNOWN_RAW_DATE) return -1
      return right.localeCompare(left)
    })
    .map(([key, items]) => ({
      key,
      label: rawRecordingDateLabel(key),
      recordings: items,
    }))
}
