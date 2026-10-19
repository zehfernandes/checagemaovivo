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
}

export function Player({ videoId, onTime, liveDelay = 0, onDelay }: Props) {
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
  // viewer hears each sentence when its verdict is already there; report how far behind we ended up.
  useEffect(() => {
    if (!liveDelay) return
    let check: number | undefined
    const poll = window.setInterval(() => {
      const p = player.current
      if (!p || typeof p.getDuration !== 'function' || p.getPlayerState() !== 1) return
      const edge = p.getDuration()
      if (edge < liveDelay + 5) return
      clearInterval(poll)
      p.seekTo(edge - liveDelay, true)
      check = window.setTimeout(() => {
        const q = player.current
        if (q) onDelayRef.current?.(q.getDuration() - q.getCurrentTime())
      }, 4000)
    }, 1000)
    return () => {
      clearInterval(poll)
      if (check) clearTimeout(check)
    }
  }, [liveDelay, videoId])

  useEffect(() => {
    let cancelled = false
    let timer: number | undefined
    loadApi().then(() => {
      if (cancelled || !host.current) return
      const el = document.createElement('div')
      host.current.replaceChildren(el)
      player.current = new YT.Player(el, {
        videoId,
        playerVars: { autoplay: 1, rel: 0, modestbranding: 1 },
        events: {
          onReady: (e: YT.PlayerEvent) => {
            e.target.mute()
            e.target.playVideo()
          },
        },
      })
      timer = window.setInterval(() => {
        const p = player.current
        if (p && typeof p.getCurrentTime === 'function') onTimeRef.current(p.getCurrentTime())
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
