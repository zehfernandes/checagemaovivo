import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import './compact.css'
import { useSession } from './useSession'
import { FitSelect } from './components/FitSelect'
import { ClaimSheet, CountBar, FeedCard, FilterSheet, LiveChip, ScoreBars, ViewSwap } from './components/Feed'
import { NO_FILTER, PhotosContext, UNNAMED, applyFilters, applyRoster, photoMap, roomTitle, scoreRows, totalOf, useSharedClaim, type Filters } from './components/feedData'
import type { Claim, Room, Verdict } from './types'

// /sem-video — the fact-checks without the video: what was said, by whom, and the verdict.
// One page for three screens: a phone (single feed), a desktop (latest claim + feed) and a TV
// (big latest claim + the last few, nothing that needs a mouse). ?tv=1 forces the TV layout.

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

export function Compact() {
  const params = new URLSearchParams(location.search)
  const tv = useTv(params.get('tv'))
  const session = useSession()
  const [rooms, setRooms] = useState<Room[] | null>(null)
  const [roomId, setRoomId] = useState(params.get('room') ?? '')
  const [filters, setFilters] = useState<Filters>(NO_FILTER)
  const [filtering, setFiltering] = useState(false)
  const closeFilters = useCallback(() => setFiltering(false), [])
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
  // the room's registered speakers decide whose claims are shown (and the photos)
  const roster = room?.speakers
  const rosterClaims = useMemo(() => applyRoster(session.claims, roster), [session.claims, roster])
  const photos = useMemo(() => photoMap(roster), [roster])

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
    setFilters(NO_FILTER)
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
    () => rosterClaims.filter((c) => !delay || revealAt(c) <= serverNow).sort((a, b) => order(b) - order(a)), // newest first
    // oxlint-disable-next-line react-hooks/exhaustive-deps -- order/revealAt only read live, delay and the clock listed here
    [rosterClaims, live, delay, delay ? Math.floor(serverNow * 2) : 0],
  )
  const counts = useMemo(() => {
    const c: Record<Verdict, number> = { pending: 0, fake: 0, valid: 0, inconclusive: 0 }
    for (const cl of claims) c[cl.verdict]++
    return c
  }, [claims])
  const rows = useMemo(() => scoreRows(claims), [claims])
  const unnamed = rows.find(([name]) => name === UNNAMED)?.[1]
  const speakers = useMemo(() => rows.map(([name, row]) => [name, totalOf(row) + row.pending] as [string, number]), [rows])
  const shown = useMemo(() => applyFilters(claims, filters), [claims, filters])
  const featured = claims[0]
  const opened = openId ? rosterClaims.find((c) => c.id === openId) ?? null : null
  useSharedClaim(rosterClaims, setOpenId)
  // highlight claims that appear while watching (arrival, or the end of their delay), not the history burst
  const shownAt = (c: Claim) => Math.max(c.receivedAt, delay && c.captured_at ? (revealAt(c) + session.clockOffset) * 1000 : 0)
  const fresh = (c: Claim) => shownAt(c) - loadedAt > 3000 && now - shownAt(c) < FRESH_MS
  const when = (c: Claim) => (live && c.captured_at ? ago(serverNow - c.captured_at) : hms(c.start))

  // what a viewer needs to know: is this happening now? (a recording's run being paused is a newsroom detail)
  const status = session.state === 'error' ? 'Aguardando' : session.state === 'resolving' || session.state === 'connecting' ? 'Conectando'
    : live ? (session.state === 'listening' ? 'Ao vivo' : 'Ao vivo · pausado') : 'Gravado'
  const busy = counts.pending > 0 || (live && session.state === 'listening')
  const watchingToggle = live && (
    <div className="compact__watching" role="group" aria-label="Assistindo em">
      <span>Assistindo em</span>
      {(['internet', 'tv'] as const).map((w) => (
        <button key={w} type="button" aria-pressed={watching === w} className={`fopt${watching === w ? ' fopt--on' : ''}`} onClick={() => pickWatching(w)}
          title={w === 'internet' ? `Segura cada checagem ${internetDelay} s para chegar junto com a fala no YouTube` : 'Mostra cada checagem assim que fica pronta'}>
          {w === 'internet' ? 'Internet' : 'TV'}
        </button>
      ))}
    </div>
  )

  return (
    <PhotosContext.Provider value={photos}>
    <div className={`compact${tv ? ' compact--tv' : ''}`}>
      <header className="fhead compact__bar">
        {rooms && rooms.length > 1 ? (
          <FitSelect className="fhead__title" label={roomTitle(room?.name ?? '')} value={room?.id ?? ''}
            onChange={(e) => pickRoom(e.target.value)} aria-label="Transmissão">
            {rooms.map((r) => <option key={r.id} value={r.id}>{roomTitle(r.name)}</option>)}
          </FitSelect>
        ) : <span className="fhead__title">{roomTitle(room?.name ?? '')}</span>}
        <LiveChip label={status} live={live && session.state === 'listening'} />
        {room && !tv && <ViewSwap href={`/?room=${room.id}`} video />}
        <span className="fspacer" />
        {!tv && <span className="compact__desk">{watchingToggle}</span>}
      </header>

      {claims.length === 0 ? (
        <main className="fwait compact__empty">Aguardando checagens</main>
      ) : (
        <main className="compact__main">
          {featured && (
            <section className="compact__now" aria-label="Última fala checada">
              <FeedCard key={featured.id} claim={featured} when={when(featured)} featured fresh={fresh(featured)} onOpen={() => setOpenId(featured.id)} />
              <ScoreBars rows={rows} unnamed={unnamed} />
            </section>
          )}
          <section className="compact__feed" aria-label="Falas checadas">
            <div className="compact__phone-score"><ScoreBars rows={rows} unnamed={unnamed} /></div>
            <CountBar n={shown.length} busy={busy} filters={filters} onFilter={() => setFiltering(true)} />
            <ol className="flist" aria-live="polite">
              {shown.length === 0 && <li className="flist__none">Nada com esse filtro.</li>}
              {shown.map((c, i) => (
                <li key={c.id} className={`compact__item${i >= TV_SIDE + 1 ? ' compact__item--more' : ''}${c.id === featured?.id ? ' compact__item--featured' : ''}`}>
                  <FeedCard claim={c} when={when(c)} fresh={fresh(c)} onOpen={() => setOpenId(c.id)} />
                </li>
              ))}
            </ol>
          </section>
        </main>
      )}
      {opened && <ClaimSheet claim={opened} when={when(opened)} onClose={closeModal} />}
      {filtering && (
        <FilterSheet filters={filters} onChange={setFilters} onClose={closeFilters} counts={counts} speakers={speakers}
          extra={<>
            {watchingToggle && <><h3 className="fsheet__label">Assistindo em</h3><div className="fopts">{watchingToggle}</div></>}
          </>} />
      )}
    </div>
    </PhotosContext.Provider>
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
