import asyncio
from typing import Literal
import json
import logging
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Header, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from .config import settings
from .factcheck import make_factchecker
from .models import StartRequest
from .news import NewsStore
from .glossary import glossary
from . import audio
from .channels import ChannelManager
from .pipeline import Pipeline
from .review import LABELS, ReviewStore, run_eval
from .rooms import RoomStore
from .admin import make_router as make_admin_router
from .auth import Access, User, UserStore, make_token, verify_google
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

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0"}


@app.middleware("http")
async def canonical_host(request: Request, call_next):
    host = request.headers.get("host", "").split(":")[0]
    if settings.canonical_host and host and host != settings.canonical_host and host not in _LOCAL_HOSTS:
        url = request.url.replace(scheme="https", netloc=settings.canonical_host)
        return RedirectResponse(str(url), status_code=301)
    return await call_next(request)

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
rooms = RoomStore(settings.news_db_file.parent / Path(settings.cache_db_path).name)
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
    if rooms.count() == 0 and settings.default_room_url:
        rooms.create(settings.default_room_name, settings.default_room_url)
    if store and settings.news_refresh_minutes > 0:
        t = asyncio.create_task(_refresh_loop())
        _bg_tasks.add(t)
        t.add_done_callback(_bg_tasks.discard)


users = UserStore(settings.news_db_file.parent / Path(settings.cache_db_path).name)
access = Access(users)


def current_user(x_admin_token: str = Header(default=""), x_admin_password: str = Header(default="")) -> User:
    """Any listed person (owner or checker). The old header still carries the password in password mode."""
    user = access.user(x_admin_token or x_admin_password)
    if user is None:
        raise HTTPException(401, "Entre com uma conta autorizada")
    return user


def require_owner(user: User = Depends(current_user)) -> User:
    if not user.is_owner:
        raise HTTPException(403, "Só owners podem fazer isso")
    return user


app.include_router(make_admin_router(channels, store, rooms, current_user, require_owner))


class LoginRequest(BaseModel):
    password: str = ""


@app.get("/admin/auth")
async def admin_auth():
    """How /admin signs people in: "google" (with the client id for the button), "password" or "open" (dev)."""
    return {"mode": access.mode, "google_client_id": settings.google_client_id or None}


@app.post("/admin/login")
async def admin_login(req: LoginRequest):
    """Password mode (no Google configured): the password itself is the credential."""
    user = access.user(req.password) if access.mode != "google" else None
    return {"ok": user is not None, "open": access.mode == "open", "mode": access.mode,
            "token": req.password if user else None, "user": user.view() if user else None}


class GoogleLogin(BaseModel):
    credential: str


@app.post("/admin/login/google")
async def admin_login_google(req: GoogleLogin):
    if access.mode != "google":
        raise HTTPException(400, "Login com Google não está configurado")
    try:
        claims = await verify_google(req.credential)
    except Exception as e:  # noqa: BLE001
        log.info("google login refused: %s", e)
        raise HTTPException(401, "Não foi possível confirmar sua conta Google")
    user = users.get(claims["email"])
    if user is None:
        log.info("google login: %s is not on the list", claims["email"])
        raise HTTPException(403, f"{claims['email']} não tem acesso. Peça a um owner para cadastrar seu e-mail.")
    users.seen(user.email, claims.get("name", ""), claims.get("picture", ""))
    user = users.get(user.email)
    return {"token": make_token(user.email), "user": user.view()}


@app.get("/admin/me")
async def admin_me(user: User = Depends(current_user)):
    return {**user.view(), "mode": access.mode}


class UserIn(BaseModel):
    email: str


@app.get("/admin/users", dependencies=[Depends(require_owner)])
async def admin_users():
    return users.list()


@app.post("/admin/users")
async def admin_user_add(req: UserIn, owner: User = Depends(require_owner)):
    try:
        return users.add(req.email, owner.email)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/admin/users/{email}", dependencies=[Depends(require_owner)])
async def admin_user_remove(email: str):
    if email.strip().lower() in users.owners():
        raise HTTPException(400, "Owners são definidos em OWNER_EMAILS e não podem ser removidos aqui")
    if not users.remove(email):
        raise HTTPException(404, "Usuário não encontrado")
    return {"ok": True}


class OverrideRequest(BaseModel):
    url: str
    claim_id: str
    verdict: Literal["fake", "valid", "inconclusive"]
    explanation: str = ""
    sources: list[dict] | None = None
    text: str | None = None  # admin rewrote the claim itself (bad extraction)
    speaker: str | None = None  # who said it; None = unchanged, "" = unknown


@app.post("/admin/claim", dependencies=[Depends(current_user)])
async def admin_claim(req: OverrideRequest):
    """Human fact-check: overrides a claim's verdict for everyone watching and in the cache + verdict memory."""
    vid = audio.extract_video_id(req.url) or req.url
    channel = channels.channels.get(vid) or await channels.get(req.url)
    c = await channel.override(req.claim_id, req.verdict, req.explanation, req.sources, req.text, req.speaker)
    if c is None:
        raise HTTPException(404, "Afirmação não encontrada")
    if store and c.get("method") == "human":  # a speaker-only fix is not a verdict
        try:
            vec = (await store.embed([c["text"]]))[0]
            store.remember_verdict(c["text"], vec, c["verdict"], 1.0, c["explanation"], c.get("sources") or [], "human")
        except Exception:  # noqa: BLE001
            log.exception("verdict memory write failed")
    return {"ok": True, "claim": c}


class GlossaryRequest(BaseModel):
    terms: str = ""
    replacements: list[dict] = []


@app.get("/admin/glossary", dependencies=[Depends(current_user)])
async def admin_glossary():
    return glossary.dump()


@app.put("/admin/glossary", dependencies=[Depends(current_user)])
async def admin_glossary_save(req: GlossaryRequest):
    """Glossary (names the speech model must spell right) + term fixes applied to every new transcript."""
    return glossary.save(req.terms, {str(r.get("from", "")): str(r.get("to", "")) for r in req.replacements})


@app.get("/news")
async def news_stats():
    return store.stats() if store else {"docs": 0, "chunks": 0, "sources": {}, "last_refresh": None}


@app.post("/news/refresh", dependencies=[Depends(current_user)])
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


@app.post("/review/label", dependencies=[Depends(current_user)])
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


@app.post("/review/missed", dependencies=[Depends(current_user)])
async def review_missed(req: MissedRequest):
    return {"id": review.add_missed(req.run, req.start, req.segment, req.claim, req.note)}


@app.delete("/review/missed/{mid}", dependencies=[Depends(current_user)])
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
                user = access.user(data.get("admin"))
                admin = user is not None
                if not (user and user.is_owner):  # checkers watch the newsroom view; only owners run pipelines
                    req.fresh = False
                    req.save = False
                if not admin:
                    # visitors only watch shared results: no fresh/save runs, never start processing
                    req.fresh = False
                    req.save = False
                    if not channels.cached(req.url):
                        failure = channels.failures.get(audio.extract_video_id(req.url) or req.url)
                        if failure:  # the newsroom tried and the link did not open: say so instead of "wait"
                            await ws.send_json({"type": "status", "state": "error", "code": "resolve_failed",
                                                "message": f"Não foi possível abrir o vídeo: {failure}"})
                        else:
                            await ws.send_json({"type": "status", "state": "error", "code": "not_started",
                                                "message": "A checagem desta transmissão ainda não começou. Volte em instantes."})
                        continue
                await leave()
                await ws.send_json({"type": "status", "state": "resolving", "message": "Abrindo o vídeo…"})
                try:
                    channel = await channels.private(req.url, req.save) if req.fresh else await channels.get(req.url, req.save)
                except Exception as e:  # noqa: BLE001
                    log.exception("resolve failed")
                    await ws.send_json({"type": "status", "state": "error", "code": "resolve_failed",
                                        "message": f"Não foi possível abrir o vídeo: {e}"})
                    continue
                queue = channel.subscribe()
                for ev in channel.snapshot():
                    await ws.send_json(ev)
                sender = asyncio.create_task(pump(queue))
                if not admin or req.watch or not user.is_owner:
                    continue
                await channel.ensure_running(req.language or settings.language or None,
                                             start_seconds=req.start_seconds if (req.fresh or len(channel.subscribers) <= 1) else None)
            elif kind == "stop":
                await leave()
                await ws.send_json({"type": "status", "state": "idle", "message": "Parado"})
            elif kind == "recheck":
                if channel and access.user(data.get("admin")):
                    await channel.recheck(str(data.get("claim_id", "")))
            elif kind == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        await leave()


# Production: serve the built frontend from the same origin (Dockerfile copies it here), so /ws, /news… are same-host.
_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _dist.is_dir():
    @app.get("/admin", include_in_schema=False)
    async def admin_page():
        return FileResponse(_dist / "index.html")

    app.mount("/", StaticFiles(directory=_dist, html=True), name="frontend")
