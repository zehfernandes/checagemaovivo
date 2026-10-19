import type { Claim } from '../types'
import { VERDICT_LABEL } from '../types'

interface Props {
  claim: Claim
  compact?: boolean
}

export function ClaimCard({ claim, compact }: Props) {
  return (
    <article className={`claim claim--${claim.verdict}${compact ? ' claim--compact' : ''}`}>
      <header className="claim__header">
        <span className="claim__badge">
          {claim.verdict === 'pending' && <span className="spinner" aria-hidden />}
          {VERDICT_LABEL[claim.verdict]}
        </span>
        {claim.verdict !== 'pending' && (
          <span className="claim__confidence">{Math.round(claim.confidence * 100)}%</span>
        )}
        <span className="claim__time">{fmt(claim.start)}</span>
      </header>
      <p className="claim__text">“{claim.text}”</p>
      {claim.explanation && <p className="claim__explanation">{claim.explanation}</p>}
      {!compact && claim.sources.length > 0 && (
        <ul className="claim__sources">
          {claim.sources.map((s) => (
            <li key={s.url}>
              <a href={s.url} target="_blank" rel="noreferrer">
                {s.title || hostname(s.url)}
              </a>
            </li>
          ))}
        </ul>
      )}
    </article>
  )
}

function fmt(s: number) {
  const m = Math.floor(s / 60)
  const sec = Math.floor(s % 60)
  return `${m}:${sec.toString().padStart(2, '0')}`
}

function hostname(url: string) {
  try {
    return new URL(url).hostname.replace(/^www\./, '')
  } catch {
    return url
  }
}
