import type { Claim } from '../types'
import { VERDICT_LABEL } from '../types'

interface Props {
  claim: Claim
  compact?: boolean
  onRecheck?: (id: string) => void
}

const METHOD_LABEL = { cache: 'Cache', db: 'DB', web: 'Web', deep: 'Deep' } as const
const METHOD_TITLE = {
  cache: 'Reused the verdict of an identical claim settled earlier (no AI calls)',
  db: 'Answered from the news database',
  web: 'Answered via live web search',
  deep: 'Answered by the deep check (stronger model, multiple searches)',
} as const

export function ClaimCard({ claim, compact, onRecheck }: Props) {
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
        {claim.method && (
          <span className={`claim__method claim__method--${claim.method}`} title={METHOD_TITLE[claim.method]}>
            {METHOD_LABEL[claim.method]}
          </span>
        )}
        {claim.deep_pending && (
          <span className="claim__deep" title="Running a deeper check: stronger model, several web searches">
            <span className="spinner" aria-hidden /> Deep check…
          </span>
        )}
        <span className="claim__time">{fmt(claim.start)}</span>
      </header>
      <p className="claim__text">“{claim.text}”</p>
      {!compact && claim.captured_at && claim.extracted_at && (
        <p className="claim__latency" title="Seconds after the audio arrived from YouTube (server-side)">
          ⏱ claim +{(claim.extracted_at - claim.captured_at).toFixed(1)}s
          {claim.verified_at && ` · verdict +${(claim.verified_at - claim.captured_at).toFixed(1)}s`}
        </p>
      )}
      {claim.explanation && <p className="claim__explanation">{claim.explanation}</p>}
      {!compact && onRecheck && claim.verdict !== 'pending' && !claim.deep_pending && (
        <button type="button" className="btn btn--ghost btn--small" onClick={() => onRecheck(claim.id)}>
          Re-check
        </button>
      )}
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
