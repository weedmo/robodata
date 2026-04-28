import { useEffect, useState, useRef, useCallback, useImperativeHandle, forwardRef } from 'react'
import client from '../api/client'

interface Camera {
  key: string
  label: string
  url: string
  from_timestamp: number
  to_timestamp: number | null
}

export interface VideoPlayerHandle {
  stepFrame: (direction: 1 | -1) => void
  seekToFrame: (frame: number) => void
  seekToTimestamp: (ts: number) => void
  togglePlay: () => void
  cycleSpeed: (direction: 1 | -1) => void
}

const SPEED_LADDER = [0.5, 1, 2, 4]

interface VideoPlayerProps {
  datasetKey: string | null
  episodeIndex: number | null
  fps: number
  onFrameChange?: (frame: number) => void
  terminalFrames?: number[]
}

export const VideoPlayer = forwardRef<VideoPlayerHandle, VideoPlayerProps>(function VideoPlayer({ datasetKey, episodeIndex, fps, onFrameChange, terminalFrames = [] }, ref) {
  const [cameras, setCameras] = useState<Camera[]>([])
  const [loading, setLoading] = useState(false)
  const [playing, setPlaying] = useState(false)
  const [currentTime, setCurrentTime] = useState(0)
  const [duration, setDuration] = useState(0)
  const [ready, setReady] = useState(false)
  const [videoStartTime, setVideoStartTime] = useState(0)
  const [videoEndTime, setVideoEndTime] = useState(0)
  const videoRefs = useRef<Map<string, HTMLVideoElement>>(new Map())
  const primaryKeyRef = useRef<string | null>(null)
  const animFrameRef = useRef<number>(0)

  const [defaultRate, setDefaultRate] = useState(() => {
    const saved = localStorage.getItem('curation-default-speed')
    return saved ? parseFloat(saved) : 1
  })
  const [playbackRate, setPlaybackRate] = useState(defaultRate)

  const setAsDefault = useCallback((rate: number) => {
    setDefaultRate(rate)
    localStorage.setItem('curation-default-speed', String(rate))
  }, [])
  const effectiveFps = fps || 30

  // Per-camera offset lookup so each <video> can seek into its own bundled file.
  // Each camera may land in a different (chunk_index, file_index) pair, so their
  // from_timestamp values inside the MP4 differ — seeking all videos to the
  // primary's offset leaks unrelated episodes into secondary cameras.
  const camInfoByKey = useRef<Map<string, Camera>>(new Map())
  const applyPerCamTime = useCallback((absPrimaryTime: number) => {
    const primaryCam = primaryKeyRef.current
      ? camInfoByKey.current.get(primaryKeyRef.current)
      : null
    const primaryFrom = primaryCam?.from_timestamp ?? 0
    const relative = Math.max(0, absPrimaryTime - primaryFrom)
    videoRefs.current.forEach((video, key) => {
      const cam = camInfoByKey.current.get(key)
      const from = cam?.from_timestamp ?? 0
      video.currentTime = from + relative
    })
  }, [])

  // Fetch cameras when episode changes
  useEffect(() => {
    if (episodeIndex === null || !datasetKey) {
      setCameras([])
      return
    }
    setLoading(true)
    setPlaying(false)
    setReady(false)
    setCurrentTime(0)
    setDuration(0)
    videoRefs.current.clear()
    camInfoByKey.current.clear()
    primaryKeyRef.current = null

    client.get<Camera[]>(`/datasets/${datasetKey}/videos/${episodeIndex}/cameras`)
      .then(res => {
        setCameras(res.data)
        res.data.forEach(cam => camInfoByKey.current.set(cam.key, cam))
        if (res.data.length > 0) {
          const cam = res.data[0]
          setVideoStartTime(cam.from_timestamp ?? 0)
          setVideoEndTime(cam.to_timestamp ?? 0)
        }
      })
      .catch(() => setCameras([]))
      .finally(() => setLoading(false))
  }, [datasetKey, episodeIndex])

  // Animation frame loop for time display - only runs when playing
  const updateTime = useCallback(() => {
    const primary = primaryKeyRef.current ? videoRefs.current.get(primaryKeyRef.current) : null
    if (primary) {
      setCurrentTime(primary.currentTime)
    }
    animFrameRef.current = requestAnimationFrame(updateTime)
  }, [])

  useEffect(() => {
    if (!playing) return
    animFrameRef.current = requestAnimationFrame(updateTime)
    return () => cancelAnimationFrame(animFrameRef.current)
  }, [playing, updateTime])

  // Register a video element
  const registerVideo = useCallback((el: HTMLVideoElement | null, key: string) => {
    if (!el) return
    videoRefs.current.set(key, el)
    if (!primaryKeyRef.current) {
      primaryKeyRef.current = key
    }
  }, [])

  // Called when a video's metadata is loaded (duration is now valid)
  const handleMetadataLoaded = useCallback((key: string) => {
    const video = videoRefs.current.get(key)
    const cam = camInfoByKey.current.get(key)
    if (video && cam && isFinite(video.duration)) {
      // Seek each video to its OWN from_timestamp (different per camera when
      // their bundled files are split at different boundaries).
      video.currentTime = cam.from_timestamp ?? 0
    }
    if (key === primaryKeyRef.current) {
      if (video && isFinite(video.duration)) {
        setDuration(video.duration)
        setReady(true)
        if (videoStartTime > 0) {
          setCurrentTime(videoStartTime)
        }
      }
    }
  }, [videoStartTime])

  const togglePlay = useCallback(() => {
    if (!ready) return
    const videos = Array.from(videoRefs.current.values())
    if (playing) {
      videos.forEach(v => v.pause())
      setPlaying(false)
    } else {
      // Sync every video to its own offset relative to the primary's time.
      const primary = primaryKeyRef.current ? videoRefs.current.get(primaryKeyRef.current) : null
      const primaryTime = primary?.currentTime ?? 0
      applyPerCamTime(primaryTime)
      videos.forEach(v => {
        v.playbackRate = playbackRate
        v.play()
      })
      setPlaying(true)
    }
  }, [playing, ready, playbackRate, applyPerCamTime])

  const seek = useCallback((time: number) => {
    // `time` is in the primary camera's file-absolute coordinate (matches
    // the scrubber/range input that uses videoStartTime/videoEndTime bounds).
    applyPerCamTime(time)
    setCurrentTime(time)
    // Report episode-relative frame index
    onFrameChange?.(Math.max(0, Math.floor((time - videoStartTime) * effectiveFps)))
  }, [applyPerCamTime, onFrameChange, effectiveFps, videoStartTime])

  const stepFrame = useCallback((direction: 1 | -1) => {
    const videos = Array.from(videoRefs.current.values())
    videos.forEach(v => v.pause())
    setPlaying(false)
    const primary = primaryKeyRef.current ? videoRefs.current.get(primaryKeyRef.current) : null
    const cur = primary?.currentTime ?? currentTime
    const endTime = videoEndTime || duration
    const newTime = Math.max(videoStartTime, Math.min(endTime, cur + direction / effectiveFps))
    seek(newTime)
  }, [currentTime, duration, effectiveFps, seek, videoStartTime, videoEndTime])

  const seekToFrame = useCallback((frame: number) => {
    const videos = Array.from(videoRefs.current.values())
    videos.forEach(v => v.pause())
    setPlaying(false)
    seek(videoStartTime + frame / effectiveFps)
  }, [effectiveFps, seek, videoStartTime])

  const seekToTimestamp = useCallback((ts: number) => {
    const videos = Array.from(videoRefs.current.values())
    videos.forEach(v => v.pause())
    setPlaying(false)
    seek(videoStartTime + ts)
  }, [seek, videoStartTime])

  const changeSpeed = useCallback((rate: number) => {
    setPlaybackRate(rate)
    videoRefs.current.forEach(v => {
      v.playbackRate = rate
    })
  }, [])

  const cycleSpeed = useCallback((direction: 1 | -1) => {
    setPlaybackRate(prev => {
      const idx = SPEED_LADDER.indexOf(prev)
      const nextIdx = idx === -1
        ? SPEED_LADDER.indexOf(1)
        : Math.max(0, Math.min(SPEED_LADDER.length - 1, idx + direction))
      const rate = SPEED_LADDER[nextIdx]
      videoRefs.current.forEach(v => { v.playbackRate = rate })
      return rate
    })
  }, [])

  useImperativeHandle(ref, () => ({
    stepFrame,
    seekToFrame,
    seekToTimestamp,
    togglePlay,
    cycleSpeed,
  }), [stepFrame, seekToFrame, seekToTimestamp, togglePlay, cycleSpeed])

  const handleVideoEnd = useCallback(() => {
    setPlaying(false)
    const videos = Array.from(videoRefs.current.values())
    videos.forEach(v => v.pause())
  }, [])

  // Sync secondary videos to primary during playback (per-camera offsets).
  const handlePrimaryTimeUpdate = useCallback(() => {
    const primary = primaryKeyRef.current ? videoRefs.current.get(primaryKeyRef.current) : null
    if (!primary) return
    setCurrentTime(primary.currentTime)
    const primaryCam = primaryKeyRef.current
      ? camInfoByKey.current.get(primaryKeyRef.current)
      : null
    const primaryFrom = primaryCam?.from_timestamp ?? 0
    const relative = Math.max(0, primary.currentTime - primaryFrom)
    onFrameChange?.(Math.floor(relative * effectiveFps))
    // Resync any secondary that drifts from its target (from + relative)
    videoRefs.current.forEach((video, key) => {
      if (key === primaryKeyRef.current) return
      const cam = camInfoByKey.current.get(key)
      const target = (cam?.from_timestamp ?? 0) + relative
      if (Math.abs(video.currentTime - target) > 0.1) {
        video.currentTime = target
      }
    })
  }, [onFrameChange, effectiveFps])

  // Episode-relative frame numbers
  const currentFrame = Math.max(0, Math.floor((currentTime - videoStartTime) * effectiveFps))
  const totalFrames = videoEndTime > 0
    ? Math.floor((videoEndTime - videoStartTime) * effectiveFps)
    : Math.floor(duration * effectiveFps)

  if (episodeIndex === null) {
    return (
      <div style={styles.empty}>
        <div style={styles.emptyIcon}>&#9654;</div>
        <div style={styles.emptyText}>Select an episode to view</div>
        <div style={styles.emptyHint}>Use arrow keys or click from the list</div>
      </div>
    )
  }

  if (loading) {
    return (
      <div style={styles.empty}>
        <div style={styles.emptyText}>Loading cameras...</div>
      </div>
    )
  }

  if (cameras.length === 0) {
    return (
      <div style={styles.empty}>
        <div style={styles.emptyText}>No video streams for episode #{episodeIndex}</div>
      </div>
    )
  }

  const gridCols = cameras.length <= 1 ? 1 : cameras.length <= 4 ? 2 : 3

  return (
    <div style={styles.container}>
      <div style={{
        ...styles.grid,
        gridTemplateColumns: `repeat(${gridCols}, 1fr)`,
      }}>
        {cameras.map((cam, i) => {
          const isPrimary = i === 0
          return (
            <div key={cam.key} style={styles.videoCell}>
              <div style={styles.cameraLabel}>{cam.label}</div>
              <video
                ref={el => registerVideo(el, cam.key)}
                src={cam.url}
                style={styles.video}
                muted
                playsInline
                preload="auto"
                onLoadedMetadata={() => handleMetadataLoaded(cam.key)}
                onEnded={isPrimary ? handleVideoEnd : undefined}
                onTimeUpdate={isPrimary ? handlePrimaryTimeUpdate : undefined}
              />
            </div>
          )
        })}
      </div>

      <div style={styles.controls}>
        <div style={styles.controlsRow}>
          <button style={styles.ctrlBtn} onClick={() => stepFrame(-1)} title="Previous frame (←)">
            &#9664;&#9664;
          </button>
          <button
            style={{
              ...styles.playBtn,
              opacity: ready ? 1 : 0.4,
            }}
            onClick={togglePlay}
            title="Play/Pause (Space)"
            disabled={!ready}
          >
            {playing ? '\u23F8' : '\u25B6'}
          </button>
          <button style={styles.ctrlBtn} onClick={() => stepFrame(1)} title="Next frame (→)">
            &#9654;&#9654;
          </button>

          {terminalFrames.length > 0 && (
            <button
              style={{ ...styles.ctrlBtn, color: 'var(--c-red)', borderColor: '#4a2a2a' }}
              onClick={() => {
                // Jump to next terminal frame after current position; wrap around
                const next = terminalFrames.find(f => f > currentFrame) ?? terminalFrames[0]
                seekToFrame(next)
              }}
              title={`Jump to next terminal frame (${terminalFrames.length} total)`}
            >
              {'\u23ED'}
            </button>
          )}

          <div style={{ flex: 1, position: 'relative' }}>
            <input
              type="range"
              min={videoStartTime}
              max={videoEndTime || duration || 1}
              step={0.001}
              value={currentTime}
              onChange={e => seek(parseFloat(e.target.value))}
              style={{ ...styles.scrubber, flex: 'none', width: '100%' }}
            />
            {totalFrames > 0 && terminalFrames.map((f, i) => (
              <div
                key={i}
                style={{
                  position: 'absolute',
                  left: `${(f / totalFrames) * 100}%`,
                  top: '50%',
                  transform: 'translate(-50%, -50%)',
                  width: '12px',
                  height: '14px',
                  cursor: 'pointer',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  zIndex: 3,
                }}
                onClick={() => seekToFrame(f)}
                title={`Terminal frame ${f}`}
              >
                <div style={{
                  width: '2px',
                  height: '10px',
                  background: 'var(--c-red)',
                  opacity: 0.7,
                  borderRadius: '1px',
                  pointerEvents: 'none',
                }} />
              </div>
            ))}
          </div>

          <span style={styles.timeLabel}>
            {Math.max(0, currentTime - videoStartTime).toFixed(1)}s / {((videoEndTime || duration) - videoStartTime).toFixed(1)}s
          </span>

          <div style={styles.speedGroup} title="Q: faster · W: slower">
            {SPEED_LADDER.map(rate => (
              <button
                key={rate}
                style={{
                  ...styles.speedBtn,
                  ...(playbackRate === rate ? styles.speedActive : {}),
                  ...(defaultRate === rate ? { borderBottom: '2px solid var(--interactive)' } : {}),
                }}
                onClick={() => changeSpeed(rate)}
                onDoubleClick={() => setAsDefault(rate)}
                title={`${rate}x${defaultRate === rate ? ' (default)' : ''} — Q faster, W slower · double-click to set as default`}
              >
                {rate}x
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
})

const styles: Record<string, React.CSSProperties> = {
  container: {
    display: 'flex',
    flexDirection: 'column',
    flex: 1,
    overflow: 'hidden',
    background: 'var(--bg-deep)',
  },
  grid: {
    display: 'grid',
    flex: 1,
    gap: '2px',
    padding: '2px',
    overflow: 'hidden',
    alignItems: 'center',
    background: 'var(--bg-deep)',
    minHeight: 0,
  },
  videoCell: {
    position: 'relative',
    overflow: 'hidden',
    borderRadius: '4px',
    background: 'var(--panel3)',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    minWidth: 0,
    minHeight: 0,
  },
  cameraLabel: {
    position: 'absolute',
    top: '6px',
    left: '8px',
    fontSize: '12px',
    fontWeight: 600,
    color: 'rgba(255,255,255,0.8)',
    background: 'rgba(0,0,0,0.5)',
    padding: '2px 8px',
    borderRadius: '3px',
    zIndex: 2,
    textTransform: 'uppercase',
    letterSpacing: '0.05em',
  },
  video: {
    width: '100%',
    height: '100%',
    objectFit: 'contain',
  },
  controls: {
    background: 'var(--panel)',
    borderTop: '1px solid var(--border2)',
    padding: '8px 16px',
    flexShrink: 0,
  },
  controlsRow: {
    display: 'flex',
    alignItems: 'center',
    gap: '8px',
  },
  ctrlBtn: {
    background: 'none',
    border: '1px solid var(--border3)',
    borderRadius: '4px',
    color: 'var(--text-muted)',
    padding: '4px 8px',
    fontSize: '12px',
    cursor: 'pointer',
    lineHeight: 1,
  },
  playBtn: {
    background: 'var(--interactive)',
    border: 'none',
    borderRadius: '50%',
    color: '#fff',
    width: '32px',
    height: '32px',
    fontSize: '14px',
    cursor: 'pointer',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    flexShrink: 0,
  },
  scrubber: {
    flex: 1,
    height: '4px',
    cursor: 'pointer',
    accentColor: 'var(--interactive)',
  },
  timeLabel: {
    fontSize: '12px',
    color: 'var(--text-muted)',
    fontFamily: 'var(--font-mono)',
    whiteSpace: 'nowrap',
    minWidth: '100px',
    textAlign: 'right',
  },
  speedGroup: {
    display: 'flex',
    gap: '2px',
    marginLeft: '8px',
  },
  speedBtn: {
    background: 'var(--panel2)',
    border: '1px solid var(--border3)',
    borderRadius: '3px',
    color: 'var(--text-dim)',
    fontSize: '11px',
    padding: '3px 7px',
    cursor: 'pointer',
    fontFamily: 'var(--font-mono)',
  },
  speedActive: {
    background: '#2a3a4a',
    borderColor: 'var(--interactive)',
    color: 'var(--interactive)',
  },
  empty: {
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    justifyContent: 'center',
    flex: 1,
    background: 'var(--bg-deep)',
    color: 'var(--text-dim)',
    gap: '8px',
  },
  emptyIcon: {
    fontSize: '36px',
    opacity: 0.5,
    width: '56px',
    height: '56px',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    border: '1px solid var(--border2)',
    borderRadius: '50%',
  },
  emptyText: {
    fontSize: '14px',
    color: 'var(--text-muted)',
  },
  emptyHint: {
    fontSize: '12px',
    color: 'var(--text-dim)',
  },
}
