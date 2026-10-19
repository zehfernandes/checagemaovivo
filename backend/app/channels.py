"""Shared per-video processing.

One `Channel` per YouTube video runs a single `Pipeline` (one audio pull, one set of AI
calls) and fans its events out to every connected viewer. Everything is persisted in a
SQLite cache, so a video is never processed twice: late joiners (or a later session)
receive the stored transcript/claims instantly, VOD processing resumes where it stopped,
and completed videos are served entirely from cache.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from . import audio
from .config import settings
from .models import StreamInfo
from .pipeline import Pipeline

log = logging.getLogger(__name__)

LIVE_IDLE_SECONDS = 300  # stop a live pipeline this long after the last viewer leaves
VOD_IDLE_SECONDS = 60  # paced VOD processing stops this long after the last viewer leaves (resumes later)
PRIVATE_IDLE_SECONDS = 5  # a fresh (uncached) run dies shortly after its only viewer leaves
MAX_QUEUE = 2000

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
  video_id TEXT PRIMARY KEY, url TEXT, title TEXT, is_live INTEGER, duration REAL,
  status TEXT, processed_to REAL, updated REAL
);
CREATE TABLE IF NOT EXISTS events (
  video_id TEXT, kind TEXT, key TEXT, start REAL, json TEXT,
  PRIMARY KEY (video_id, kind, key)
);
CREATE INDEX IF NOT EXISTS events_video ON events(video_id, start);
"""

PipelineFactory = Callable[[Callable], Pipeline]


class Cache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def video(self, video_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM videos WHERE video_id = ?", (video_id,)).fetchone()
        return dict(r) if r else None

    def save_video(self, video_id: str, url: str, info: StreamInfo, status: str, processed_to: float) -> None:
        self.conn.execute(
            """INSERT INTO videos VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(video_id) DO UPDATE SET url=excluded.url, title=excluded.title, is_live=excluded.is_live,
               duration=excluded.duration, status=excluded.status, processed_to=excluded.processed_to, updated=excluded.updated""",
            (video_id, url, info.title, int(info.is_live), info.duration, status, processed_to, time.time()))
        self.conn.commit()

    def put(self, video_id: str, kind: str, key: str, start: float, payload: dict) -> None:
        self.conn.execute("INSERT OR REPLACE INTO events VALUES (?, ?, ?, ?, ?)",
                          (video_id, kind, key, start, json.dumps(payload, ensure_ascii=False)))
        self.conn.commit()

    def delete_event(self, video_id: str, kind: str, key: str) -> None:
        self.conn.execute("DELETE FROM events WHERE video_id = ? AND kind = ? AND key = ?", (video_id, kind, key))
        self.conn.commit()

    def events(self, video_id: str) -> list[tuple[str, dict]]:
        rows = self.conn.execute("SELECT kind, json FROM events WHERE video_id = ? ORDER BY start, kind DESC", (video_id,))
        return [(r["kind"], json.loads(r["json"])) for r in rows]

    def claim_count(self, video_id: str) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM events WHERE video_id = ? AND kind = 'claim'", (video_id,)).fetchone()[0]

    def stats(self) -> dict:
        n = self.conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        c = self.conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'claim'").fetchone()[0]
        return {"videos": n, "claims": c}


def _mmss(s: float) -> str:
    return f"{int(s // 60)}:{int(s % 60):02d}"


class Channel:
    def __init__(self, manager: "ChannelManager", video_id: str, url: str, info: StreamInfo, private: bool = False,
                 save: bool = False):
        self.manager = manager
        self.save = save  # decided by whoever starts the channel; shown to every viewer
        self.private = private  # fresh run: not shared, never reads or writes the cache
        self.video_id = video_id
        self.url = url
        self.info = info
        self.subscribers: set[asyncio.Queue] = set()
        self.transcripts: dict[str, dict] = {}
        self.claims: dict[str, dict] = {}
        self.status = {"state": "idle", "message": "Pronto"}
        self.processed_to = 0.0
        self.complete = False
        self.pipeline: Pipeline | None = None
        self.run_task: asyncio.Task | None = None
        self._idle_task: asyncio.Task | None = None
        self._retries = 0
        self._language: str | None = None
        self._start_override: float | None = None
        clip = settings.clip_map.get(self.video_id)
        if clip and not info.is_live:
            info.clip_start, info.clip_end = clip
        for kind, ev in ([] if private else manager.cache.events(video_id)):
            if kind == "transcript":
                self.transcripts[ev["segment"]["id"]] = ev
                self.processed_to = max(self.processed_to, ev["segment"]["end"])
            elif kind == "claim":
                self.claims[ev["claim"]["id"]] = ev
        row = None if private else manager.cache.video(video_id)
        if row:
            self.processed_to = max(self.processed_to, row["processed_to"] or 0)
            self.complete = row["status"] == "complete" and not info.is_live
            if self.complete:
                self.status = {"state": "listening", "message": "Processado por completo · servido do cache"}

    # ---- fan-out ---------------------------------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)
        self.subscribers.add(q)
        if self._idle_task:
            self._idle_task.cancel()
            self._idle_task = None
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)
        paced_vod = not self.info.is_live and not settings.vod_full_speed
        if not self.subscribers and (self.private or self.info.is_live or paced_vod) and self.run_task and not self.run_task.done():
            idle = PRIVATE_IDLE_SECONDS if self.private else LIVE_IDLE_SECONDS if self.info.is_live else VOD_IDLE_SECONDS
            self._idle_task = asyncio.create_task(self._idle_shutdown(idle))

    async def _idle_shutdown(self, seconds: int) -> None:
        await asyncio.sleep(seconds)
        if not self.subscribers:
            log.info("channel %s: no viewers for %ss, stopping pipeline (processed to %.0fs)", self.video_id, seconds, self.processed_to)
            await self.stop_pipeline()

    def snapshot(self) -> list[dict]:
        """Everything a new viewer needs: info, history (time-ordered), current status."""
        events: list[dict] = [{"type": "info", "info": self.info.model_dump()}]
        events += sorted(self.transcripts.values(), key=lambda e: e["segment"]["start"])
        events += sorted(self.claims.values(), key=lambda e: e["claim"]["start"])
        events.append({"type": "status", **self.status})
        events.append(self._progress())
        return events

    def _progress(self) -> dict:
        return {"type": "progress", "processed_to": self.processed_to, "complete": self.complete,
                "viewers": len(self.subscribers), "fresh": self.private, "server_time": time.time(),
                "save": self.save, "usage": self.pipeline.usage.summary() if self.pipeline else None}

    def _put(self, kind: str, key: str, start: float, ev: dict) -> None:
        if not self.private:
            self.manager.cache.put(self.video_id, kind, key, start, ev)

    def _save(self, status: str) -> None:
        if not self.private:
            self.manager.cache.save_video(self.video_id, self.url, self.info, status, self.processed_to)

    async def emit(self, ev: dict) -> None:
        kind = ev.get("type")
        if kind == "transcript":
            seg = ev["segment"]
            self.transcripts[seg["id"]] = ev
            self.processed_to = max(self.processed_to, seg["end"])
            self._put("transcript", seg["id"], seg["start"], ev)
            if self.info.clip_end is not None and seg["end"] >= self.info.clip_end and not self.complete:
                self.complete = True
                self.status = {"state": "listening", "message": "Processado por completo · servido do cache"}
                self._save("complete")
                self.broadcast({"type": "status", **self.status})
                asyncio.get_running_loop().call_soon(lambda: asyncio.create_task(self.stop_pipeline()))
        elif kind == "claim":
            c = ev["claim"]
            self.claims[c["id"]] = ev
            self._put("claim", c["id"], c["start"], ev)
        elif kind == "status":
            if ev.get("state") == "ended":
                # Only trust "ended" when we actually reached the end of a recording; an audio source
                # failure (expired media URL -> HTTP 403, live HLS hiccup) also ends the chunk stream.
                reached_end = (not self.info.is_live and self.info.duration is not None
                               and self.processed_to >= self.info.duration - 2 * settings.chunk_seconds)
                if reached_end:
                    self.complete = True
                    self.status = {"state": "listening", "message": "Processado por completo · servido do cache"}
                    self._save("complete")
                else:
                    self._save("stopped")
                    if self.subscribers and self._retries < 3:
                        self._retries += 1
                        self.status = {"state": "resolving", "message": f"Áudio caiu em {_mmss(self.processed_to)} — reconectando ({self._retries}/3)…"}
                        asyncio.get_running_loop().call_later(3, lambda: asyncio.create_task(self.ensure_running(None, _retry=True)))
                    elif self.info.is_live:
                        self.status = {"state": "ended", "message": "Áudio da live terminou — clique em Iniciar para reconectar"}
                    else:
                        self.status = {"state": "error", "message": f"Áudio parou em {_mmss(self.processed_to)} — clique em Iniciar para continuar"}
                ev = {"type": "status", **self.status}
            elif ev.get("state") in ("listening", "resolving", "error", "ended"):
                self.status = {"state": ev["state"], "message": ev.get("message", "")}
        elif kind == "info":
            return  # viewers already got info from the snapshot
        self.broadcast(ev)
        if kind == "transcript":
            self.broadcast(self._progress())

    def broadcast(self, ev: dict) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                log.warning("channel %s: dropping slow subscriber", self.video_id)
                self.subscribers.discard(q)

    # ---- processing ------------------------------------------------------------------------
    async def ensure_running(self, language: str | None, _retry: bool = False, start_seconds: float | None = None) -> None:
        running = bool(self.run_task and not self.run_task.done())
        if (running and start_seconds is not None and not self.info.is_live and not self.complete
                and len(self.subscribers) <= 1 and start_seconds > self.processed_to + 30):
            # the only viewer pressed Start far ahead of what is processed: jump there instead of
            # making them wait for the paced run to catch up (shared channel, so only when alone)
            log.info("channel %s: jumping from %.0fs to %.0fs for its only viewer", self.video_id, self.processed_to, start_seconds)
            await self.stop_pipeline()
            running = False
        if self.complete or running:
            return
        if start_seconds is not None:
            self._start_override = start_seconds
        if not _retry:
            self._retries = 0
        if _retry and not self.subscribers:
            return
        language = language or self._language
        self._language = language
        self.pipeline = self.manager.make_pipeline(self.emit, self.save)
        start = 0.0 if self.info.is_live else (self._start_override if self._start_override is not None else self.processed_to)
        if self.info.clip_start is not None:
            start = max(start, self.info.clip_start)
        self.status = {"state": "resolving", "message": "Abrindo o vídeo…"}
        self.broadcast({"type": "status", **self.status})
        self._save("processing")

        async def run() -> None:
            try:
                await self.pipeline.run(self.url, language, start, realtime=not settings.vod_full_speed)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("channel %s: pipeline failed", self.video_id)
                await self.emit({"type": "status", "state": "error", "message": f"Processing failed: {e}"})
            finally:
                self._save("complete" if self.complete else "stopped")

        self.run_task = asyncio.create_task(run())

    async def stop_pipeline(self) -> None:
        if self.run_task and not self.run_task.done():
            self.run_task.cancel()
            await asyncio.gather(self.run_task, return_exceptions=True)
        if self.pipeline:
            await self.pipeline.stop(keep_verifications=True)
        self.status = {"state": "idle", "message": "Parado"}
        self.broadcast({"type": "status", **self.status})
        if self.private:
            self.manager.private_channels.discard(self)

    async def override(self, claim_id: str, verdict: str, explanation: str, sources: list[dict] | None = None,
                       text: str | None = None) -> dict | None:
        """Human correction by an admin: replaces the verdict for every viewer (live fan-out) and in the cache."""
        entry = self.claims.get(claim_id)
        if not entry:
            return None
        c = {**entry["claim"], "verdict": verdict, "explanation": explanation, "method": "human",
             "confidence": 1.0, "deep_pending": False}
        if sources is not None:
            c["sources"] = sources
        if text and text.strip():
            c["text"] = text.strip()
        await self.emit({**entry, "claim": c})
        return c

    async def remove_claim(self, claim_id: str) -> bool:
        """Admin deletes a claim (bad extraction): gone from the cache and from every viewer's list."""
        if claim_id not in self.claims:
            return False
        del self.claims[claim_id]
        if not self.private:
            self.manager.cache.delete_event(self.video_id, "claim", claim_id)
        self.broadcast({"type": "claim_removed", "id": claim_id})
        return True

    async def recheck(self, claim_id: str) -> None:
        if self.pipeline is None:
            self.pipeline = self.manager.make_pipeline(self.emit, self.save)
        entry = self.claims.get(claim_id)
        if entry and claim_id not in self.pipeline.claims:
            # claim came from cache: rebuild enough state for the deep pass
            from .models import Claim
            claim = Claim.model_validate(entry["claim"])
            ctx = " ".join(t["segment"]["text"] for t in self.transcripts.values()
                           if abs(t["segment"]["start"] - claim.start) <= 24)
            self.pipeline.claims[claim_id] = (claim, ctx)
        await self.pipeline.recheck(claim_id)


class ChannelManager:
    def __init__(self, make_pipeline: PipelineFactory, cache_path: Path):
        self.make_pipeline = make_pipeline
        self.cache = Cache(cache_path)
        self.channels: dict[str, Channel] = {}
        self.private_channels: set[Channel] = set()
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, url: str, save: bool = False) -> Channel:
        vid = audio.extract_video_id(url) or url
        lock = self._locks.setdefault(vid, asyncio.Lock())
        async with lock:
            ch = self.channels.get(vid)
            if ch:
                return ch
            row = self.cache.video(vid)
            if row and row["status"] == "complete" and not row["is_live"]:
                info = StreamInfo(video_id=vid, title=row["title"], is_live=False, duration=row["duration"])
            else:
                info, _media = await audio.resolve_stream(url)
            ch = Channel(self, info.video_id or vid, url, info, save=save)
            self.channels[ch.video_id] = ch
            if ch.video_id != vid:
                self.channels[vid] = ch
            return ch

    def cached(self, url: str) -> bool:
        """True if the video has (or is getting) shared results a visitor can watch."""
        vid = audio.extract_video_id(url) or url
        return vid in self.channels or self.cache.video(vid) is not None

    async def private(self, url: str, save: bool = False) -> Channel:
        """A one-off channel for a single viewer: fresh pipeline, no cache read/write, not shared.
        Used to measure true end-to-end latency instead of being served from cache."""
        info, _media = await audio.resolve_stream(url)
        vid = info.video_id or audio.extract_video_id(url) or url
        ch = Channel(self, vid, url, info, private=True, save=save)
        self.private_channels.add(ch)
        return ch

    def stats(self) -> dict:
        return {**self.cache.stats(), "active": [
            {"video_id": c.video_id, "title": c.info.title, "viewers": len(c.subscribers),
             "processing": bool(c.run_task and not c.run_task.done()), "complete": c.complete,
             "processed_to": c.processed_to, "fresh": c.private, "save": c.save,
             "usage": c.pipeline.usage.summary() if c.pipeline else None}
            for c in list({id(c): c for c in self.channels.values()}.values()) + list(self.private_channels)]}
