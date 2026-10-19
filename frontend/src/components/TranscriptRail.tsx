import { useEffect, useRef } from 'react'
import type { TranscriptSegment } from '../types'

export function TranscriptRail({ segments }: { segments: TranscriptSegment[] }) {
  const end = useRef<HTMLDivElement>(null)
  useEffect(() => end.current?.scrollIntoView({ behavior: 'smooth' }), [segments.length])
  return (
    <div className="transcript">
      {segments.length === 0 && <p className="muted">A transcrição aparece aqui.</p>}
      {segments.map((s) => (
        <p key={s.id} className="transcript__line">
          <span className="transcript__time">{fmt(s.start)}</span> {s.text}
        </p>
      ))}
      <div ref={end} />
    </div>
  )
}

function fmt(s: number) {
  const m = Math.floor(s / 60)
  const sec = Math.floor(s % 60)
  return `${m}:${sec.toString().padStart(2, '0')}`
}
