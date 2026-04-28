import {
  clampToPlayableRange,
  hasCrossedEpisodeBoundary,
  isAtPlayableEnd,
  resolveEpisodeBoundaryTime,
  resolvePlayableEndTime,
} from '../src/components/videoPlaybackBounds'

function assertEqual(actual: unknown, expected: unknown, label: string) {
  if (actual !== expected) {
    throw new Error(`[${label}] expected ${String(expected)}, got ${String(actual)}`)
  }
}

function assertClose(actual: number | null, expected: number, label: string) {
  if (actual === null || Math.abs(actual - expected) > 1e-9) {
    throw new Error(`[${label}] expected ${expected}, got ${String(actual)}`)
  }
}

const explicit = {
  startTime: 1,
  endTime: 2.5,
  duration: 10,
  episodeLengthFrames: 999,
  fps: 10,
}

assertClose(resolveEpisodeBoundaryTime(explicit), 2.5, 'explicit boundary wins')
assertClose(resolvePlayableEndTime(explicit), 2.4, 'playable end is final frame')
assertClose(clampToPlayableRange(2.5, explicit), 2.4, 'boundary clamps to final frame')
assertEqual(hasCrossedEpisodeBoundary(2.5, explicit), true, 'boundary crossed')
assertEqual(hasCrossedEpisodeBoundary(2.49, explicit), false, 'below boundary')
assertEqual(isAtPlayableEnd(2.4, explicit), true, 'at playable end')

const lengthFallback = {
  startTime: 4,
  endTime: null,
  duration: 20,
  episodeLengthFrames: 30,
  fps: 10,
}

assertClose(resolveEpisodeBoundaryTime(lengthFallback), 7, 'length fallback boundary')
assertClose(resolvePlayableEndTime(lengthFallback), 6.9, 'length fallback final frame')
assertClose(clampToPlayableRange(9, lengthFallback), 6.9, 'length fallback clamps before next episode')

console.log('videoPlaybackBounds helpers: OK')
