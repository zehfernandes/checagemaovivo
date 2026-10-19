"""Admin API behind /admin (password in the `x-admin-password` header): rooms, the fact-check
cache (edit/delete claims with live fan-out) and the news DB (browse/add/delete articles)."""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import audio
from .channels import ChannelManager
from .config import settings
from .news import NewsStore
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


class RoomStart(BaseModel):
    language: str | None = None
    save: bool = False
    start_seconds: float | None = None


class DocIn(BaseModel):
    url: str = ""
    title: str = ""
    text: str = ""
    source: str = "admin"


def make_router(channels: ChannelManager, store: NewsStore | None, rooms: RoomStore, require_admin) -> APIRouter:
    r = APIRouter()
    guard = [Depends(require_admin)]

    def room_view(room: dict) -> dict:
        vid = room["video_id"]
        ch = channels.channels.get(vid)
        row = channels.cache.video(vid)
        out = {**room, "active": bool(room["active"]), "title": (ch.info.title if ch else (row or {}).get("title")),
               "is_live": (ch.info.is_live if ch else bool((row or {}).get("is_live"))),
               "processing": bool(ch and ch.run_task and not ch.run_task.done()),
               "viewers": len(ch.subscribers) if ch else 0,
               "complete": ch.complete if ch else bool(row and row["status"] == "complete"),
               "processed_to": ch.processed_to if ch else float((row or {}).get("processed_to") or 0),
               "state": ch.status["state"] if ch else ("cached" if row else "new"),
               "message": ch.status["message"] if ch else "",
               "claims": len(ch.claims) if ch else channels.cache.claim_count(vid),
               "usage": ch.pipeline.usage.summary() if ch and ch.pipeline else None}
        return out

    # ---- rooms ---------------------------------------------------------------------------
    @r.get("/rooms")
    async def public_rooms():
        return [room_view(x) for x in rooms.list(active_only=True)]

    @r.get("/admin/rooms", dependencies=guard)
    async def admin_rooms():
        return [room_view(x) for x in rooms.list()]

    @r.post("/admin/rooms", dependencies=guard)
    async def create_room(body: RoomIn):
        if not audio.extract_video_id(body.url):
            raise HTTPException(400, "Informe um link válido do YouTube")
        if not body.name.strip():
            raise HTTPException(400, "Dê um nome à sala")
        return room_view(rooms.create(body.name, body.url, body.kind))

    @r.patch("/admin/rooms/{room_id}", dependencies=guard)
    async def patch_room(room_id: str, body: RoomPatch):
        if body.url is not None and not audio.extract_video_id(body.url):
            raise HTTPException(400, "Informe um link válido do YouTube")
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
    @r.get("/admin/claims", dependencies=guard)
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

    # ---- news DB -------------------------------------------------------------------------
    def need_store() -> NewsStore:
        if not store:
            raise HTTPException(503, "Base de notícias desativada (PROVIDER=mock ou sem chave da OpenAI)")
        return store

    @r.get("/admin/news/docs", dependencies=guard)
    async def news_docs(q: str = "", limit: int = 100, offset: int = 0):
        if not store:
            return {"total": 0, "docs": [], "disabled": True}
        return store.list_docs(q, min(limit, 500), offset)

    @r.post("/admin/news/docs", dependencies=guard)
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

    @r.delete("/admin/news/docs/{doc_id}", dependencies=guard)
    async def news_delete(doc_id: int):
        if not need_store().delete_doc(doc_id):
            raise HTTPException(404, "Artigo não encontrado")
        return {"ok": True}

    return r
