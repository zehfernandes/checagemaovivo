import { useEffect, useState } from 'react'
import type { Claim } from '../types'
import { ClaimCard } from './ClaimCard'

const MAX_CARDS = 3
const SETTLED_TTL_MS = 25_000

interface Props {
  claims: Claim[]
  isLive: boolean
  currentTime: number
}

export function Overlay({ claims, isLive, currentTime }: Props) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [])

  const visible = claims
    .filter((c) => isLive || c.start <= currentTime + 1)
    .filter((c) => !c.settledAt || now - c.settledAt < SETTLED_TTL_MS)
    .slice(-MAX_CARDS)

  return (
    <div className="overlay">
      {visible.map((c) => (
        <ClaimCard key={c.id} claim={c} compact />
      ))}
    </div>
  )
}
