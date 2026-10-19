import { useEffect, useRef } from 'react'

declare global {
  interface Window {
    onYouTubeIframeAPIReady?: () => void
  }
}

let apiPromise: Promise<void> | null = null
function loadApi(): Promise<void> {
  if (window.YT?.Player) return Promise.resolve()
  if (!apiPromise) {
    apiPromise = new Promise((resolve) => {
      window.onYouTubeIframeAPIReady = () => resolve()
      const s = document.createElement('script')
      s.src = 'https://www.youtube.com/iframe_api'
      document.head.appendChild(s)
    })
  }
  return apiPromise
}

interface Props {
  videoId: string
  onTime: (seconds: number) => void
  /** seconds behind the live edge to play (0 = live edge); needs the stream's DVR window */
  liveDelay?: number
  onDelay?: (behindLive: number) => void
  /** recordings limited to [start, end] seconds: the player opens at start and is kept inside */
  clip?: [number, number]
}

export function Player({ videoId, onTime, liveDelay = 0, onDelay, clip }: Props) {
  const clipRef = useRef(clip)
  clipRef.current = clip
  const host = useRef<HTMLDivElement>(null)
  const player = useRef<YT.Player | null>(null)
  const onTimeRef = useRef(onTime)
  useEffect(() => {
    onTimeRef.current = onTime
  }, [onTime])

  const onDelayRef = useRef(onDelay)
  useEffect(() => {
    onDelayRef.current = onDelay
  }, [onDelay])

  // Broadcast delay: once the live stream is playing, seek back into the DVR window so the
  // viewer hears each sentence when its verdict is already there. Pre-roll ads and the player
  // snapping back to the edge are handled by re-checking and re-seeking a few times; the final
  // distance behind the edge is reported via onDelay.
  const prevDelay = useRef(0)
  const edgeRef = useRef<{ pos: number; wall: number } | null>(null)
  // YouTube's getDuration() is unreliable on live streams (stale / off by an hour), so the live
  // edge is estimated from the first PLAYING position (autoplay starts at the edge) + wall time.
  useEffect(() => {
    const was = prevDelay.current
    prevDelay.current = liveDelay
    if (!liveDelay) {
      const p = player.current
      const e = edgeRef.current
      if (was && p && e) p.seekTo(e.pos + (Date.now() - e.wall) / 1000 + 60, true) // past the edge -> live
      return
    }
    let attempts = 0
    let seekedAt = 0
    const poll = window.setInterval(() => {
      const p = player.current
      if (!p || typeof p.getCurrentTime !== 'function' || p.getPlayerState() !== 1) return
      const pos = p.getCurrentTime()
      const now = Date.now()
      if (pos < liveDelay + 5) return
      let e = edgeRef.current
      if (!e) e = edgeRef.current = { pos, wall: now }
      const edgeNow = e.pos + (now - e.wall) / 1000
      const behind = edgeNow - pos
      if (seekedAt && now - seekedAt < 5000) return
      if (seekedAt && Math.abs(behind - liveDelay) <= 5) {
        onDelayRef.current?.(behind)
        clearInterval(poll)
        return
      }
      // snapped back to the edge (no DVR) or the content started after an ad: re-sample the edge
      if (seekedAt && behind < 3) e = edgeRef.current = { pos, wall: now }
      if (attempts >= 6) {
        onDelayRef.current?.(Math.max(0, behind))
        clearInterval(poll)
        return
      }
      attempts += 1
      seekedAt = now
      p.seekTo(e.pos + (now - e.wall) / 1000 - liveDelay, true)
    }, 1000)
    return () => clearInterval(poll)
  }, [liveDelay, videoId])

  useEffect(() => {
    let cancelled = false
    let timer: number | undefined
    loadApi().then(() => {
      if (cancelled || !host.current) return
      const el = document.createElement('div')
      host.current.replaceChildren(el)
      edgeRef.current = null
      player.current = new YT.Player(el, {
        videoId,
        playerVars: { autoplay: 1, rel: 0, modestbranding: 1 },
        events: {
          onReady: (e: YT.PlayerEvent) => {
            e.target.mute()
            const c = clipRef.current
            if (c && e.target.getCurrentTime() < c[0]) e.target.seekTo(c[0], true)
            e.target.playVideo()
          },
        },
      })
      timer = window.setInterval(() => {
        const p = player.current
        if (p && typeof p.getCurrentTime === 'function') {
          const c = clipRef.current
          const pos = p.getCurrentTime()
          if (c && (pos < c[0] - 1 || pos > c[1] + 1)) {
            p.seekTo(pos < c[0] - 1 ? c[0] : c[1] - 1, true)
            if (pos > c[1] + 1) p.pauseVideo()
            return
          }
          onTimeRef.current(pos)
        }
      }, 500)
    })
    return () => {
      cancelled = true
      if (timer) clearInterval(timer)
      player.current?.destroy()
      player.current = null
    }
  }, [videoId])

  return <div className="player" ref={host} />
}
