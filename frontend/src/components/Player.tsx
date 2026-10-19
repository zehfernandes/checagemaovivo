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
}

export function Player({ videoId, onTime }: Props) {
  const host = useRef<HTMLDivElement>(null)
  const player = useRef<YT.Player | null>(null)
  const onTimeRef = useRef(onTime)
  useEffect(() => {
    onTimeRef.current = onTime
  }, [onTime])

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
