import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import './compact.css'
import { useSession } from './useSession'
import type { Claim, Room, Verdict } from './types'
import { VERDICT_LABEL, speakerLabel } from './types'

// /sem-video — the fact-checks without the video: what was said, by whom, and the verdict.
// One page for three screens: a phone (single feed), a desktop (latest claim + feed) and a TV
// (big latest claim + the last few, nothing that needs a mouse). ?tv=1 forces the TV layout.

type Filter = 'all' | Exclude<Verdict, 'pending'> | `speaker:${string}`
const FINAL: Exclude<Verdict, 'pending'>[] = ['fake', 'valid', 'inconclusive']
const PLURAL: Record<Exclude<Verdict, 'pending'>, string> = { fake: 'falsas', valid: 'verdadeiras', inconclusive: 'inconclusivas' }
const TV_SIDE = 6 // claims listed next to the featured one on a TV
// Lives only: the YouTube player sits ~12 s behind the live edge the server reads, so a claim held this
// long shows up about when an internet viewer hears it (?atraso=N overrides). Broadcast TV is ahead of
// YouTube, so "Assistindo em: TV" shows everything at once.
const DEFAULT_DELAY_S = 8
type Watching = 'internet' | 'tv'
function storedWatching(): Watching {
  try { return localStorage.getItem('sem-video:assistindo') === 'tv' ? 'tv' : 'internet' } catch { return 'internet' }
}
const FRESH_MS = 6000 // a just-arrived claim is highlighted this long
const UNNAMED = '' // claims with no identified speaker get their own row and filter, so every count adds up
const nameOf = (speaker: string) => (speaker === UNNAMED ? 'Sem identificação' : speakerLabel(speaker))
const total = (row: Record<Verdict, number>) => FINAL.reduce((n, v) => n + row[v], 0)

function hms(s: number) {
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = Math.floor(s % 60).toString().padStart(2, '0')
  return h ? `${h}:${m.toString().padStart(2, '0')}:${sec}` : `${m}:${sec}`
}

function ago(seconds: number) {
  if (seconds < 45) return 'agora'
  if (seconds < 3600) return `há ${Math.round(seconds / 60)} min`
  return `há ${Math.floor(seconds / 3600)} h`
}

function hostname(url: string) {
  try { return new URL(url).hostname.replace(/^www\./, '') } catch { return url }
}

export function Compact() {
  const params = new URLSearchParams(location.search)
  const tv = useTv(params.get('tv'))
  const session = useSession()
  const [rooms, setRooms] = useState<Room[] | null>(null)
  const [roomId, setRoomId] = useState(params.get('room') ?? '')
  const [filter, setFilter] = useState<Filter>('all')
  const [openId, setOpenId] = useState<string | null>(null)
  const closeModal = useCallback(() => setOpenId(null), [])
  const [now, setNow] = useState(() => Date.now())
  const [loadedAt] = useState(() => Date.now())
  const [watching, setWatching] = useState<Watching>(storedWatching)
  const asked = Number(params.get('atraso'))
  const internetDelay = params.get('atraso') !== null && Number.isFinite(asked) && asked >= 0 ? asked : DEFAULT_DELAY_S
  function pickWatching(w: Watching) {
    setWatching(w)
    try { localStorage.setItem('sem-video:assistindo', w) } catch { /* private mode: just this visit */ }
  }

  useEffect(() => {
    let alive = true
    const load = () => fetch('/rooms').then((r) => r.json()).then((d: Room[]) => alive && setRooms(d)).catch(() => alive && setRooms([]))
    load()
    const t = setInterval(load, 30_000)
    return () => { alive = false; clearInterval(t) }
  }, [])
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 15_000) // "há 2 min" labels
    return () => clearInterval(t)
  }, [])
  const room = rooms?.find((r) => r.id === roomId) ?? rooms?.[0]
  const url = room?.url

  // follow the room: subscribe, and quietly retry while its checking hasn't started or the connection dropped
  const started = useRef('')
  useEffect(() => {
    if (!url) return
    const isNew = url !== started.current
    const lost = session.state === 'error' && session.lost
    const waiting = session.state === 'error' && !lost
    if (!isNew && !lost && !waiting) return
    const t = setTimeout(() => { started.current = url; session.start(url, 'pt', 0, false, false, false, true) }, isNew ? 0 : lost ? 2000 : 15000)
    return () => clearTimeout(t)
    // oxlint-disable-next-line react-hooks/exhaustive-deps
  }, [url, session.state, session.lost])
  function pickRoom(id: string) {
    setRoomId(id)
    setFilter('all')
    const p = new URLSearchParams(location.search)
    p.set('room', id)
    history.replaceState(null, '', `?${p}`)
  }

  const live = session.info?.is_live ?? false
  const delay = live && watching === 'internet' ? internetDelay : 0
  // a delayed live needs a fine clock to reveal claims on time; otherwise the 15 s one (for "há 2 min") is enough
  useEffect(() => {
    if (!delay) return
    const t = setInterval(() => setNow(Date.now()), 500)
    return () => clearInterval(t)
  }, [delay])
  const serverNow = now / 1000 - session.clockOffset
  // a live's stream time (`start`) restarts at 0 whenever its run reconnects, so lives are ordered by when the
  // audio arrived; recordings by their position in the video
  const order = (c: Claim) => (live ? c.captured_at ?? c.start : c.start)
  const revealAt = (c: Claim) => (c.captured_at ?? 0) + delay // server clock (s)
  const claims = useMemo(
    () => session.claims.filter((c) => !delay || revealAt(c) <= serverNow).sort((a, b) => order(b) - order(a)), // newest first
    // oxlint-disable-next-line react-hooks/exhaustive-deps -- order/revealAt only read live, delay and the clock listed here
    [session.claims, live, delay, delay ? Math.floor(serverNow * 2) : 0],
  )
  const counts = useMemo(() => {
    const c: Record<Verdict, number> = { pending: 0, fake: 0, valid: 0, inconclusive: 0 }
    for (const cl of claims) c[cl.verdict]++
    return c
  }, [claims])
  const bySpeaker = useMemo(() => {
    const m = new Map<string, Record<Verdict, number>>()
    for (const c of claims) {
      const who = c.speaker || UNNAMED
      const row = m.get(who) ?? { pending: 0, fake: 0, valid: 0, inconclusive: 0 }
      row[c.verdict]++
      m.set(who, row)
    }
    // most checked first; the unidentified row always last
    return [...m].sort((a, b) => (a[0] === UNNAMED ? 1 : b[0] === UNNAMED ? -1 : total(b[1]) - total(a[1])))
  }, [claims])
  // the chart compares people on one absolute scale; unidentified claims are summed apart (they would dwarf everyone)
  const named = bySpeaker.filter(([name, row]) => name !== UNNAMED && total(row) > 0)
  const unnamed = bySpeaker.find(([name]) => name === UNNAMED)?.[1]
  const maxChecked = Math.max(1, ...named.map(([, row]) => total(row)))
  const shown = useMemo(() => claims.filter((c) =>
    filter === 'all' ? true : filter.startsWith('speaker:') ? (c.speaker || UNNAMED) === filter.slice(8) : c.verdict === filter), [claims, filter])
  const featured = claims[0]
  const opened = openId ? session.claims.find((c) => c.id === openId) ?? null : null
  // highlight claims that appear while watching (arrival, or the end of their delay), not the history burst
  const shownAt = (c: Claim) => Math.max(c.receivedAt, delay && c.captured_at ? (revealAt(c) + session.clockOffset) * 1000 : 0)
  const fresh = (c: Claim) => shownAt(c) - loadedAt > 3000 && now - shownAt(c) < FRESH_MS
  const when = (c: Claim) => (live && c.captured_at ? ago(serverNow - c.captured_at) : hms(c.start))

  // what a viewer needs to know: is this happening now? (a recording's run being paused is a newsroom detail)
  const status = session.state === 'error' ? 'Aguardando' : session.state === 'resolving' || session.state === 'connecting' ? 'Conectando'
    : live ? (session.state === 'listening' ? 'Ao vivo' : 'Ao vivo · pausado') : 'Gravação'
  const empty = session.state === 'error' ? session.statusText || 'A checagem desta transmissão ainda não começou.'
    : session.state === 'connecting' || session.state === 'resolving' ? 'Conectando…' : 'Aguardando as primeiras falas…'

  return (
    <div className={`compact${tv ? ' compact--tv' : ''}`}>
      <header className="compact__bar">
        <span className="compact__brand"><span className="compact__dot" aria-hidden />Checagem ao vivo</span>
        {rooms && rooms.length > 1 ? (
          <select className="compact__room" value={room?.id ?? ''} onChange={(e) => pickRoom(e.target.value)} aria-label="Transmissão">
            {rooms.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
          </select>
        ) : <span className="compact__room compact__room--static">{room?.name ?? ''}</span>}
        <span className={`compact__state compact__state--${live && session.state === 'listening' ? 'live' : session.state}`}>{status}</span>
        {live && (
          <div className="compact__watching" role="group" aria-label="Assistindo em">
            <span>Assistindo em:</span>
            {(['internet', 'tv'] as const).map((w) => (
              <button key={w} type="button" aria-pressed={watching === w} className={watching === w ? 'active' : ''} onClick={() => pickWatching(w)}
                title={w === 'internet' ? `Segura cada checagem ${internetDelay} s para chegar junto com a fala no YouTube` : 'Mostra cada checagem assim que fica pronta'}>
                {w === 'internet' ? 'Internet' : 'TV'}
              </button>
            ))}
          </div>
        )}
        <div className="compact__score" aria-label="Placar">
          {FINAL.map((v) => (
            <span key={v} className={`compact__count compact__count--${v}`}><strong>{counts[v]}</strong> {PLURAL[v]}</span>
          ))}
          {counts.pending > 0 && <span className="compact__count compact__count--pending"><strong>{counts.pending}</strong> verificando</span>}
        </div>
      </header>

      {claims.length === 0 ? (
        <main className="compact__empty"><p>{empty}</p></main>
      ) : (
        <main className="compact__main">
          {featured && (
            <section className="compact__now" aria-label="Última fala checada">
              <span className="compact__label">Agora</span>
              <ClaimView key={featured.id} claim={featured} when={when(featured)} featured fresh={fresh(featured)} />
              {(named.length > 0 || unnamed) && (
                <div className="compact__people">
                  {named.map(([name, row]) => (
                    <div key={name} className="compact__person">
                      <span className="compact__person-name">{nameOf(name)}</span>
                      <span className="compact__track">
                        {/* one scale for everyone: the bar's length is how many claims were checked, not a share */}
                        <span className="compact__bars" style={{ width: `calc((100% - 40px) * ${total(row) / maxChecked})` }}
                          aria-label={FINAL.map((v) => `${row[v]} ${PLURAL[v]}`).join(', ')}>
                          {FINAL.map((v) => row[v] > 0 && (
                            <span key={v} className={`compact__seg compact__seg--${v}`} style={{ flexGrow: row[v] }}>{row[v]}</span>
                          ))}
                        </span>
                        <span className="compact__person-total">{total(row)}</span>
                      </span>
                    </div>
                  ))}
                  {unnamed && total(unnamed) > 0 && (
                    <p className="compact__unnamed">
                      <strong>{total(unnamed)}</strong> sem identificação de quem falou:{' '}
                      {FINAL.filter((v) => unnamed[v] > 0).map((v) => `${unnamed[v]} ${PLURAL[v]}`).join(' · ')}
                    </p>
                  )}
                </div>
              )}
            </section>
          )}
          <section className="compact__feed" aria-label="Falas checadas">
            <nav className="compact__filters" aria-label="Filtrar">
              <Chip on={filter === 'all'} onClick={() => setFilter('all')}>Todas <em>{claims.length}</em></Chip>
              {FINAL.map((v) => counts[v] > 0 && (
                <Chip key={v} kind={v} on={filter === v} onClick={() => setFilter(v)}>{VERDICT_LABEL[v]} <em>{counts[v]}</em></Chip>
              ))}
              {bySpeaker.map(([name, row]) => (
                <Chip key={name || '-'} on={filter === `speaker:${name}`} onClick={() => setFilter(`speaker:${name}`)}>
                  {nameOf(name)} <em>{total(row) + row.pending}</em>
                </Chip>
              ))}
            </nav>
            <ol className="compact__list" aria-live="polite">
              {shown.length === 0 && <li className="compact__none">Nada com esse filtro.</li>}
              {shown.map((c, i) => (
                <li key={c.id} className={`compact__item${i >= TV_SIDE + 1 ? ' compact__item--more' : ''}${c.id === featured?.id ? ' compact__item--featured' : ''}`}>
                  <ClaimView claim={c} when={when(c)} fresh={fresh(c)} onOpen={() => setOpenId(c.id)} />
                </li>
              ))}
            </ol>
          </section>
        </main>
      )}
      {opened && <ClaimModal claim={opened} when={when(opened)} onClose={closeModal} />}
    </div>
  )
}

// TV layout: ?tv=1 forces it, ?tv=0 turns it off, otherwise any big landscape screen gets it
const TV_QUERY = '(min-width: 1600px) and (min-height: 700px)'
function useTv(param: string | null): boolean {
  const [big, setBig] = useState(() => window.matchMedia(TV_QUERY).matches)
  useEffect(() => {
    const mq = window.matchMedia(TV_QUERY)
    const on = () => setBig(mq.matches)
    mq.addEventListener('change', on)
    return () => mq.removeEventListener('change', on)
  }, [])
  return param === '1' || (param !== '0' && big)
}

function Chip({ on, kind, onClick, children }: { on: boolean; kind?: string; onClick: () => void; children: ReactNode }) {
  return <button type="button" className={`compact__chip${kind ? ` compact__chip--${kind}` : ''}${on ? ' active' : ''}`} aria-pressed={on} onClick={onClick}>{children}</button>
}

function ClaimView({ claim: c, when, featured, fresh, detailed, onOpen }: {
  claim: Claim; when: string; featured?: boolean; fresh?: boolean; detailed?: boolean; onOpen?: () => void
}) {
  const full = featured || detailed // the featured claim and the modal show the explanation and sources
  // a list item is one big target: click/Enter opens the details
  const open = onOpen && {
    role: 'button', tabIndex: 0, 'aria-haspopup': 'dialog' as const, onClick: onOpen,
    onKeyDown: (e: KeyboardEvent<HTMLElement>) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpen() } },
  }
  return (
    <article className={`cclaim cclaim--${c.verdict}${featured ? ' cclaim--featured' : ''}${detailed ? ' cclaim--detailed' : ''}${fresh ? ' cclaim--fresh' : ''}${onOpen ? ' cclaim--link' : ''}`} {...open}>
      <header className="cclaim__head">
        <span className="cclaim__verdict">{c.verdict === 'pending' && <span className="cclaim__spin" aria-hidden />}{VERDICT_LABEL[c.verdict]}</span>
        {c.deep_pending && <span className="cclaim__deep">checagem profunda…</span>}
        <span className="cclaim__when">{when}</span>
      </header>
      <p className="cclaim__quote">
        {c.speaker && <span className="cclaim__speaker">{speakerLabel(c.speaker)}: </span>}
        “{c.quote || c.text}”
      </p>
      {full && c.explanation && <p className="cclaim__why">{c.explanation}</p>}
      {full && c.verdict === 'pending' && !c.explanation && <p className="cclaim__why">A explicação aparece assim que a checagem terminar.</p>}
      {full && c.sources.length > 0 && (
        <ul className="cclaim__sources">
          {c.sources.map((s) => <li key={s.url}><a href={s.url} target="_blank" rel="noreferrer">{s.title || hostname(s.url)}</a></li>)}
        </ul>
      )}
    </article>
  )
}

function ClaimModal({ claim, when, onClose }: { claim: Claim; when: string; onClose: () => void }) {
  const close = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    const back = document.activeElement as HTMLElement | null // focus returns to the item that opened it
    close.current?.focus()
    const onKey = (e: globalThis.KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    const scroll = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => { window.removeEventListener('keydown', onKey); document.body.style.overflow = scroll; back?.focus() }
  }, [onClose])
  return (
    <div className="cmodal" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <div className="cmodal__box" role="dialog" aria-modal="true" aria-label="Detalhes da checagem">
        <button ref={close} type="button" className="cmodal__close" onClick={onClose} aria-label="Fechar">×</button>
        <ClaimView claim={claim} when={when} featured detailed />
      </div>
    </div>
  )
}
