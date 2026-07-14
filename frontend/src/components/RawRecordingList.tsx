import { useEffect, useMemo, useState } from 'react'
import type { RawRecording } from '../api/rawViz'
import {
  ALL_RAW_DATES,
  groupRawRecordings,
} from '../utils/rawRecordingDate'

interface Props {
  recordings: RawRecording[]
  activeRecording: string | null
  onVisualize: (recording: string) => void
}

export function RawRecordingList({ recordings, activeRecording, onVisualize }: Props) {
  const [selectedDate, setSelectedDate] = useState(ALL_RAW_DATES)
  const allGroups = useMemo(() => groupRawRecordings(recordings), [recordings])
  const visibleGroups = useMemo(
    () => groupRawRecordings(recordings, selectedDate),
    [recordings, selectedDate],
  )

  useEffect(() => {
    if (
      selectedDate !== ALL_RAW_DATES
      && !allGroups.some((group) => group.key === selectedDate)
    ) {
      setSelectedDate(ALL_RAW_DATES)
    }
  }, [allGroups, selectedDate])

  return (
    <div className="raw-recording-browser">
      <div className="raw-recording-toolbar">
        <label className="raw-recording-date-filter">
          <span>날짜</span>
          <select value={selectedDate} onChange={(event) => setSelectedDate(event.target.value)}>
            <option value={ALL_RAW_DATES}>전체 날짜</option>
            {allGroups.map((group) => (
              <option key={group.key} value={group.key}>
                {group.label} ({group.recordings.length})
              </option>
            ))}
          </select>
        </label>
        <span className="raw-recording-total">
          {visibleGroups.reduce((total, group) => total + group.recordings.length, 0)} recordings
        </span>
      </div>

      <div className="raw-recording-groups">
        {visibleGroups.map((group) => (
          <section key={group.key} className="raw-recording-group">
            <div className="raw-recording-group-header">
              <span>{group.label}</span>
              <span>{group.recordings.length}</span>
            </div>
            <ul className="raw-viz-list">
              {group.recordings.map((recording) => (
                <li key={recording.recording} className="raw-viz-item raw-recording-item">
                  <span className="raw-recording-serial">{recording.serial}</span>
                  <button
                    type="button"
                    disabled={activeRecording === recording.recording}
                    onClick={() => onVisualize(recording.recording)}
                  >
                    {activeRecording === recording.recording ? '여는 중…' : 'Rerun에서 보기'}
                  </button>
                </li>
              ))}
            </ul>
          </section>
        ))}
      </div>
    </div>
  )
}
