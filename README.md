# debate-fake-news

Real-time fact-checking overlay for YouTube live streams (debates, broadcasts).

Pipeline: **YouTube audio → transcription → claim extraction → news-DB verification (web-search fallback) → overlay**.
Each claim shows up on top of the video within seconds as *Checking…* and then flips to
**Fake**, **Valid** or **Inconclusive** with a short justification and sources.

```
backend/   FastAPI + WebSocket. yt-dlp/ffmpeg pull the audio from the live edge in short
           chunks; each chunk is transcribed and checked in parallel so slow web searches
           never delay the next chunk.
frontend/  React + Vite. Embedded YouTube player, verdict overlay, scoreboard, claims list
           and live transcript.
```

## Requirements

- Python 3.10+, `ffmpeg` on PATH
- Node 20+ (used by yt-dlp as JS runtime for YouTube, and for the frontend)
- An OpenAI API key (`gpt-4o-mini-transcribe` for speech, `gpt-4.1-mini` + web search for verification)

## Run locally

```bash
cp .env.example .env            # set PROVIDER=openai and OPENAI_API_KEY=... (or keep PROVIDER=mock)

# backend
pip install -r backend/requirements.txt
cd backend && uvicorn app.main:app --reload --port 8000

# frontend (new terminal)
cd frontend && npm install && npm run dev
```

- Viewer: http://localhost:5173 — opens the first published room (the Band debate is created
  automatically on an empty DB, see `DEFAULT_ROOM_URL`). Visitors never start processing; they
  watch what the newsroom started and get the cached history instantly.
- Newsroom: http://localhost:5173/admin — rooms, live claim editor, news DB (see [Admin](#admin-admin)).
  With `ADMIN_PASSWORD` empty (the dev default) no login is needed and **everyone is admin**.
- Nothing happens until an admin presses **Iniciar** on a room in `/admin` → *Salas*.

`PROVIDER=mock` runs the whole UI without any API key (synthetic transcript and random
verdicts); the news DB is disabled in mock mode. With `PROVIDER=openai` the first start ingests
the Brazilian feeds into `backend/data/news.db` (~1–2 min, a few cents of embeddings) and refreshes
every `NEWS_REFRESH_MINUTES`. `backend/data/` is gitignored — every clone starts with empty DBs.

### Checklist for a new contributor

1. Access to this repo (it is private) and `git clone` + `git checkout main`.
2. Python 3.10+, `ffmpeg` on PATH, Node 20+.
3. An OpenAI API key of their own (or `PROVIDER=mock` for UI work).
4. `cp .env.example .env`, set `PROVIDER`/`OPENAI_API_KEY`; optionally `ADMIN_PASSWORD`.
5. Backend + frontend as above; `cd frontend && npm run lint && npx tsc -b` before pushing.
6. To test the production site no setup is needed: https://www.checagemaovivo.org (viewer) and
   https://www.checagemaovivo.org/admin (newsroom, needs the deployed `ADMIN_PASSWORD`).
7. If yt-dlp says *Sign in to confirm you're not a bot*, see `YTDLP_ARGS` below.
8. To start with production's data (cached Band debate checks, news DB, glossary) instead of empty
   DBs: `cd backend && python -m scripts.pull_data https://www.checagemaovivo.org <ADMIN_PASSWORD>`
   (backend stopped; uses `GET /admin/export/{cache.db|news.db|review.db|glossary.json}`).

## Admin (`/admin`)

**Access.** With `GOOGLE_CLIENT_ID` set, people sign in with Google and get in only if their verified e-mail
is listed: **owners** (`OWNER_EMAILS`) can do everything; **checkers**, added by an owner in *Usuários*, see
*Salas* (enter a room to review, correct and re-check its claims; review labels), *Base de notícias* and *Glossário* —
they can't create, configure, start or stop rooms, export data or manage users.
The role is checked on every request, so removing someone cuts them off at once. Endpoints: `GET /admin/auth`,
`POST /admin/login/google`, `GET /admin/me`, `GET/POST /admin/users`, `DELETE /admin/users/{email}`.
Google Cloud Console → *APIs & Services* → *Credentials* → *OAuth client ID* (Web application); authorized
JavaScript origins: `http://localhost:5173`, `https://www.checagemaovivo.org`, `https://debate-fact-check.fly.dev`.
Without `GOOGLE_CLIENT_ID` the shared `ADMIN_PASSWORD` still works (as an owner); with neither, everyone is an owner (dev).

The viewer UI has no admin controls; everything operational lives at `/admin` (password =
`ADMIN_PASSWORD`, sent as the `x-admin-password` header / `admin` field on the WebSocket).

- **Salas** — the rooms are listed in the sidebar (status dot + name); opening one shows its fact-checks (the review
  queue below) and, for owners, the room panel beside them with two tabs, *Dados* (the room's form, locked while
  checking runs) and *Logs* (usage and the run's log), plus Iniciar/Religar/Parar. Checkers only see the checks.
  Rooms are YouTube URLs (debate, live interview…) stored in the cache DB. Owners create, rename,
  hide/publish (`active`), delete; **Iniciar/Parar** the shared processing (optionally in save mode);
  viewer count, claim count and progress per room. Only published rooms appear in the viewer's room picker.
  The room panel also shows the backend's own log for the run (`GET /admin/rooms/{id}/activity`): transcription
  mode, work in flight, every transcript with its lag, extraction/gate/dedup drops, each verification with
  method and time, errors and realtime reconnects.
  Endpoints: `GET /rooms` (public, active only), `GET/POST /admin/rooms`, `PATCH/DELETE /admin/rooms/{id}`,
  `POST /admin/rooms/{id}/start|stop`.
- **Checks inside a room** — the newsroom review queue. With `HUMAN_REVIEW=true` (default) every machine analysis is
  a **draft** that the public site never sees; a person opens it, adjusts it if needed and **Publica** (now
  on the site) or marks it **Irrelevante** (never on the site), and the next card opens by itself.
  **Revisar** opens the oldest draft the machine has finished. Filters: Aguardando / Publicadas /
  Irrelevantes / Todas, verdict, search. A published claim can be corrected or taken off the site later.
  Several people review at once: opening a card *leases* it for 120 s (renewed every 45 s while open), so
  nobody else can open it and **Revisar** skips it; the list shows who has it ("✎ Ana"). A decision carries
  the version (`rev`) the reviewer loaded, so it is refused (409) if someone changed the claim meanwhile.
  Late machine updates (a deep check finishing) never overwrite a claim a person decided. Reviewer names
  (asked once per browser) are newsroom-only: the public feed carries no review fields. Leases live in the
  backend process (one machine on Fly), not in the DB. Claims cached before the queue existed count as published.
  Endpoints: `GET /admin/claims?url=`, `POST /admin/review/next`, `POST /admin/review/{claim_id}/lease|release|decide`,
  `POST /admin/claim` (direct correction, publishes), `DELETE /admin/claims/{video_id}/{claim_id}`;
  the admin WebSocket sends `"review": true` on `start` to also receive drafts.
- **Base de notícias** — browse/search the evidence DB, add an article by URL (extracted + embedded) or by
  pasted text, delete documents, refresh the feeds. Endpoints: `GET/POST /admin/news/docs`,
  `DELETE /admin/news/docs/{id}`, `POST /news/refresh`.

## Deploy (Fly.io)

One Docker image (`Dockerfile`): the frontend is built and served by FastAPI, SQLite DBs live on the
`data` volume at `/data`. Config in `fly.toml` (app `debate-fact-check`, region `gru`, machines never
auto-stop so WebSockets and the shared pipeline stay alive).

```bash
fly launch --copy-config --no-deploy        # first time only
fly volumes create data --size 3 -r gru     # first time only
fly secrets set OPENAI_API_KEY=... ADMIN_PASSWORD=...
fly deploy --remote-only                    # every release (builds on Fly, no local Docker needed)
```

Continuous deploy: `.github/workflows/fly-deploy.yml` runs `flyctl deploy --remote-only` on every push to
`main` (or manually via *Run workflow*). It only needs the repo secret `FLY_API_TOKEN`
(`fly tokens create deploy -a debate-fact-check -x 8760h`), so contributors deploy by merging —
no Fly account or production secrets required. Production secrets (`OPENAI_API_KEY`,
`ADMIN_PASSWORD`, `AI_GATEWAY_API_KEY`) live only in Fly (`fly secrets set`).

## Configuration (`.env`)

| Variable | Default | Description |
| --- | --- | --- |
| `PROVIDER` | `mock` | `openai` or `mock` |
| `OPENAI_API_KEY` | | required for `openai` |
| `OPENAI_BASE_URL` | | optional OpenAI-compatible base URL (e.g. Vercel AI Gateway) |
| `NEWS_DB_PATH` | `data/news.db` | SQLite file for the news/evidence DB (relative to `backend/`) |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | embedding model for the news DB |
| `CACHE_DB_PATH` | `data/cache.db` | per-video transcript/claim cache shared by all viewers |
| `VOD_FULL_SPEED` | `false` | process recordings at full speed once instead of pacing with playback |
| `NEWS_MIN_SIMILARITY` | `0.55` | min cosine similarity for a DB hit to count as evidence |
| `NEWS_MAX_AGE_DAYS` | `14` | ignore feed items older than this |
| `NEWS_REFRESH_MINUTES` | `30` | background feed refresh interval (0 = off) |
| `NEWS_FEEDS` | built-in list | override feeds: `Name=url,Name2=url2` |
| `TRANSCRIBE_MODEL` | `gpt-4o-mini-transcribe` | speech-to-text model |
| `FACTCHECK_MODEL` | `gpt-4.1-mini` | model for claim extraction and verification |
| `DEEP_MODEL` | `gpt-5-mini` | reasoning model for the deep check (third level / Rechecar in `/admin`); `reasoning.effort=low`, no `temperature` |
| `HUMAN_REVIEW` | `true` | machine analyses wait as drafts until a person publishes them in `/admin` → *Salas* → the room; `false` publishes them at once (the old behaviour) |
| `CANDIDATES_ONLY` | `true` | only fact-check candidates/interviewees: claims the extractor attributes to moderators, presenters or journalists (e.g. a question's premise) are dropped; set `false` for news broadcasts |
| `DEEP_CHECK` | `true` | automatically run the deep check on claims the fast pass left inconclusive |
| `DECIDER` | `openai` | `jev`: TypeSafe Jev decision model via Vercel AI Gateway decides from DB evidence in ~0.3 s and gates non-checkable claims; falls back to OpenAI on low confidence/error |
| `AI_GATEWAY_API_KEY` | – | Vercel AI Gateway key (required for `DECIDER=jev`; the team needs paid credits) |
| `JEV_MODEL` | `typesafe-ai/jev` | decision model id on the gateway |
| `CLAIM_GATE_MIN` | `0.35` | with Jev: drop candidate claims whose "checkable" probability is below this |
| `TRANSCRIBE_GLOSSARY` | debate names | names/terms passed to the speech model so they are spelled correctly (Vorcaro, Galípolo…) |
| `CHUNK_SECONDS` | `8` | audio chunk size; smaller = lower latency, more API calls |
| `TRANSCRIBE_MODE` | `chunks` | `realtime`: stream audio to an OpenAI Realtime transcription session (same `TRANSCRIBE_MODEL`); segments end at pauses instead of every `CHUNK_SECONDS`, text ~1 s after the speaker stops. Paced runs only (`VOD_FULL_SPEED` keeps chunks); talks to api.openai.com over WebSocket |
| `REALTIME_SILENCE_MS` | `500` | pause that ends a realtime segment; lower = faster, more mid-sentence cuts |
| `REALTIME_MAX_SEGMENT_S` | `8` | cut a realtime segment anyway after this long (TV audio with a music bed or a speaker who never pauses keeps VAD "speaking"); higher = fewer mid-sentence cuts, more latency |
| `LANGUAGE` | `pt` | transcription language hint (ISO-639-1), empty for auto |
| `CORS_ORIGINS` | `http://localhost:5173` | allowed frontend origins |
| `CANONICAL_HOST` | | if set, HTTP requests for other hosts 301 to it (prod: `www.checagemaovivo.org`) |
| `GOOGLE_CLIENT_ID` | | Google sign-in for `/admin` (OAuth client id, type *Web application*). When set, `ADMIN_PASSWORD` stops working and only listed e-mails get in |
| `OWNER_EMAILS` | | comma-separated Google e-mails with full access (rooms, exports, users, plus everything checkers do); fixed here, cannot be removed in the UI |
| `SESSION_SECRET` | | signs admin sessions (7 days); set it in production or everyone is signed out on each restart |
| `ADMIN_PASSWORD` | | password for `/admin` and admin WebSocket actions; **empty = everyone is admin** (dev only) |
| `DEFAULT_ROOM_URL` / `DEFAULT_ROOM_NAME` | Band debate | room created on an empty DB; empty URL = none |
| `VIDEO_CLIPS` | | only process a slice of recordings: `VIDEO_ID=start-end,...` (seconds) |
| `YTDLP_ARGS` | | extra yt-dlp args, e.g. `--cookies-from-browser chrome:/path` when YouTube asks to confirm you're not a bot |

## News database (first level) + web search (fallback) + deep check (third level)

Verification ladder (fastest first):

0. **Verdict memory** – every settled verdict (DB, web or deep; not inconclusive) is stored with the
   claim's embedding (`verdicts` table in the news DB). A new claim ≥ `VERDICT_CACHE_SIMILARITY`
   (0.9) similar to one settled in the last `VERDICT_CACHE_DAYS` (7) reuses it instantly — no AI
   calls, **Cache** badge. Debates repeat talking points, and the same claim across videos is free.
   The claim embedding is computed once (in the duplicate filter) and reused here and in step 1.
1. **News DB** – `backend/app/news.py` ingests RSS from Brazilian fact-checkers and outlets
   (Lupa, g1 Fato ou Fake, g1 Política, Agência Brasil, Folha Poder, CNN Brasil, Poder360,
   E-Farsas, Boatos.org), extracts article text, embeds it (`text-embedding-3-small`) and stores
   it in SQLite. At verify time the claim is embedded and matched by cosine similarity (in memory,
   ~ms); if the best matches are above `NEWS_MIN_SIMILARITY` the model decides from that evidence
   only (no tools, ~1–2 s). Cards answered this way show a **DB** badge.
   Guard against false "Fake"s: with `DECIDER=jev` the decider also answers whether the evidence is
   about the *same subject* as the claim (`relevant`); a **fake** verdict needs `relevant ≥ 0.8` and
   `sufficient ≥ 0.6` (valid: `relevant ≥ 0.6`), otherwise the claim skips the DB and goes straight to
   web search — articles that merely share the topic (e.g. generic elevator rules vs. one man's
   home-made elevator) are not a contradiction. The claim gate also requires public interest
   (public figures, institutions, statistics, laws, public events), so a reportage subject's personal
   story is not fact-checked.
   When Jev is unsure the claim goes straight to web search (no second LLM pass over the same evidence).
2. **Web search** – when the DB has nothing relevant, or the model says the evidence is not
   sufficient, the claim goes to OpenAI web search (~4–6 s, **Web** badge). Pages cited by the
   search are written back into the DB so the next similar claim is a DB hit.

The DB refreshes on backend start and every `NEWS_REFRESH_MINUTES`; `/admin` → *Base de notícias* shows the
article count and an **Atualizar feeds** button (`POST /news/refresh`). CLI:

```bash
cd backend
python -m app.news refresh 14          # ingest the last 14 days
python -m app.news search "maioridade penal PT"
python -m app.news stats
```

### Deep check (third level)

A claim that is still `inconclusive` after the DB and web passes is automatically re-run through a
slower, stronger pass (`DEEP_MODEL`, several web searches targeting fact-checkers and official sources,
two independent sources required for fake/valid). While it runs the card shows "Deep check…"; the result
replaces the verdict with a `Deep` badge. Every settled card in `/admin` → *Salas* → the room also has a **Rechecar** button that triggers
the same pass manually (`{"type":"recheck","claim_id":"…"}` over the WebSocket).

## Shared processing + cache (many viewers, one pipeline)

Processing is **per video, not per viewer** (`backend/app/channels.py`): the first viewer of a URL starts
one pipeline (one audio pull, one set of AI calls); every other browser just subscribes to that video's
channel over `/ws` and receives the same transcript/claim events. Everything is persisted in
`backend/data/cache.db`, so a late joiner (or a later day) gets the history instantly and a video is never
processed twice; a recording resumes from where processing stopped. Live pipelines stop 5 min after the
last viewer leaves; paced VOD processing stops after 60 s and resumes on the next viewer. Set
`VOD_FULL_SPEED=true` to run a recording through at full speed once instead (one-off cost, instant seeks).
`GET /channels` shows active channels, viewer counts and progress. The UI reveals only what has been said
up to the player position for recordings, so seeking is instant and spoiler-free.

### Fresh run (no cache) + latency readout

Admins can send `"fresh": true` on `start` (the public viewer has no such control) to bypass the shared channel: a private
pipeline processes the video again from scratch, reads nothing from and writes nothing to the cache, and
dies a few seconds after you stop. Use it to measure true end-to-end latency instead of being served from
cache. Every transcript/claim event carries server wall-clock timestamps (`captured_at` = audio arrived from
YouTube, `transcribed_at`, `extracted_at` = claim shown as pending, `verified_at` = verdict); the header shows
the average "⏱ claim +Xs · verdict +Ys" for live/fresh runs and each card shows its own numbers. Measured on
a live Record News stream (8 s chunks): transcript +0.7 s, claim +2.3–2.6 s, web verdict +5.4–6.6 s, deep
check +9.7 s after the audio arrived. Note YouTube's own live delay (HLS edge vs. what the player shows) is
on top of that and is not measured here.

## Offline review loop (learn from human checks)

Process a whole recording offline, label every claim, and measure the live pipeline against the labels:

```bash
cd backend
# 1) transcript: whisper verbose_json dumps (one per 10-min part) or a jsonl of {start,end,text}
python -m app.review batch band-2turno /path/to/whisper-json --from 9000 --to 10800 --title "Debate 150–180 min"
# 2) open the UI: http://localhost:5173/#review  -> Correct / Wrong verdict (+correct verdict, source URL) / Not a claim / Unsure
#    Transcript tab: "+ missed claim" on windows where nothing was extracted
# 3) golden set + metrics
python -m app.review export band-2turno > eval/golden.jsonl
python -m app.review eval band-2turno --rerun     # re-verifies labelled claims with the current prompts/models
```

Endpoints: `GET /review/runs`, `GET /review/items?run=`, `GET /review/segments?run=`, `POST /review/label`,
`POST /review/missed`, `DELETE /review/missed/{id}`, `GET /review/export?run=`, `GET /review/eval?run=&rerun=`.
A source URL given with a *Wrong verdict* label is ingested into the news DB (source `human-review`), so the
next similar claim is answered from it. Data lives in `backend/data/review.db` (gitignored).

## WebSocket protocol (`/ws`)

Client → server: `{"type":"start","url":"...","language":"pt","start_seconds":0,"fresh":false,"save":false,"watch":false,"admin":"<ADMIN_PASSWORD>"}`
(`watch: true` subscribes without starting processing — what the viewer sends; non-admins are always watch-only) · `{"type":"stop"}` · `{"type":"recheck","claim_id":"..."}`

Server → client:

- `{"type":"status","state":"resolving|listening|ended|error|idle","message":"..."}`
- `{"type":"info","info":{"video_id","title","is_live","duration"}}`
- `{"type":"transcript","segment":{"id","text","start","end","captured_at","transcribed_at"}}`
- `{"type":"claim","claim":{"id","text","verdict":"pending|fake|valid|inconclusive","confidence","explanation","sources":[{"title","url"}],"start","end","method","deep_pending","captured_at","extracted_at","verified_at"}}`
  — sent once with `pending` and again when the verdict is ready
- `{"type":"claim_removed","id":"..."}` — an admin deleted the claim
- `{"type":"error","message":"..."}`
- `{"type":"progress","processed_to","complete","viewers","fresh"}`

## Broadcast delay (live streams)

The server always processes the real live edge, but a verdict takes ~3–11 s, so without a delay a
claim can show up before the viewer hears the sentence. The **Delay** selector (default 20 s) seeks
the embedded player that many seconds back into YouTube's DVR window and the UI reveals each claim
(and transcript line) at `captured_at + delay + 10 s` (the embedded player's live edge trails the
server's HLS reader by ~12 s), i.e. when the viewer actually hears it — by then most
verdicts are already settled, so cards and danmu pills appear with their verdict instead of
"Checking…". Use the **−2s / +2s** sync buttons if claims come early/late for your player. Needs the
stream's DVR enabled (YouTube default); if the player cannot seek back the header warns about it.

If yt-dlp fails with *Sign in to confirm you're not a bot* (YouTube rate-limits datacenter IPs), set
`YTDLP_ARGS=--cookies-from-browser chrome:/path/to/profile` (or `--cookies cookies.txt`) in `.env`.

## Save mode + cost meter

**Save mode** (*modo econômico* when starting a room in `/admin`) keeps the full experience but cuts the expensive calls: no
automatic deep check (the Re-check button still runs one), stricter Jev claim gate
(`CLAIM_GATE_MIN_SAVE`, 0.5), at most `SAVE_MAX_CLAIMS_PER_MIN` (6) claims, two segments of
extraction context instead of three, and chunks under 12 words are not sent to the extractor.
It is decided by whoever starts a video's shared run and shown to all its viewers ("save mode").

Every run meters its own OpenAI usage (tokens per model, `web_search` calls, audio minutes) and the
header shows a rough **≈ US$** estimate at list prices (hover for the breakdown); `/channels`
exposes the same numbers per active video.
