# debate-fake-news

Real-time fact-checking overlay for YouTube live streams (debates, broadcasts).

Pipeline: **YouTube audio → transcription → claim extraction → web-search verification → overlay**.
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
| `TRANSCRIBE_MODEL` | `gpt-4o-mini-transcribe` | speech-to-text model |
| `FACTCHECK_MODEL` | `gpt-4.1-mini` | model for claim extraction and verification |
| `CHUNK_SECONDS` | `8` | audio chunk size; smaller = lower latency, more API calls |
| `LANGUAGE` | `pt` | transcription language hint (ISO-639-1), empty for auto |
| `CORS_ORIGINS` | `http://localhost:5173` | allowed frontend origins |

## WebSocket protocol (`/ws`)

Client → server: `{"type":"start","url":"...","language":"pt","start_seconds":0}` · `{"type":"stop"}`

Server → client:

- `{"type":"status","state":"resolving|listening|ended|error|idle","message":"..."}`
- `{"type":"info","info":{"video_id","title","is_live","duration"}}`
- `{"type":"transcript","segment":{"id","text","start","end"}}`
- `{"type":"claim","claim":{"id","text","verdict":"pending|fake|valid|inconclusive","confidence","explanation","sources":[{"title","url"}],"start","end"}}`
  — sent once with `pending` and again when the verdict is ready
- `{"type":"error","message":"..."}`
