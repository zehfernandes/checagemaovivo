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

## Run

```bash
cp .env.example .env            # set PROVIDER=openai and OPENAI_API_KEY=...

# backend
pip install -r backend/requirements.txt
cd backend && uvicorn app.main:app --reload --port 8000

# frontend (new terminal)
cd frontend && npm install && npm run dev
```

Open http://localhost:5173, paste a YouTube live/video URL and press **Start**.

`PROVIDER=mock` runs the whole UI without any API key (synthetic transcript and random
verdicts); leave the URL empty in mock mode to skip YouTube entirely.

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
| `DEEP_MODEL` | `gpt-4.1` | stronger model for the deep check (third level / Re-check button) |
| `DEEP_CHECK` | `true` | automatically run the deep check on claims the fast pass left inconclusive |
| `DECIDER` | `openai` | `jev`: TypeSafe Jev decision model via Vercel AI Gateway decides from DB evidence in ~0.3 s and gates non-checkable claims; falls back to OpenAI on low confidence/error |
| `AI_GATEWAY_API_KEY` | – | Vercel AI Gateway key (required for `DECIDER=jev`; the team needs paid credits) |
| `JEV_MODEL` | `typesafe-ai/jev` | decision model id on the gateway |
| `CLAIM_GATE_MIN` | `0.35` | with Jev: drop candidate claims whose "checkable" probability is below this |
| `TRANSCRIBE_GLOSSARY` | debate names | names/terms passed to the speech model so they are spelled correctly (Vorcaro, Galípolo…) |
| `CHUNK_SECONDS` | `8` | audio chunk size; smaller = lower latency, more API calls |
| `LANGUAGE` | `pt` | transcription language hint (ISO-639-1), empty for auto |
| `CORS_ORIGINS` | `http://localhost:5173` | allowed frontend origins |

## News database (first level) + web search (fallback) + deep check (third level)

Verification is two-level:

1. **News DB** – `backend/app/news.py` ingests RSS from Brazilian fact-checkers and outlets
   (Lupa, g1 Fato ou Fake, g1 Política, Agência Brasil, Folha Poder, CNN Brasil, Poder360,
   E-Farsas, Boatos.org), extracts article text, embeds it (`text-embedding-3-small`) and stores
   it in SQLite. At verify time the claim is embedded and matched by cosine similarity (in memory,
   ~ms); if the best matches are above `NEWS_MIN_SIMILARITY` the model decides from that evidence
   only (no tools, ~1–2 s). Cards answered this way show a **DB** badge.
2. **Web search** – when the DB has nothing relevant, or the model says the evidence is not
   sufficient, the claim goes to OpenAI web search (~4–6 s, **Web** badge). Pages cited by the
   search are written back into the DB so the next similar claim is a DB hit.

The DB refreshes on backend start and every `NEWS_REFRESH_MINUTES`; the UI header shows the
article count and an **Update** button (`POST /news/refresh`). CLI:

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
replaces the verdict with a `Deep` badge. Every settled card also has a **Re-check** button that triggers
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

Client → server: `{"type":"start","url":"...","language":"pt","start_seconds":0}` · `{"type":"stop"}`

Server → client:

- `{"type":"status","state":"resolving|listening|ended|error|idle","message":"..."}`
- `{"type":"info","info":{"video_id","title","is_live","duration"}}`
- `{"type":"transcript","segment":{"id","text","start","end"}}`
- `{"type":"claim","claim":{"id","text","verdict":"pending|fake|valid|inconclusive","confidence","explanation","sources":[{"title","url"}],"start","end"}}`
  — sent once with `pending` and again when the verdict is ready
- `{"type":"error","message":"..."}`
