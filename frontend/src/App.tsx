import { useMemo, useState, type FormEvent } from 'react'
import { ClaimCard } from './components/ClaimCard'
import { Overlay } from './components/Overlay'
import { Player } from './components/Player'
import { TranscriptRail } from './components/TranscriptRail'
import { useSession } from './useSession'
import type { Verdict } from './types'

const DEFAULT_URL = 'https://www.youtube.com/watch?v=Amwukq3hviM'

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
  const [language, setLanguage] = useState('pt')
  const [currentTime, setCurrentTime] = useState(0)
  const [tab, setTab] = useState<'claims' | 'transcript'>('claims')
  const session = useSession()

  const videoId = session.info?.video_id || extractVideoId(url)
  const running = session.state === 'connecting' || session.state === 'resolving' || session.state === 'listening'

  const counts = useMemo(() => {
    const c: Record<Verdict, number> = { pending: 0, fake: 0, valid: 0, inconclusive: 0 }
    for (const cl of session.claims) c[cl.verdict]++
    return c
  }, [session.claims])

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    if (running) session.stop()
    else session.start(url.trim(), language)
  }

  return (
    <div className="app">
      <header className="topbar">
        <h1>
          <span className="logo">●</span> Debate Fact-Check
        </h1>
        <form className="controls" onSubmit={onSubmit}>
          <input
            type="url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="YouTube live / video URL"
            disabled={running}
            required
          />
          <select value={language} onChange={(e) => setLanguage(e.target.value)} disabled={running}>
            <option value="pt">PT</option>
            <option value="en">EN</option>
            <option value="es">ES</option>
            <option value="">Auto</option>
          </select>
          <button type="submit" className={running ? 'btn btn--stop' : 'btn'}>
            {running ? 'Stop' : 'Start'}
          </button>
        </form>
        <div className={`status status--${session.state}`}>
          {session.state === 'listening' && <span className="dot" />}
          {session.statusText || 'Idle'}
          {session.info?.is_live && <span className="live-tag">LIVE</span>}
        </div>
      </header>

      {session.error && <div className="banner banner--error">{session.error}</div>}

      <main className="layout">
        <section className="stage">
          {videoId ? (
            <Player videoId={videoId} onTime={setCurrentTime} />
          ) : (
            <div className="player player--empty">Paste a YouTube URL to begin</div>
          )}
          <Overlay claims={session.claims} isLive={session.info?.is_live ?? true} currentTime={currentTime} />
          <div className="scoreboard">
            <Score label="Fake" n={counts.fake} kind="fake" />
            <Score label="Valid" n={counts.valid} kind="valid" />
            <Score label="Inconclusive" n={counts.inconclusive} kind="inconclusive" />
            <Score label="Checking" n={counts.pending} kind="pending" />
          </div>
        </section>

        <aside className="side">
          <nav className="tabs">
            <button className={tab === 'claims' ? 'active' : ''} onClick={() => setTab('claims')}>
              Claims ({session.claims.length})
            </button>
            <button className={tab === 'transcript' ? 'active' : ''} onClick={() => setTab('transcript')}>
              Transcript
            </button>
          </nav>
          {tab === 'claims' ? (
            <div className="claims-list">
              {session.claims.length === 0 && <p className="muted">Checked claims will appear here…</p>}
              {[...session.claims].reverse().map((c) => (
                <ClaimCard key={c.id} claim={c} />
              ))}
            </div>
          ) : (
            <TranscriptRail segments={session.transcript} />
          )}
        </aside>
      </main>
    </div>
  )
}

function Score({ label, n, kind }: { label: string; n: number; kind: Verdict }) {
  return (
    <div className={`score score--${kind}`}>
      <strong>{n}</strong> {label}
    </div>
  )
}
