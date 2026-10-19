import { useEffect, useRef, useState } from 'react'
import type { Claim, Verdict } from '../types'
import { VERDICT_LABEL } from '../types'

const LANES = 7
const SPEED_PX_S = 120
const CHAR_PX = 9.5
const PADDING_PX = 140

interface Bullet {
  key: string
  claimId: string
  text: string
  verdict: Verdict
  explanation: string
  lane: number
  spawnedAt: number
  durMs: number
  widthPx: number
}

interface Props {
  claims: Claim[]
  isLive: boolean
  currentTime: number
  enabled: boolean
}

/** Danmu-style overlay: each claim flies right-to-left across the video.
 *  A pending bullet updates in place when its verdict lands while still in
 *  flight; otherwise the verdict gets its own bullet. */
export function Danmu({ claims, isLive, currentTime, enabled }: Props) {
  const box = useRef<HTMLDivElement>(null)
  const [stageW, setStageW] = useState(0)
  const [bullets, setBullets] = useState<Bullet[]>([])
  const seen = useRef(new Map<string, Verdict>())
  const laneFree = useRef<number[]>(Array(LANES).fill(0))

  useEffect(() => {
    const el = box.current
    if (!el) return
    const ro = new ResizeObserver(([e]) => setStageW(e.contentRect.width))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  useEffect(() => {
    if (!enabled) {
      // remember current claims so re-enabling doesn't burst-spawn the backlog
      for (const c of claims) seen.current.set(c.id, c.verdict)
      return
    }
    if (!stageW) return
    const now = Date.now()
    const spawned: Bullet[] = []
    const updates = new Map<string, Verdict>()
    for (const c of claims) {
      if (!isLive && c.start > currentTime + 1) continue
      const prev = seen.current.get(c.id)
      if (prev === c.verdict) continue
      seen.current.set(c.id, c.verdict)
      if (prev !== undefined) updates.set(c.id, c.verdict)
      if (prev === undefined || prev === 'pending') {
        if (prev === 'pending') continue
        const widthPx = c.text.length * CHAR_PX + PADDING_PX
        const durMs = ((stageW + widthPx) / SPEED_PX_S) * 1000
        const lane = pickLane(laneFree.current, now)
        laneFree.current[lane] = now + (widthPx / SPEED_PX_S) * 1000 + 600
        spawned.push({ key: `${c.id}-${now}`, claimId: c.id, text: c.text, verdict: c.verdict,
          explanation: c.explanation, lane, spawnedAt: now, durMs, widthPx })
      }
    }
    if (!spawned.length && !updates.size) return
    // oxlint-disable-next-line react/set-state-in-effect -- bullets are time/lane-keyed spawns, not derivable in render
    setBullets((bs) => {
      const alive = bs.filter((b) => now - b.spawnedAt < b.durMs + 500)
      const inFlight = new Set(alive.map((b) => b.claimId))
      const next = alive.map((b) => {
        const v = updates.get(b.claimId)
        if (!v) return b
        const c = claims.find((x) => x.id === b.claimId)
        return { ...b, verdict: v, explanation: c?.explanation ?? b.explanation }
      })
      for (const [id, v] of updates) {
        if (inFlight.has(id) || v === 'pending') continue
        const c = claims.find((x) => x.id === id)
        if (!c) continue
        const widthPx = c.text.length * CHAR_PX + PADDING_PX
        const durMs = ((stageW + widthPx) / SPEED_PX_S) * 1000
        const lane = pickLane(laneFree.current, now)
        laneFree.current[lane] = now + (widthPx / SPEED_PX_S) * 1000 + 600
        next.push({ key: `${id}-${now}`, claimId: id, text: c.text, verdict: v,
          explanation: c.explanation, lane, spawnedAt: now, durMs, widthPx })
      }
      return [...next, ...spawned]
    })
  }, [claims, isLive, currentTime, enabled, stageW])

  useEffect(() => {
    if (!bullets.length) return
    const t = setInterval(() => {
      const now = Date.now()
      setBullets((bs) => (bs.some((b) => now - b.spawnedAt > b.durMs + 500)
        ? bs.filter((b) => now - b.spawnedAt <= b.durMs + 500) : bs))
    }, 2000)
    return () => clearInterval(t)
  }, [bullets.length])

  return (
    <div ref={box} className="danmu" style={{ '--stage-w': `${stageW}px` } as React.CSSProperties}>
      {enabled && bullets.map((b) => (
        <div
          key={b.key}
          className={`danmu__item danmu__item--${b.verdict}`}
          style={{ top: `${6 + b.lane * 12}%`, animationDuration: `${b.durMs}ms` }}
          title={b.explanation || undefined}
        >
          <span className="danmu__badge">
            {b.verdict === 'pending' && <span className="spinner" />}
            {VERDICT_LABEL[b.verdict]}
          </span>
          {b.text}
        </div>
      ))}
    </div>
  )
}

function pickLane(laneFree: number[], now: number): number {
  // lane that has been free the longest (or frees soonest if all are busy)
  let best = 0
  for (let i = 1; i < laneFree.length; i++) if (laneFree[i] < laneFree[best]) best = i
  return laneFree[best] <= now ? best : best
}
