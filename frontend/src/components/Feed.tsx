import { useContext, useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import '../feed.css'
import type { Claim, Source, Verdict } from '../types'
import { FINAL, PLURAL, PUBLIC_VERDICT, PhotosContext, UNNAMED, hostOf, humanReviewed, claimLink, publisherOf, totalOf, type Filters } from './feedData'

// The viewer's visual language, shared by / and /sem-video: who said it (photo + name), the sentence in
// their words, the verdict as a solid pill, who decided it (a person or the machine) and the sources' logos.

// ---- speaker photo: the one registered for the room, else /speakers/<slug>.jpg when the file exists, else the initials
const slug = (name: string) => name.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')
const initials = (name: string) => name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join('').toUpperCase()
const missingPhotos = new Set<string>() // one 404 per name per visit

export function Avatar({ name }: { name: string }) {
  const registered = useContext(PhotosContext).get(name)
  const file = `/speakers/${slug(name)}.jpg`
  const [failed, setFailed] = useState(() => !name || missingPhotos.has(file))
  return (
    <span className="favatar" aria-hidden>
      {registered ? <img src={registered} alt="" />
        : failed ? <span className="favatar__initials">{name ? initials(name) : '?'}</span>
        : <img src={file} alt="" onError={() => { missingPhotos.add(file); setFailed(true) }} />}
    </span>
  )
}

// ---- verdict and review ----------------------------------------------------------------------------
function VerdictIcon({ verdict }: { verdict: Verdict }) {
  if (verdict === 'pending') return <span className="fspin" aria-hidden />
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      {verdict === 'valid' && <path d="M2.5 7.5 5.5 10.5 11.5 3.5" />}
      {verdict === 'fake' && <path d="M3.5 3.5 10.5 10.5M10.5 3.5 3.5 10.5" />}
      {verdict === 'inconclusive' && <path d="M5 5a2 2 0 1 1 2.8 1.8C7.3 7 7 7.4 7 8M7 10.8v.2" />}
    </svg>
  )
}

export function VerdictPill({ verdict }: { verdict: Verdict }) {
  return <span className={`fpill fpill--${verdict}`}><VerdictIcon verdict={verdict} />{PUBLIC_VERDICT[verdict]}</span>
}

export function ReviewChip({ claim: c }: { claim: Claim }) {
  if (c.deep_pending) return <span className="fchip"><span className="fspin" aria-hidden />Checagem profunda</span>
  if (c.verdict === 'pending') return null
  return <span className="fchip">{humanReviewed(c) ? 'Revisão humana' : 'Revisão por IA'}</span>
}

// ---- sources: the outlet's logo (its favicon) and name ---------------------------------------------------
const logoUrl = (url: string) => `https://www.google.com/s2/favicons?domain=${encodeURIComponent(hostOf(url))}&sz=64`

export function SourceLogo({ url }: { url: string }) {
  const [failed, setFailed] = useState(false)
  return (
    <span className="flogo" aria-hidden>
      {failed ? <span className="flogo__letter">{publisherOf(url)[0]?.toUpperCase()}</span>
        : <img src={logoUrl(url)} alt="" referrerPolicy="no-referrer" onError={() => setFailed(true)} />}
    </span>
  )
}

function uniqueOutlets(sources: Source[]) {
  const seen = new Set<string>()
  return sources.filter((s) => { const h = publisherOf(s.url); if (seen.has(h)) return false; seen.add(h); return true })
}

function SourceStack({ sources }: { sources: Source[] }) {
  const outlets = uniqueOutlets(sources)
  if (outlets.length === 0) return null
  return (
    <span className="fstack" title={outlets.map((s) => publisherOf(s.url)).join(', ')}>
      {outlets.slice(0, 3).map((s) => <SourceLogo key={s.url} url={s.url} />)}
    </span>
  )
}

const Chevron = () => (
  <svg className="fcard__chev" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
    <path d="M4 6l4 4 4-4" />
  </svg>
)

// ---- a claim in the list: tap to open the details --------------------------------------------------------
export function FeedCard({ claim: c, when, fresh, onOpen, featured }: {
  claim: Claim; when?: string; fresh?: boolean; onOpen: () => void; featured?: boolean
}) {
  const name = c.speaker || UNNAMED
  const onKey = (e: KeyboardEvent<HTMLElement>) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpen() } }
  return (
    <article className={`fcard fcard--${c.verdict}${fresh ? ' fcard--fresh' : ''}${featured ? ' fcard--featured' : ''}`}
      role="button" tabIndex={0} aria-haspopup="dialog" onClick={onOpen} onKeyDown={onKey}>
      <header className="fcard__who">
        <Avatar name={name} />
        <span className="fcard__name">{name || 'Sem identificação'}</span>
        {when && <span className="fcard__when">{when}</span>}
      </header>
      <p className="fcard__quote">“{c.quote || c.text}”</p>
      {featured && c.explanation && <p className="fcard__why">{c.explanation}</p>}
      <footer className="fcard__foot">
        <VerdictPill verdict={c.verdict} />
        <ReviewChip claim={c} />
        <span className="fspacer" />
        <SourceStack sources={c.sources} />
        <Chevron />
      </footer>
    </article>
  )
}

// ---- the details: full screen on a phone, a dialog on bigger screens ----------------------------------------
function useDialog(onClose: () => void) {
  const first = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    const back = document.activeElement as HTMLElement | null // focus returns to the card that opened it
    first.current?.focus()
    const onKey = (e: globalThis.KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    const scroll = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => { window.removeEventListener('keydown', onKey); document.body.style.overflow = scroll; back?.focus() }
  }, [onClose])
  return first
}

async function share(c: Claim): Promise<'shared' | 'copied' | null> {
  const url = claimLink(c.id)
  const text = `${c.speaker ? `${c.speaker}: ` : ''}“${c.quote || c.text}” — ${PUBLIC_VERDICT[c.verdict]}`
  try {
    if (navigator.share) { await navigator.share({ title: 'Checagem ao vivo', text, url }); return 'shared' }
    await navigator.clipboard.writeText(`${text}\n${url}`)
    return 'copied'
  } catch {
    return null // cancelled, or no clipboard access
  }
}

export function ClaimSheet({ claim: c, when, onClose }: { claim: Claim; when?: string; onClose: () => void }) {
  const close = useDialog(onClose)
  const [copied, setCopied] = useState(false)
  const name = c.speaker || UNNAMED
  return (
    <div className="fsheet" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <div className={`fsheet__box fsheet__box--${c.verdict}`} role="dialog" aria-modal="true" aria-label="Detalhes da checagem">
        <header className="fsheet__head">
          <Avatar name={name} />
          <span className="fsheet__name">{name || 'Sem identificação'}</span>
          <span className="fspacer" />
          <button type="button" className="ficon" title={copied ? 'Link copiado' : 'Compartilhar'} aria-label="Compartilhar"
            onClick={async () => { if (await share(c) === 'copied') { setCopied(true); setTimeout(() => setCopied(false), 2000) } }}>
            {copied ? <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden><path d="M4 10.5 8 14.5 16 5.5" /></svg>
              : <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden><path d="M10 13V2.5M6 6l4-4 4 4M4 10v6.5h12V10" /></svg>}
          </button>
          <button ref={close} type="button" className="ficon" onClick={onClose} aria-label="Fechar">
            <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden><path d="M4.5 4.5l11 11M15.5 4.5l-11 11" /></svg>
          </button>
        </header>
        <h2 className="fsheet__quote">“{c.quote || c.text}”</h2>
        <div className="fsheet__pills">
          <VerdictPill verdict={c.verdict} />
          <ReviewChip claim={c} />
          {when && <span className="fsheet__when">{when}</span>}
        </div>
        {c.explanation ? <p className="fsheet__why">{c.explanation}</p>
          : c.verdict === 'pending' && <p className="fsheet__why">A explicação aparece assim que a checagem terminar.</p>}
        {c.sources.length > 0 && (
          <ul className="fsheet__sources">
            {c.sources.map((s) => (
              <li key={s.url}>
                <a className="fsource" href={s.url} target="_blank" rel="noreferrer">
                  <span className="fsource__title">{s.title || hostOf(s.url)}</span>
                  <span className="fsource__foot">
                    <SourceLogo url={s.url} />
                    <span className="fsource__name">{publisherOf(s.url)}</span>
                    <svg className="fsource__go" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden><path d="M5 11l6-6M6 5h5v5" /></svg>
                  </span>
                </a>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}

// ---- score: one row per candidate, one absolute scale for everyone ------------------------------------------
// `reserve`: rows to keep room for from the start (the room's registered speakers), so what sits above doesn't move
export function ScoreBars({ rows, unnamed, reserve = 0 }: { rows: [string, Record<Verdict, number>][]; unnamed?: Record<Verdict, number>; reserve?: number }) {
  const named = rows.filter(([name, row]) => name !== UNNAMED && totalOf(row) > 0)
  if (named.length === 0 && !reserve) return null
  const max = Math.max(1, ...named.map(([, row]) => totalOf(row)))
  return (
    <section className="fscore" aria-label="Placar por candidato" style={reserve ? { minHeight: `calc(var(--fscore-pad) + ${reserve} * 32px + ${reserve - 1} * 6px)` } : undefined}>
      {named.map(([name, row]) => (
        <div key={name} className="fscore__row" title={name}>
          <Avatar name={name} />
          {/* the bar's length is how many claims were checked (same scale for everyone), split by verdict */}
          <span className="fscore__bars" style={{ width: `calc((100% - 44px) * ${totalOf(row) / max})` }}
            aria-label={`${name}: ${FINAL.map((v) => `${row[v]} ${PLURAL[v]}`).join(', ')}`}>
            {FINAL.map((v) => row[v] > 0 && (
              <span key={v} className={`fscore__seg fscore__seg--${v}`} style={{ flexGrow: row[v] }}>{row[v]}</span>
            ))}
          </span>
        </div>
      ))}
      {unnamed && totalOf(unnamed) > 0 && (
        <p className="fscore__unnamed">
          + {totalOf(unnamed)} sem identificação de quem falou: {FINAL.filter((v) => unnamed[v] > 0).map((v) => `${unnamed[v]} ${PLURAL[v]}`).join(' · ')}
        </p>
      )}
    </section>
  )
}

// ---- header: the broadcast's name and whether it is live -----------------------------------------------------
export function LiveChip({ label, live }: { label: string; live: boolean }) {
  return <span className={`flive${live ? ' flive--on' : ''}`}><span className="flive__dot" aria-hidden />{label}</span>
}

// ---- "138 checagens" + the filter button -----------------------------------------------------------------------

export function CountBar({ n, busy, filters, onFilter }: { n: number; busy: boolean; filters: Filters; onFilter: () => void }) {
  const active = filters.verdict !== 'all' || filters.speaker !== null
  return (
    <div className="fcount">
      {busy && <span className="fspin fspin--lg" aria-label="Checando" />}
      <span className="fcount__n">{n} {n === 1 ? 'checagem' : 'checagens'}</span>
      <span className="fspacer" />
      <button type="button" className={`ficon fcount__filter${active ? ' fcount__filter--on' : ''}`} onClick={onFilter} aria-label="Filtrar" title="Filtrar">
        <svg width="22" height="22" viewBox="0 0 22 22" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden>
          <path d="M3 7h9M16 7h3M3 15h3M10 15h9" /><circle cx="14" cy="7" r="2" /><circle cx="8" cy="15" r="2" />
        </svg>
      </button>
    </div>
  )
}

export function FilterSheet({ filters, onChange, onClose, counts, speakers, extra }: {
  filters: Filters; onChange: (f: Filters) => void; onClose: () => void
  counts: Record<Verdict, number>; speakers: [string, number][]; extra?: ReactNode
}) {
  const first = useDialog(onClose)
  const total = FINAL.reduce((n, v) => n + counts[v], 0) + counts.pending
  const opt = (on: boolean, label: ReactNode, n: number, pick: () => void, key?: string) => (
    <button key={key} type="button" className={`fopt${on ? ' fopt--on' : ''}`} aria-pressed={on} onClick={pick}>{label}<em>{n}</em></button>
  )
  return (
    <div className="fsheet fsheet--filters" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <div className="fsheet__box" role="dialog" aria-modal="true" aria-label="Filtrar checagens">
        <header className="fsheet__head">
          <span className="fsheet__title">Filtrar</span>
          <span className="fspacer" />
          <button ref={first} type="button" className="ficon" onClick={onClose} aria-label="Fechar">
            <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden><path d="M4.5 4.5l11 11M15.5 4.5l-11 11" /></svg>
          </button>
        </header>
        <h3 className="fsheet__label">Veredito</h3>
        <div className="fopts">
          {opt(filters.verdict === 'all', 'Todos', total, () => onChange({ ...filters, verdict: 'all' }))}
          {FINAL.map((v) => opt(filters.verdict === v, <><VerdictIcon verdict={v} />{PUBLIC_VERDICT[v]}</>, counts[v], () => onChange({ ...filters, verdict: v }), v))}
        </div>
        {speakers.length > 0 && <>
          <h3 className="fsheet__label">Quem falou</h3>
          <div className="fopts">
            {opt(filters.speaker === null, 'Todos', total, () => onChange({ ...filters, speaker: null }))}
            {speakers.map(([name, n]) => opt(filters.speaker === name, <><Avatar name={name} />{name || 'Sem identificação'}</>, n, () => onChange({ ...filters, speaker: name }), name || '-'))}
          </div>
        </>}
        {extra}
      </div>
    </div>
  )
}

