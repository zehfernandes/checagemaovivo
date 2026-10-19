import asyncio
import json
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from .config import settings
from .factcheck import make_factchecker
from .models import StartRequest
from .news import NewsStore
from .channels import ChannelManager
from .pipeline import Pipeline
from .review import LABELS, ReviewStore, run_eval
from .transcribe import make_transcriber

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("app")

app = FastAPI(title="debate-fake-news")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = (
    AsyncOpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url or None)
    if settings.openai_api_key
    else None
)
if settings.provider == "openai" and client is None:
    log.warning("PROVIDER=openai but OPENAI_API_KEY is empty; falling back to mock")

store: NewsStore | None = (
    NewsStore(client, settings.news_db_file, settings.embedding_model, settings.news_feed_map)
    if client and settings.provider == "openai"
    else None
)
review = ReviewStore()
channels = ChannelManager(
    lambda emit, save=False: Pipeline(make_transcriber(client), make_factchecker(client, store), emit, save=save),
    settings.news_db_file.parent / Path(settings.cache_db_path).name,
)
_bg_tasks: set[asyncio.Task] = set()


async def _refresh_loop() -> None:
    while True:
        try:
            await store.refresh(settings.news_max_age_days)
        except Exception:  # noqa: BLE001
            log.exception("news refresh failed")
        await asyncio.sleep(settings.news_refresh_minutes * 60)


@app.on_event("startup")
async def _startup() -> None:
    if store and settings.news_refresh_minutes > 0:
        t = asyncio.create_task(_refresh_loop())
        _bg_tasks.add(t)
        t.add_done_callback(_bg_tasks.discard)


@app.get("/news")
async def news_stats():
    return store.stats() if store else {"docs": 0, "chunks": 0, "sources": {}, "last_refresh": None}


@app.post("/news/refresh")
async def news_refresh():
    if not store:
        return {"error": "news DB disabled (mock provider)"}
    return await store.refresh(settings.news_max_age_days)


@app.get("/health")
async def health():
    return {
        "ok": True,
        "provider": settings.provider if client or settings.provider == "mock" else "mock",
        "chunk_seconds": settings.chunk_seconds,
        "news": store.stats() if store else None,
    }



@app.get("/channels")
async def channel_stats():
    return channels.stats()


# ---- offline review loop (human labels -> golden set -> eval) ----------------------------------
@app.get("/review/runs")
async def review_runs():
    return review.runs()


@app.get("/review/items")
async def review_items(run: str):
    return {"items": review.items(run), "missed": review.missed(run), "metrics": review.metrics(run)}


@app.get("/review/segments")
async def review_segments(run: str, start: float = 0, end: float = 1e12):
    return review.segments(run, start, end)


class LabelRequest(BaseModel):
    item_id: str
    label: str
    correct_verdict: str | None = None
    note: str = ""
    source_url: str = ""


@app.post("/review/label")
async def review_label(req: LabelRequest):
    if req.label not in LABELS:
        raise HTTPException(400, f"label must be one of {LABELS}")
    item = review.label(req.item_id, req.label, req.correct_verdict, req.note, req.source_url)
    # Learning loop: a human-provided source for a wrong verdict goes straight into the news DB.
    if store and req.label == "wrong" and req.source_url.startswith("http") and item:
        summary = f"{item['claim']}\n\nRevisão humana: veredito correto = {req.correct_verdict}. {req.note}"
        t = asyncio.create_task(store.add_url(req.source_url, "", source="human-review", fallback_text=summary))
        _bg_tasks.add(t)
        t.add_done_callback(_bg_tasks.discard)
    return {"ok": True}


class MissedRequest(BaseModel):
    run: str
    start: float
    segment: str
    claim: str
    note: str = ""


@app.post("/review/missed")
async def review_missed(req: MissedRequest):
    return {"id": review.add_missed(req.run, req.start, req.segment, req.claim, req.note)}


@app.delete("/review/missed/{mid}")
async def review_missed_delete(mid: str):
    review.delete_missed(mid)
    return {"ok": True}


@app.get("/review/export")
async def review_export(run: str):
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in review.golden(run))
    return PlainTextResponse(body, media_type="application/x-ndjson",
                             headers={"Content-Disposition": f'attachment; filename="golden-{run}.jsonl"'})


@app.get("/review/eval")
async def review_eval(run: str, rerun: bool = False):
    checker = make_factchecker(client, store) if rerun else None
    return await run_eval(review, checker, run, rerun)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    """One socket = one viewer. Processing is shared per video (see channels.py): `start`
    subscribes to the video's channel (history first, then live events), `stop` unsubscribes."""
    await ws.accept()
    channel = None
    queue: asyncio.Queue | None = None
    sender: asyncio.Task | None = None

    async def pump(q: asyncio.Queue) -> None:
        while True:
            await ws.send_json(await q.get())

    async def leave() -> None:
        nonlocal queue, sender
        if sender:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            sender = None
        if channel and queue is not None:
            channel.unsubscribe(queue)
        queue = None

    try:
        while True:
            data = await ws.receive_json()
            kind = data.get("type")
            if kind == "start":
                try:
                    req = StartRequest.model_validate(data)
                except ValidationError as e:
                    await ws.send_json({"type": "error", "message": e.errors()[0]["msg"]})
                    continue
                await leave()
                await ws.send_json({"type": "status", "state": "resolving", "message": "Resolving stream…"})
                try:
                    channel = await channels.private(req.url, req.save) if req.fresh else await channels.get(req.url, req.save)
                except Exception as e:  # noqa: BLE001
                    log.exception("resolve failed")
                    await ws.send_json({"type": "status", "state": "error", "message": f"Could not resolve video: {e}"})
                    continue
                queue = channel.subscribe()
                for ev in channel.snapshot():
                    await ws.send_json(ev)
                sender = asyncio.create_task(pump(queue))
                await channel.ensure_running(req.language or settings.language or None,
                                             start_seconds=req.start_seconds if req.fresh else None)
            elif kind == "stop":
                await leave()
                await ws.send_json({"type": "status", "state": "idle", "message": "Stopped"})
            elif kind == "recheck":
                if channel:
                    await channel.recheck(str(data.get("claim_id", "")))
            elif kind == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        await leave()
