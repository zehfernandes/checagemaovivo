import { useRef, useEffect, useMemo, useState, type FormEvent } from 'react'
import { ClaimCard } from './components/ClaimCard'
import { Danmu } from './components/Danmu'
import { Player } from './components/Player'
import { useSession } from './useSession'
import type { Room, Verdict } from './types'

const DEFAULT_URL = 'https://www.youtube.com/watch?v=Amwukq3hviM'
// A chunk's audio spans [captured_at - 8, captured_at] on the server clock. Measured on YouTube live:
// the embedded player's own live edge trails the server's HLS reader by ~12 s, so a viewer `delay` s
// behind the player edge starts hearing that chunk at about captured_at + delay + 8.
const PLAYER_EDGE_LAG_S = 8
const LIVE_DELAY_S = 20 // lives: player sits this far behind the edge so verdicts are ready when the viewer hears the sentence
const RECENT_S = 180
// Recordings: a claim is stamped where its sentence ends inside an 8 s chunk (interpolated, ±3-4 s),
// so reveal it a few seconds later rather than while the sentence is still being spoken.
const REVEAL_LAG_S = 4


function extractVideoId(url: string): string | null {
  try {
    const u = new URL(url)
    if (u.hostname === 'youtu.be') return u.pathname.slice(1) || null
    if (u.pathname.startsWith('/live/')) return u.pathname.split('/')[2] || null
    return u.searchParams.get('v')
  } catch {
    return null
  }
}

export default function App() {
  const [url, setUrl] = useState(DEFAULT_URL)
  const language = 'pt'
  const [currentTime, setCurrentTime] = useState(0)
  const [tab, setTab] = useState<'recent' | 'all'>('recent')
  const danmu = true
  const [sideOpen, setSideOpen] = useState(() => localStorage.getItem('side') !== 'closed')
  // recordings: show only what is said from the position where Start was pressed (no backlog dump)
  const [baseline, setBaseline] = useState(0)
  useEffect(() => localStorage.setItem('side', sideOpen ? 'open' : 'closed'), [sideOpen])
  const delay = LIVE_DELAY_S
  const nudge = 0
  const [now, setNow] = useState(() => Date.now())
  const session = useSession()

  // Rooms: broadcasts opened by the newsroom. When there are rooms, viewers pick one instead of pasting a URL.
  const [rooms, setRooms] = useState<Room[] | null>(null)
  const [roomId, setRoomId] = useState(() => new URLSearchParams(location.search).get('room') ?? '')
  useEffect(() => {
    let alive = true
    const load = () => fetch('/rooms').then((r) => r.json()).then((d: Room[]) => alive && setRooms(d)).catch(() => alive && setRooms([]))
    load()
    const t = setInterval(load, 30_000)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [])
  const room = rooms?.find((r) => r.id === roomId) ?? rooms?.[0]
  const roomUrl = room?.url
  useEffect(() => {
    if (!roomUrl) return
    // oxlint-disable-next-line react-hooks/set-state-in-effect -- the room decides the URL
    setUrl(roomUrl)
    setBaseline(0)
  }, [roomUrl])
  function pickRoom(id: string) {
    setRoomId(id)
    const p = new URLSearchParams(location.search)
    p.set('room', id)
    history.replaceState(null, '', `?${p}`)
  }

  const typedId = extractVideoId(url)
  const videoId = session.info?.video_id || (typedId && typedId.length === 11 ? typedId : null)
  const isLive = session.info?.is_live ?? true
  // broadcast delay: only once the server confirmed a live stream; kept after Stop so buffered
  // claims don't flush at once (the player stays behind the edge too)
  const delayed = session.info?.is_live === true && delay > 0
  useEffect(() => {
    if (!delayed) return
    const t = setInterval(() => setNow(Date.now()), 500)
    return () => clearInterval(t)
  }, [delayed])
  useEffect(() => {
    if (!isLive && currentTime < baseline - 2) setBaseline(Math.max(0, currentTime))
    // oxlint-disable-next-line react-hooks/exhaustive-deps
  }, [currentTime, isLive])
  const revealAt = (capturedAt?: number | null) =>
    capturedAt ? (capturedAt + delay + nudge + PLAYER_EDGE_LAG_S + session.clockOffset) * 1000 : 0


  // Processing is shared per video and cached server-side; for a recording we only reveal
  // what has been said up to the player position; for a delayed live, what the viewer heard.
  const visibleClaims = useMemo(() => {
    if (!isLive) return session.claims.filter((c) => c.start >= baseline - 1 && c.start + REVEAL_LAG_S <= currentTime)
    if (!delayed) return session.claims
    return session.claims
      .map((c) => ({ ...c, revealAt: revealAt(c.captured_at) }))
      .filter((c) => c.revealAt <= now)
    // oxlint-disable-next-line react-hooks/exhaustive-deps -- revealAt is derived from delay/nudge/clockOffset listed below
  }, [session.claims, currentTime, isLive, delayed, now, delay, nudge, session.clockOffset, baseline])

  // last 3 minutes of the video (stream time), newest first
  const recentClaims = useMemo(() => {
    // recordings: relative to the player position; live: relative to the newest claim
    const ref = isLive ? visibleClaims.reduce((m, c) => Math.max(m, c.start), 0) : currentTime
    return visibleClaims.filter((c) => c.start >= ref - RECENT_S)
  }, [visibleClaims, isLive, currentTime])

  const counts = useMemo(() => {
    const c: Record<Verdict, number> = { pending: 0, fake: 0, valid: 0, inconclusive: 0 }
    for (const cl of visibleClaims) c[cl.verdict]++
    return c
  }, [visibleClaims])

  const startedFor = useRef('')
  function begin() {
    const u = url.trim()
    const isNew = u !== startedFor.current
    const from = isNew ? 0 : currentTime // a new video starts from the top; retries resume
    startedFor.current = u
    if (isNew) setBaseline(0) // a reconnect must not hide what was already listed
    session.start(u, language, from, false, false, false, true)
  }
  function onSubmit(e: FormEvent) {
    e.preventDefault()
  }
  // no button: checking starts by itself when a video is chosen, follows the player and reconnects
  // if the connection drops (e.g. server restart). A room whose checking hasn't started is retried quietly.
  useEffect(() => {
    const u = url.trim()
    if (!u || (!roomUrl && !(typedId && typedId.length === 11))) return
    const isNew = u !== startedFor.current
    const lost = session.state === 'error' && session.lost
    const waiting = session.state === 'error' && !lost && !!roomUrl
    if (!isNew && !lost && !waiting) return
    const t = setTimeout(begin, isNew ? 0 : lost ? 2000 : 15000)
    return () => clearTimeout(t)
    // oxlint-disable-next-line react-hooks/exhaustive-deps
  }, [session.state, session.lost, url, roomUrl, typedId])

  const clip = useMemo<[number, number] | undefined>(() => {
    const i = session.info
    return i?.clip_start != null && i?.clip_end != null ? [i.clip_start, i.clip_end] : undefined
  }, [session.info])

  return (
    <div className="app">
      <header className="topbar">
        <form className="controls" onSubmit={onSubmit}>
          <span className="logo" aria-hidden>●</span>
          {rooms && rooms.length > 0 ? (
            <select className="room-picker" value={room?.id ?? ''} onChange={(e) => pickRoom(e.target.value)} title="Transmissão">
              {rooms.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
            </select>
          ) : (
            <input
              type="url"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="Cole a URL de um vídeo ou live do YouTube"
              required
            />
          )}
        </form>
        <span className={`indicator status--${session.state}`} title={session.statusText}>
          {(session.state === 'listening' || session.state === 'resolving') && <span className="dot" />}
          {session.state === 'listening' && <span>Escutando</span>}
          {session.state === 'resolving' && <span>Conectando</span>}
          {session.state === 'error' && <span>Erro</span>}
          {session.state === 'listening' && session.info?.is_live && <span className="live-tag">AO VIVO</span>}
        </span>
      </header>


      {session.error && <div className="banner banner--error">{session.error}</div>}

      <main className={`layout${sideOpen ? '' : ' layout--collapsed'}`}>
        <section className="stage">
          <button
            type="button"
            className="side-toggle"
            onClick={() => setSideOpen((o) => !o)}
            title={sideOpen ? 'Recolher a barra lateral' : 'Mostrar afirmações e transcrição'}
            aria-expanded={sideOpen}
          >
            {sideOpen ? '›' : '‹'}
            {!sideOpen && visibleClaims.length > 0 && <span className="side-toggle__count">{visibleClaims.length}</span>}
          </button>
          {videoId ? (
            <Player videoId={videoId} onTime={setCurrentTime} liveDelay={delayed ? delay : 0} clip={clip} />
          ) : (
            <div className="player player--empty">Cole uma URL do YouTube para começar</div>
          )}
          <Danmu claims={visibleClaims} isLive={isLive} currentTime={currentTime} enabled={danmu} />
          <div className="scoreboard">
            <Score label="falsas" n={counts.fake} kind="fake" />
            <Score label="verdadeiras" n={counts.valid} kind="valid" />
            <Score label="inconclusivas" n={counts.inconclusive} kind="inconclusive" />
            {counts.pending > 0 && <Score label="verificando" n={counts.pending} kind="pending" />}
          </div>
        </section>

        {sideOpen && (
        <aside className="side">
          <nav className="tabs">
            <button className={tab === 'recent' ? 'active' : ''} onClick={() => setTab('recent')} title="Afirmações dos últimos 3 minutos">
              Recentes {recentClaims.length > 0 && <span className="muted">{recentClaims.length}</span>}
            </button>
            <button className={tab === 'all' ? 'active' : ''} onClick={() => setTab('all')}>
              Todas {visibleClaims.length > 0 && <span className="muted">{visibleClaims.length}</span>}
            </button>
          </nav>
          <div className="claims-list">
              {(tab === 'recent' ? recentClaims : visibleClaims).length === 0 && (
                <p className="muted empty">{tab === 'recent' ? 'Nada nos últimos 3 minutos.' : 'As afirmações verificadas aparecem aqui.'}</p>
              )}
              {[...(tab === 'recent' ? recentClaims : visibleClaims)].reverse().map((c) => (
                <ClaimCard key={c.id} claim={c} />
              ))}
          </div>
        </aside>
        )}
      </main>
    </div>
  )
}

function Score({ label, n, kind }: { label: string; n: number; kind: Verdict }) {
  return (
    <span className={`score score--${kind}`}>
      <strong>{n}</strong> {label}
    </span>
  )
}
