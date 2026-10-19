import asyncio
import logging

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI
from pydantic import ValidationError

from .config import settings
from .factcheck import make_factchecker
from .models import StartRequest
from .news import NewsStore
from .pipeline import Pipeline
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


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    send_lock = asyncio.Lock()

    async def emit(msg: dict) -> None:
        async with send_lock:
            await ws.send_json(msg)

    pipeline: Pipeline | None = None
    run_task: asyncio.Task | None = None

    async def run(req: StartRequest) -> None:
        nonlocal pipeline
        pipeline = Pipeline(make_transcriber(client), make_factchecker(client, store), emit)
        try:
            await pipeline.run(req.url, req.language or settings.language or None, req.start_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("pipeline failed")
            await emit({"type": "error", "message": str(e)})
            await emit({"type": "status", "state": "error", "message": "Stopped"})

    async def stop() -> None:
        nonlocal run_task, pipeline
        if run_task and not run_task.done():
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)
        if pipeline:
            await pipeline.stop()
        run_task, pipeline = None, None

    try:
        while True:
            data = await ws.receive_json()
            kind = data.get("type")
            if kind == "start":
                try:
                    req = StartRequest.model_validate(data)
                except ValidationError as e:
                    await emit({"type": "error", "message": e.errors()[0]["msg"]})
                    continue
                await stop()
                run_task = asyncio.create_task(run(req))
            elif kind == "stop":
                await stop()
                await emit({"type": "status", "state": "idle", "message": "Stopped"})
            elif kind == "ping":
                await emit({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        await stop()
