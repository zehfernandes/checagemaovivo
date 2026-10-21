"""Admin API behind /admin (password in the `x-admin-password` header): rooms, the fact-check
cache (edit/delete claims with live fan-out) and the news DB (browse/add/delete articles)."""
from __future__ import annotations

import asyncio
import html
import ipaddress
import logging
import re
import shutil
import socket
import sqlite3
import tempfile
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import audio
from .auth import User
from .channels import ChannelManager, ReviewConflict
from .config import settings
from .news import UA, NewsStore
from .rooms import RoomStore

log = logging.getLogger(__name__)


class RoomIn(BaseModel):
    name: str
    url: str
    kind: str = "debate"


class RoomPatch(BaseModel):
    name: str | None = None
    url: str | None = None
    kind: str | None = None
    active: bool | None = None
    human_review: bool | None = None  # "Aguardar checagem humana": changeable while checking runs, applies to new claims


class RoomStart(BaseModel):
    language: str | None = None
    save: bool = False
    start_seconds: float | None = None


class ReviewIn(BaseModel):
    url: str
    reviewer: str = ""  # typed name; only used without Google sign-in (the signed-in person wins)
    skip: list[str] = []  # next: claims this reviewer already passed on
    after: str | None = None  # next: the claim just reviewed, so the queue continues from there


class DecisionIn(BaseModel):
    url: str
    reviewer: str = ""
    action: str  # "publish" | "irrelevant"
    rev: int | None = None  # the claim version the reviewer loaded (stale -> 409)
    verdict: str | None = None
    explanation: str | None = None
    sources: list[dict] | None = None
    text: str | None = None
    speaker: str | None = None
    quote: str | None = None  # the sentence as shown on the site ("Nós criamos…")


class DocIn(BaseModel):
    url: str = ""
    title: str = ""
    text: str = ""
    source: str = "admin"


_OG_TITLE = re.compile(r"""<meta[^>]+(?:property|name)=["'](?:og:title|twitter:title)["'][^>]*>""", re.I)
_CONTENT = re.compile(r"""content=["']([^"']*)["']""", re.I)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


async def _public_host(url: str) -> bool:
    """Only fetch pages on the public internet (no localhost / private network from the server)."""
    host = httpx.URL(url).host
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    except OSError:
        return False
    return all(ipaddress.ip_address(i[4][0]).is_global for i in infos)


async def page_title(url: str) -> str:
    """og:title, else <title>, read from the first ~400 KB of the page."""
    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=8, follow_redirects=True) as http:
        async with http.stream("GET", url) as r:
            if not await _public_host(str(r.url)):
                return ""
            r.raise_for_status()
            chunks, size = [], 0
            async for chunk in r.aiter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size > 400_000 or b"</head>" in chunk.lower():
                    break
            page = b"".join(chunks).decode(r.encoding or "utf-8", errors="ignore")
    for tag in _OG_TITLE.findall(page):
        m = _CONTENT.search(tag)
        if m and m.group(1).strip():
            return html.unescape(m.group(1)).strip()
    m = _TITLE.search(page)
    return html.unescape(re.sub(r"\s+", " ", m.group(1))).strip() if m else ""


def make_router(channels: ChannelManager, store: NewsStore | None, rooms: RoomStore, require_user, require_owner) -> APIRouter:
    r = APIRouter()
    guard = [Depends(require_owner)]  # rooms and data exports: owners only
    checker = [Depends(require_user)]  # fact-checks, news DB, glossary: checkers too

    def room_view(room: dict) -> dict:
        vid = room["video_id"]
        ch = channels.channels.get(vid)
        row = channels.cache.video(vid)
        out = {**room, "active": bool(room["active"]), "human_review": bool(room.get("human_review", 1)), "title": (ch.info.title if ch else (row or {}).get("title")),
               "is_live": (ch.info.is_live if ch else bool((row or {}).get("is_live"))),
               "processing": bool(ch and ch.run_task and not ch.run_task.done()),
               "viewers": len(ch.subscribers) if ch else 0,
               "complete": ch.complete if ch else bool(row and row["status"] == "complete"),
               "processed_to": ch.processed_to if ch else float((row or {}).get("processed_to") or 0),
               "state": ch.status["state"] if ch else ("error" if vid in channels.failures else "cached" if row else "new"),
               "message": ch.status["message"] if ch else channels.failures.get(vid, ""),
               "source": audio.source_of(room["url"]),
               "claims": len(ch.claims) if ch else channels.cache.claim_count(vid),
               "usage": ch.pipeline.usage.summary() if ch and ch.pipeline else None}
        return out

    # ---- rooms ---------------------------------------------------------------------------
    # Snapshot of a data file so a fresh clone can start with production's cache/news/glossary
    # (`backend/data/` is gitignored). SQLite files are copied with the online backup API.
    data_dir = Path(settings.cache_db_path).resolve().parent

    @r.get("/admin/export/{name}", dependencies=guard)
    async def export_data(name: str, bg: BackgroundTasks):
        if name not in {"cache.db", "news.db", "review.db", "glossary.json"}:
            raise HTTPException(404, "Arquivo desconhecido")
        src = data_dir / name
        if not src.exists():
            raise HTTPException(404, f"{name} ainda não existe no servidor")
        tmp = Path(tempfile.mkdtemp()) / name
        if name.endswith(".db"):
            with sqlite3.connect(src) as a, sqlite3.connect(tmp) as b:
                a.backup(b)
        else:
            shutil.copyfile(src, tmp)
        bg.add_task(shutil.rmtree, tmp.parent, True)
        return FileResponse(tmp, filename=name, media_type="application/octet-stream")

    @r.get("/rooms")
    async def public_rooms():
        return [room_view(x) for x in rooms.list(active_only=True)]

    @r.get("/admin/rooms", dependencies=checker)
    async def admin_rooms():
        return [room_view(x) for x in rooms.list()]

    @r.post("/admin/rooms", dependencies=guard)
    async def create_room(body: RoomIn):
        if not audio.is_supported_url(body.url):
            raise HTTPException(400, "Informe um link válido (YouTube, g1/Globo…)")
        if not body.name.strip():
            raise HTTPException(400, "Dê um nome à sala")
        return room_view(rooms.create(body.name, body.url, body.kind))

    @r.patch("/admin/rooms/{room_id}", dependencies=guard)
    async def patch_room(room_id: str, body: RoomPatch):
        if body.url is not None and not audio.is_supported_url(body.url):
            raise HTTPException(400, "Informe um link válido (YouTube, g1/Globo…)")
        room = rooms.update(room_id, **body.model_dump())
        if not room:
            raise HTTPException(404, "Sala não encontrada")
        return room_view(room)

    @r.delete("/admin/rooms/{room_id}", dependencies=guard)
    async def delete_room(room_id: str):
        room = rooms.get(room_id)
        if not room:
            raise HTTPException(404, "Sala não encontrada")
        ch = channels.channels.get(room["video_id"])
        if ch and ch.run_task and not ch.run_task.done():
            await ch.stop_pipeline()
        rooms.delete(room_id)
        return {"ok": True}

    @r.get("/admin/rooms/{room_id}/activity", dependencies=checker)
    async def room_activity(room_id: str):
        """What the backend is doing for this room: transcription mode, work in flight and the run's log."""
        room = rooms.get(room_id)
        if not room:
            raise HTTPException(404, "Sala não encontrada")
        ch = channels.channels.get(room["video_id"])
        return ch.pipeline.snapshot() if ch and ch.pipeline else {"mode": "", "in_flight": 0, "verifying": 0, "activity": []}

    @r.post("/admin/rooms/{room_id}/start", dependencies=guard)
    async def start_room(room_id: str, body: RoomStart):
        room = rooms.get(room_id)
        if not room:
            raise HTTPException(404, "Sala não encontrada")
        try:
            ch = await channels.get(room["url"], body.save)
        except Exception as e:  # noqa: BLE001
            log.exception("room %s: resolve failed", room_id)
            raise HTTPException(502, f"Não foi possível abrir o vídeo: {e}")
        await ch.ensure_running(body.language or settings.language or None, start_seconds=body.start_seconds)
        return room_view(room)

    @r.post("/admin/rooms/{room_id}/stop", dependencies=guard)
    async def stop_room(room_id: str):
        room = rooms.get(room_id)
        if not room:
            raise HTTPException(404, "Sala não encontrada")
        ch = channels.channels.get(room["video_id"])
        if ch:
            await ch.stop_pipeline()
        return room_view(room)

    # ---- fact-check cache ----------------------------------------------------------------
    @r.get("/admin/claims", dependencies=checker)
    async def list_claims(url: str, verdict: str | None = None, q: str = ""):
        vid = audio.extract_video_id(url) or url
        ch = channels.channels.get(vid)
        entries = list(ch.claims.values()) if ch else [ev for kind, ev in channels.cache.events(vid) if kind == "claim"]
        claims = [e["claim"] for e in entries]
        if verdict:
            claims = [c for c in claims if c["verdict"] == verdict]
        if q.strip():
            needle = q.strip().lower()
            claims = [c for c in claims if needle in c["text"].lower() or needle in (c.get("explanation") or "").lower()]
        claims.sort(key=lambda c: c["start"])
        return {"video_id": vid, "live": ch is not None, "claims": claims}

    @r.delete("/admin/claims/{video_id}/{claim_id}", dependencies=guard)
    async def delete_claim(video_id: str, claim_id: str):
        ch = channels.channels.get(video_id)
        if ch:
            ok = await ch.remove_claim(claim_id)
        else:
            ok = any(ev["claim"]["id"] == claim_id for kind, ev in channels.cache.events(video_id) if kind == "claim")
            if ok:
                channels.cache.delete_event(video_id, "claim", claim_id)
        if not ok:
            raise HTTPException(404, "Afirmação não encontrada")
        return {"ok": True}

    # ---- newsroom review: drafts wait for a person; one reviewer per claim at a time ------------
    async def review_channel(url: str):
        vid = audio.extract_video_id(url) or url
        try:
            return channels.channels.get(vid) or await channels.get(url)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"Não foi possível abrir o vídeo: {e}")

    def reviewer_of(user: User, typed: str) -> str:
        """With Google sign-in the reviewer is the signed-in person; otherwise (password/open) the typed name."""
        name = (user.name or user.email) if settings.google_client_id else typed
        name = name.strip()[:60]
        if not name:
            raise HTTPException(400, "Informe seu nome para revisar")
        return name

    @r.post("/admin/review/next")
    async def review_next(body: ReviewIn, user: User = Depends(require_user)):
        ch = await review_channel(body.url)
        return {"claim": ch.next_for_review(reviewer_of(user, body.reviewer), set(body.skip), body.after)}

    @r.post("/admin/review/{claim_id}/lease")
    async def review_lease(claim_id: str, body: ReviewIn, user: User = Depends(require_user)):
        ch = await review_channel(body.url)
        try:
            return {"claim": ch.lease(claim_id, reviewer_of(user, body.reviewer))}
        except KeyError:
            raise HTTPException(404, "Afirmação não encontrada")
        except ReviewConflict as e:
            raise HTTPException(409, str(e))

    @r.post("/admin/review/{claim_id}/release")
    async def review_release(claim_id: str, body: ReviewIn, user: User = Depends(require_user)):
        (await review_channel(body.url)).release(claim_id, reviewer_of(user, body.reviewer))
        return {"ok": True}

    @r.post("/admin/review/{claim_id}/skip")
    async def review_skip(claim_id: str, body: ReviewIn, user: User = Depends(require_user)):
        try:
            (await review_channel(body.url)).skip(claim_id, reviewer_of(user, body.reviewer))
        except KeyError:
            raise HTTPException(404, "Afirmação não encontrada")
        return {"ok": True}

    @r.post("/admin/review/{claim_id}/decide")
    async def review_decide(claim_id: str, body: DecisionIn, user: User = Depends(require_user)):
        if body.action not in ("publish", "irrelevant"):
            raise HTTPException(400, "Ação inválida")
        if body.verdict is not None and body.verdict not in ("fake", "valid", "inconclusive"):
            raise HTTPException(400, "Veredito inválido")
        ch = await review_channel(body.url)
        try:
            c = ch.decide(claim_id, reviewer_of(user, body.reviewer), body.action, body.rev, body.verdict, body.explanation,
                          body.sources, body.text, body.speaker, quote=body.quote)
        except KeyError:
            raise HTTPException(404, "Afirmação não encontrada")
        except ReviewConflict as e:
            raise HTTPException(409, str(e))
        if store and body.action == "publish" and c.get("method") == "human":  # a person's verdict: reuse it across videos
            try:
                vec = (await store.embed([c["text"]]))[0]
                store.remember_verdict(c["text"], vec, c["verdict"], 1.0, c["explanation"], c.get("sources") or [], "human")
            except Exception:  # noqa: BLE001
                log.exception("verdict memory write failed")
        return {"claim": c}

    @r.get("/admin/page-title", dependencies=checker)
    async def admin_page_title(url: str):
        """Title of a pasted source link (the browser cannot read other sites itself)."""
        if not re.match(r"https?://", url) or not await _public_host(url):
            raise HTTPException(400, "Link inválido")
        try:
            return {"title": await page_title(url)}
        except Exception:  # noqa: BLE001 — a slow or blocked site just leaves the title for the person to type
            log.info("page title failed: %s", url, exc_info=True)
            return {"title": ""}

    # ---- news DB -------------------------------------------------------------------------
    def need_store() -> NewsStore:
        if not store:
            raise HTTPException(503, "Base de notícias desativada (PROVIDER=mock ou sem chave da OpenAI)")
        return store

    @r.get("/admin/news/docs", dependencies=checker)
    async def news_docs(q: str = "", limit: int = 100, offset: int = 0):
        if not store:
            return {"total": 0, "docs": [], "disabled": True}
        return store.list_docs(q, min(limit, 500), offset)

    @r.post("/admin/news/docs", dependencies=checker)
    async def news_add(body: DocIn):
        s = need_store()
        url, text = body.url.strip(), body.text.strip()
        if url and not url.startswith("http"):
            raise HTTPException(400, "A URL precisa começar com http")
        if not url and len(text) < 40:
            raise HTTPException(400, "Informe uma URL ou um texto com pelo menos 40 caracteres")
        if url and s.has_url(url):
            raise HTTPException(409, "Esse link já está na base")
        t0 = time.time()
        if url:
            ok = await s.add_url(url, body.title, source=body.source or "admin", fallback_text=text)
            if not ok:
                raise HTTPException(422, "Não consegui extrair texto dessa página — cole o texto do artigo no campo de texto")
        else:
            if not await s.add_text(body.title, text, body.source or "admin"):
                raise HTTPException(422, "Nada foi indexado")
        return {"ok": True, "seconds": round(time.time() - t0, 1), **s.stats()}

    @r.delete("/admin/news/docs/{doc_id}", dependencies=checker)
    async def news_delete(doc_id: int):
        if not need_store().delete_doc(doc_id):
            raise HTTPException(404, "Artigo não encontrado")
        return {"ok": True}

    return r
