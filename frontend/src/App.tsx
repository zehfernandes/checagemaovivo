import { useCallback, useRef, useEffect, useMemo, useState, type FormEvent } from 'react'
import { Danmu } from './components/Danmu'
import { ClaimSheet, CountBar, FeedCard, FilterSheet, LiveChip, ScoreBars, ViewSwap } from './components/Feed'
import { NO_FILTER, PhotosContext, UNNAMED, applyFilters, applyRoster, photoMap, roomTitle, scoreRows, totalOf, useSharedClaim, type Filters } from './components/feedData'
import { Player } from './components/Player'
import { useSession } from './useSession'
import type { Claim, Room, Verdict } from './types'

const DEFAULT_URL = 'https://www.youtube.com/watch?v=Amwukq3hviM'
// A chunk's audio spans [captured_at - 8, captured_at] on the server clock. Measured on YouTube live:
// the embedded player's own live edge trails the server's HLS reader by ~12 s, so a viewer `delay` s
// behind the player edge starts hearing that chunk at about captured_at + delay + 8.
const PLAYER_EDGE_LAG_S = 8
const LIVE_DELAY_S = 20 // lives: player sits this far behind the edge so verdicts are ready when the viewer hears the sentence
// Recordings: a claim is stamped where its sentence ends inside an 8 s chunk (interpolated, ±3-4 s),
// so reveal it a few seconds later rather than while the sentence is still being spoken.
const REVEAL_LAG_S = 4


function extractVideoId(url: string): string | null {
  try {
    const u = new URL(url)
    if (!/(^|\.)(youtube\.com|youtu\.be)$/.test(u.hostname)) return null
    if (u.hostname === 'youtu.be') return u.pathname.slice(1) || null
    if (u.pathname.startsWith('/live/')) return u.pathname.split('/')[2] || null
    return u.searchParams.get('v')
  } catch {
    return null
  }
}

function isHttpUrl(url: string): boolean {
  try {
    const u = new URL(url)
    return (u.protocol === 'https:' || u.protocol === 'http:') && u.hostname.includes('.')
  } catch {
    return false
  }
}

// a phone gets the single feed (score, "N checagens" + filters) instead of the desktop's tabs
const NARROW = '(max-width: 900px)'
function useNarrow() {
  const [narrow, setNarrow] = useState(() => window.matchMedia(NARROW).matches)
  useEffect(() => {
    const mq = window.matchMedia(NARROW)
    const on = () => setNarrow(mq.matches)
    mq.addEventListener('change', on)
    return () => mq.removeEventListener('change', on)
  }, [])
  return narrow
}

function hms(s: number) {
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = Math.floor(s % 60).toString().padStart(2, '0')
  return h ? `${h}:${m.toString().padStart(2, '0')}:${sec}` : `${m}:${sec}`
}

const SOURCE_LABEL: Record<string, string> = { youtube: 'YouTube', g1: 'g1', globoplay: 'Globoplay', ge: 'ge' }
function sourceLabel(source: string | undefined, url: string): string {
  if (source && SOURCE_LABEL[source]) return SOURCE_LABEL[source]
  if (source && source !== 'other') return source
  try {
    const host = new URL(url).hostname.replace(/^www\./, '')
    if (/(^|\.)(youtube\.com|youtu\.be)$/.test(host)) return 'YouTube'
    if (host.endsWith('.globo.com')) return SOURCE_LABEL[host.split('.')[0]] ?? host.split('.')[0]
    return host
  } catch {
    return 'site original'
  }
}

export default function App() {
  const [url, setUrl] = useState(DEFAULT_URL)
  const language = 'pt'
  const [currentTime, setCurrentTime] = useState(0)
  const danmu = true
  // recordings: show only what is said from the position where Start was pressed (no backlog dump)
  const [baseline, setBaseline] = useState(0)
  const delay = LIVE_DELAY_S
  const nudge = 0
  const [now, setNow] = useState(() => Date.now())
  const session = useSession()
  const narrow = useNarrow()
  const [filters, setFilters] = useState<Filters>(NO_FILTER)
  const [filtering, setFiltering] = useState(false)
  const closeFilters = useCallback(() => setFiltering(false), [])
  const [openId, setOpenId] = useState<string | null>(null)
  const closeClaim = useCallback(() => setOpenId(null), [])

  // Rooms: broadcasts opened by the newsroom. When there are rooms, viewers pick one instead of pasting a URL.
  const [rooms, setRooms] = useState<Room[] | null>(null)
  const [roomId] = useState(() => new URLSearchParams(location.search).get('room') ?? '')
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
  // no ?room=: the room being checked live, else the first one
  const room = rooms?.find((r) => r.id === roomId) ?? rooms?.find((r) => r.is_live && r.processing) ?? rooms?.[0]
  const roomUrl = room?.url
  // the room's registered speakers decide whose claims are shown (and the photos)
  const roster = room?.speakers
  const rosterClaims = useMemo(() => applyRoster(session.claims, roster), [session.claims, roster])
  const photos = useMemo(() => photoMap(roster), [roster])
  useEffect(() => {
    if (!roomUrl) return
    // oxlint-disable-next-line react-hooks/set-state-in-effect -- the room decides the URL
    setUrl(roomUrl)
    setBaseline(0)
  }, [roomUrl])

  const typedId = extractVideoId(url)
  // YouTube gets the synced player, g1/Globo a plain iframe below; anything else yt-dlp reads is checked from
  // the audio alone: the viewer gets the claims and a link to watch on the original site.
  const external = !!session.info && (session.info.source ?? 'youtube') !== 'youtube'
  const videoId = external ? null : session.info?.video_id || (typedId && typedId.length === 11 ? typedId : null)
  // the owner can disable embedding (101/150): keep checking, drop the player
  const [playerError, setPlayerError] = useState<number | null>(null)
  // oxlint-disable-next-line react-hooks/set-state-in-effect -- a new video gets a fresh player
  useEffect(() => setPlayerError(null), [videoId])
  const noPlayer = external || playerError !== null
  const failed = session.state === 'error' && !session.lost && session.statusCode === 'resolve_failed'
  const waiting = session.state === 'error' && !session.lost && session.statusCode === 'not_started'
  const pageUrl = session.info?.page_url || url
  const label = sourceLabel(session.info?.source, pageUrl)
  // g1/Globo: yt-dlp's id is the Globo video id, which Globo's iframe player plays (no time API, so no sync)
  const globoId = external && /^\d{6,}$/.test(session.info?.video_id ?? '') && /^https?:\/\/([^/]+\.)?globo\.com\//.test(pageUrl)
    ? session.info!.video_id : null
  const isLive = session.info?.is_live ?? true
  // The player reports its position twice a second; on a live only reconnects need it, so it stays
  // in a ref instead of re-rendering the whole page every tick.
  const timeRef = useRef(0)
  const liveRef = useRef(isLive)
  useEffect(() => {
    liveRef.current = isLive
  }, [isLive])
  const onTime = useCallback((t: number) => {
    timeRef.current = t
    if (!liveRef.current) setCurrentTime(t)
  }, [])
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
  // Reveal times are stamped once per claim list (not per tick) so card objects keep their identity
  // and the memoized cards don't re-render twice a second.
  const scheduledClaims = useMemo(
    () => (delayed ? rosterClaims.map((c) => ({ ...c, revealAt: revealAt(c.captured_at) })) : rosterClaims),
    // oxlint-disable-next-line react-hooks/exhaustive-deps -- revealAt is derived from delay/nudge/clockOffset listed below
    [rosterClaims, delayed, delay, nudge, session.clockOffset],
  )
  const visibleClaims = useMemo(() => {
    if (!isLive && noPlayer) return rosterClaims // nothing to sync to: list everything
    if (!isLive) return rosterClaims.filter((c) => c.start >= baseline - 1 && c.start + REVEAL_LAG_S <= currentTime)
    if (!delayed) return scheduledClaims
    return scheduledClaims.filter((c) => (c.revealAt ?? 0) <= now)
  }, [rosterClaims, scheduledClaims, currentTime, isLive, noPlayer, delayed, now, baseline])

  const counts = useMemo(() => {
    const c: Record<Verdict, number> = { pending: 0, fake: 0, valid: 0, inconclusive: 0 }
    for (const cl of visibleClaims) c[cl.verdict]++
    return c
  }, [visibleClaims])
  const rows = useMemo(() => scoreRows(visibleClaims), [visibleClaims])
  const filterSpeakers = useMemo(() => rows.map(([name, row]) => [name, totalOf(row) + row.pending] as [string, number]), [rows])
  // every claim so far, newest first, through the filters (same feed as /sem-video)
  const feedClaims = useMemo(() => applyFilters(visibleClaims, filters).slice().reverse(), [visibleClaims, filters])
  const opened = openId ? rosterClaims.find((c) => c.id === openId) ?? null : null
  useSharedClaim(rosterClaims, setOpenId)
  const when = (c: Claim) => (isLive ? undefined : hms(c.start))

  const startedFor = useRef('')
  function begin() {
    const u = url.trim()
    const isNew = u !== startedFor.current
    const from = isNew ? 0 : timeRef.current // a new video starts from the top; retries resume
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
    const youtube = !!typedId && typedId.length === 11
    if (!u || (!roomUrl && !youtube && !isHttpUrl(u))) return
    // wait for the room list (and the room's URL): starting the default video first leaks its claims into the room
    if (rooms === null || (roomUrl && u !== roomUrl)) return
    const isNew = u !== startedFor.current
    const lost = session.state === 'error' && session.lost
    const waiting = session.state === 'error' && !lost && !!roomUrl
    if (!isNew && !lost && !waiting) return
    // a pasted non-YouTube link is only known to be complete once typing pauses
    const t = setTimeout(begin, isNew ? (roomUrl || youtube ? 0 : 800) : lost ? 2000 : 15000)
    return () => clearTimeout(t)
    // oxlint-disable-next-line react-hooks/exhaustive-deps
  }, [session.state, session.lost, url, roomUrl, typedId, rooms])

  const clip = useMemo<[number, number] | undefined>(() => {
    const i = session.info
    return i?.clip_start != null && i?.clip_end != null ? [i.clip_start, i.clip_end] : undefined
  }, [session.info])

  return (
    <PhotosContext.Provider value={photos}>
    <div className="app">
      <header className="fhead topbar">
        <form className="controls" onSubmit={onSubmit}>
          {rooms && rooms.length > 0 ? (<>
            <span className="fhead__title">{roomTitle(room?.name ?? '')}</span>
          </>) : (
            <input
              type="url"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="Cole o link de um vídeo ou live (YouTube, g1…)"
              required
            />
          )}
        </form>
        <span className="fhead__status" title={session.statusText}>
          <LiveChip live={session.state === 'listening' && !!session.info?.is_live}
            label={session.state === 'error' ? (waiting ? 'Aguardando' : 'Erro') : session.state === 'resolving' || session.state === 'connecting' || !session.info ? 'Conectando'
              : session.info.is_live ? (session.state === 'listening' ? 'Ao vivo' : 'Ao vivo · pausado') : 'Gravado'} />
        </span>
        {rooms && rooms.length > 0 && room && <ViewSwap href={`/sem-video?room=${room.id}`} video={false} />}
      </header>


      {session.error && <div className="banner banner--error">{session.error}</div>}

      <main className="layout">
        <section className="stage">
          <div className="stage__video">
          {failed ? (
            <StageNotice tone="error" kicker="Link indisponível" title="Não foi possível abrir este link"
              text={session.statusText.replace(/^Não foi possível abrir o vídeo: /, '')} href={pageUrl} cta={`Abrir no ${label}`} />
          ) : waiting ? (
            <StageNotice kicker="Aguardando" title="A checagem ainda não começou"
              text="Esta página atualiza sozinha quando a redação iniciar a checagem." href={pageUrl} cta={`Assistir no ${label}`} />
          ) : videoId && playerError === null ? (
            <Player videoId={videoId} onTime={onTime} liveDelay={delayed ? delay : 0} clip={clip} onError={setPlayerError} />
          ) : globoId ? (
            <div className="player">
              <iframe src={`https://s.videos.globo.com/p3/amp/index.html?videosids=${globoId}`} title={session.info?.title || label}
                allow="autoplay; fullscreen; encrypted-media; picture-in-picture" allowFullScreen />
            </div>
          ) : external ? (
            <StageNotice tone={isLive ? 'live' : undefined} kicker={isLive ? `Ao vivo no ${label}` : `Transmissão no ${label}`}
              title={session.info?.title || room?.name || 'Checagem ao vivo'}
              text={`O vídeo fica no ${label}; aqui o áudio é checado direto da fonte e as afirmações aparecem conforme são ditas.`}
              href={pageUrl} cta={`Assistir no ${label}`} />
          ) : playerError !== null ? (
            <StageNotice kicker="Sem player" title="O YouTube não permite exibir este vídeo aqui"
              text="A checagem continua normalmente: as afirmações aparecem ao lado enquanto você assiste no YouTube."
              href={videoId ? `https://www.youtube.com/watch?v=${videoId}` : pageUrl} cta="Assistir no YouTube" />
          ) : (
            <div className="player player--empty">Cole o link de um vídeo ou live para começar</div>
          )}
          <Danmu claims={visibleClaims} isLive={isLive || noPlayer} currentTime={currentTime} enabled={danmu && !narrow && !(noPlayer && !isLive)} />
          </div>
          <ScoreBars rows={rows} unnamed={rows.find(([name]) => name === UNNAMED)?.[1]} reserve={roster?.length ?? 0} />
        </section>

        <aside className="side">
          {visibleClaims.length === 0 ? <p className="fwait">Aguardando checagens</p> : <>
          <CountBar n={feedClaims.length} busy={counts.pending > 0 || (isLive && session.state === 'listening')} filters={filters} onFilter={() => setFiltering(true)} />
          <ol className="flist claims-list">
            {feedClaims.length === 0 && <li className="flist__none">Nada com esse filtro.</li>}
            {feedClaims.map((c) => (
              <li key={c.id}><FeedCard claim={c} when={when(c)} onOpen={() => setOpenId(c.id)} /></li>
            ))}
          </ol>
          </>}
        </aside>
      </main>
      {opened && <ClaimSheet claim={opened} when={when(opened)} onClose={closeClaim} />}
      {filtering && (
        <FilterSheet filters={filters} onChange={setFilters} onClose={closeFilters} counts={counts} speakers={filterSpeakers} />
      )}
    </div>
    </PhotosContext.Provider>
  )
}

function StageNotice({ kicker, title, text, href, cta, tone }: {
  kicker: string; title: string; text?: string; href?: string; cta?: string; tone?: 'live' | 'error'
}) {
  return (
    <div className={`player stage-notice${tone ? ` stage-notice--${tone}` : ''}`}>
      <div className="stage-notice__box">
        <span className="stage-notice__kicker">{kicker}</span>
        <h2>{title}</h2>
        {text && <p>{text}</p>}
        {href && cta && (
          <a className="stage-notice__link" href={href} target="_blank" rel="noreferrer">{cta} ↗</a>
        )}
      </div>
    </div>
  )
}
