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
from .news import _blocked_host
from .pipeline import Pipeline

log = logging.getLogger(__name__)

LIVE_IDLE_SECONDS = 300  # stop a live pipeline this long after the last viewer leaves
VOD_IDLE_SECONDS = 60  # paced VOD processing stops this long after the last viewer leaves (resumes later)
PRIVATE_IDLE_SECONDS = 5  # a fresh (uncached) run dies shortly after its only viewer leaves
MAX_QUEUE = 2000
REVIEW_LEASE_SECONDS = 120  # an open review card holds its claim this long; the newsroom UI renews it while open
# newsroom-only fields: who is reviewing / reviewed a claim never reaches the public site
REVIEW_FIELDS = ("status", "claimed_by", "claimed_until", "reviewed_by", "reviewed_at", "rev", "skipped_by")


class ReviewConflict(Exception):
    """Another reviewer holds the claim, or changed it since this reviewer loaded it."""


def is_public(c: dict) -> bool:
    # claims cached before human review existed have no status and stay on the site
    return c.get("status", "published") == "published"


def public_view(ev: dict) -> dict:
    return {**ev, "claim": {k: v for k, v in ev["claim"].items() if k not in REVIEW_FIELDS}}


def _held_by_other(c: dict, reviewer: str) -> bool:
    return bool(c.get("claimed_by")) and c["claimed_by"] != reviewer and (c.get("claimed_until") or 0) > time.time()

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
        self.strip_blocked_sources(settings.blocked_domain_list)

    def strip_blocked_sources(self, hosts: list[str]) -> int:
        """Remove links to blocked (partisan) hosts from cached claims; verdicts are left untouched."""
        if not hosts:
            return 0
        changed = 0
        rows = self.conn.execute("SELECT video_id, key, json FROM events WHERE kind = 'claim'").fetchall()
        for r in rows:
            ev = json.loads(r["json"])
            sources = ev.get("claim", {}).get("sources") or []
            kept = [s for s in sources if not _blocked_host(s.get("url", ""), hosts)]
            if len(kept) != len(sources):
                ev["claim"]["sources"] = kept
                self.conn.execute("UPDATE events SET json = ? WHERE video_id = ? AND kind = 'claim' AND key = ?",
                                  (json.dumps(ev, ensure_ascii=False), r["video_id"], r["key"]))
                changed += 1
        self.conn.commit()
        if changed:
            log.info("stripped blocked-host sources from %d cached claims (%s)", changed, hosts)
        return changed

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
        # queue -> True for the newsroom (sees drafts and who reviews what), False for the public site
        self.subscribers: dict[asyncio.Queue, bool] = {}
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
    def subscribe(self, newsroom: bool = False) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)
        self.subscribers[q] = newsroom
        if self._idle_task:
            self._idle_task.cancel()
            self._idle_task = None
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.pop(q, None)
        paced_vod = not self.info.is_live and not settings.vod_full_speed
        if not self.subscribers and (self.private or self.info.is_live or paced_vod) and self.run_task and not self.run_task.done():
            idle = PRIVATE_IDLE_SECONDS if self.private else LIVE_IDLE_SECONDS if self.info.is_live else VOD_IDLE_SECONDS
            self._idle_task = asyncio.create_task(self._idle_shutdown(idle))

    async def _idle_shutdown(self, seconds: int) -> None:
        await asyncio.sleep(seconds)
        if not self.subscribers:
            log.info("channel %s: no viewers for %ss, stopping pipeline (processed to %.0fs)", self.video_id, seconds, self.processed_to)
            await self.stop_pipeline()

    def snapshot(self, newsroom: bool = False) -> list[dict]:
        """Everything a new viewer needs: info, history (time-ordered), current status.
        The public only gets published claims, without the newsroom's review fields."""
        events: list[dict] = [{"type": "info", "info": self.info.model_dump()}]
        events += sorted(self.transcripts.values(), key=lambda e: e["segment"]["start"])
        claims = sorted(self.claims.values(), key=lambda e: e["claim"]["start"])
        events += claims if newsroom else [public_view(e) for e in claims if is_public(e["claim"])]
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
            self._retries = 0  # audio is flowing again: a later drop gets a fresh set of reconnects
            self._put("transcript", seg["id"], seg["start"], ev)
            if self.info.clip_end is not None and seg["end"] >= self.info.clip_end and not self.complete:
                self.complete = True
                self.status = {"state": "listening", "message": "Processado por completo · servido do cache"}
                self._save("complete")
                self.broadcast({"type": "status", **self.status})
                asyncio.get_running_loop().call_soon(lambda: asyncio.create_task(self.stop_pipeline()))
        elif kind == "claim":
            # the machine (extraction, verdict, deep check): a claim waits for the newsroom unless review is off
            c = ev["claim"]
            old = self.claims.get(c["id"])
            if old is None:
                c = {**c, "status": "draft" if settings.human_review and not self.private else "published", "rev": 0}
            elif old["claim"].get("reviewed_by"):
                return  # a person already decided it: a late deep check must not overwrite their verdict
            else:
                c = {**c, **{k: old["claim"][k] for k in REVIEW_FIELDS if k in old["claim"]}}
            self._store_claim({**ev, "claim": c}, old)
            return
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

    def broadcast(self, ev: dict, public: dict | None | bool = True) -> None:
        """Send `ev` to the newsroom; the public gets `public` instead (True = the same event, None = nothing)."""
        for q, newsroom in list(self.subscribers.items()):
            out = ev if newsroom or public is True else public
            if out is None:
                continue
            try:
                q.put_nowait(out)
            except asyncio.QueueFull:
                log.warning("channel %s: dropping slow subscriber", self.video_id)
                self.subscribers.pop(q, None)

    def _store_claim(self, ev: dict, old: dict | None) -> None:
        c = ev["claim"]
        self.claims[c["id"]] = ev
        self._put("claim", c["id"], c["start"], ev)
        was_public = old is not None and is_public(old["claim"])
        if is_public(c):
            public = public_view(ev)
            if was_public and public == public_view(old):
                public = None  # only the newsroom's fields changed (a reviewer opened it): nothing new for the site
        else:  # unpublished (draft / irrelevant): the site never sees it, and drops it if it was there
            public = {"type": "claim_removed", "id": c["id"]} if was_public else None
        self.broadcast(ev, public)

    # ---- newsroom review: one person per claim at a time ----------------------------------
    def _review_entry(self, claim_id: str, reviewer: str) -> dict:
        entry = self.claims.get(claim_id)
        if not entry:
            raise KeyError(claim_id)
        if _held_by_other(entry["claim"], reviewer):
            raise ReviewConflict(f"{entry['claim']['claimed_by']} está revisando esta análise")
        return entry

    def lease(self, claim_id: str, reviewer: str) -> dict:
        """Open (or keep open) a claim's review card. No await between check and write: atomic on the event loop."""
        entry = self._review_entry(claim_id, reviewer)
        c = {**entry["claim"], "claimed_by": reviewer, "claimed_until": time.time() + REVIEW_LEASE_SECONDS}
        self._store_claim({**entry, "claim": c}, entry)
        return c

    def release(self, claim_id: str, reviewer: str) -> None:
        entry = self.claims.get(claim_id)
        if entry and entry["claim"].get("claimed_by") == reviewer:
            c = {**entry["claim"], "claimed_by": None, "claimed_until": None}
            self._store_claim({**entry, "claim": c}, entry)

    def skip(self, claim_id: str, reviewer: str) -> None:
        """`reviewer` passed on it: never offered to them again (they can still open it), still offered to others."""
        entry = self.claims.get(claim_id)
        if not entry:
            raise KeyError(claim_id)
        c = entry["claim"]
        skipped = sorted({*(c.get("skipped_by") or []), reviewer})
        held = c.get("claimed_by") == reviewer
        self._store_claim({**entry, "claim": {**c, "skipped_by": skipped, **({"claimed_by": None, "claimed_until": None} if held else {})}}, entry)

    def next_for_review(self, reviewer: str, skip: set[str] | None = None, after: str | None = None) -> dict | None:
        """The next draft in speaking order that the machine has finished, nobody else is reviewing and `reviewer`
        did not skip. With `after` (the card just closed) it continues from there and wraps to the oldest."""
        skip = skip or set()
        order = sorted(self.claims.values(), key=lambda e: (e["claim"]["start"], e["claim"]["id"]))
        if after and after in self.claims:
            mark = (self.claims[after]["claim"]["start"], after)
            order = ([e for e in order if (e["claim"]["start"], e["claim"]["id"]) > mark]
                     + [e for e in order if (e["claim"]["start"], e["claim"]["id"]) <= mark])
        for entry in order:
            c = entry["claim"]
            if (c.get("status") == "draft" and c["id"] not in skip and c["id"] != after
                    and reviewer not in (c.get("skipped_by") or []) and c["verdict"] != "pending"
                    and not c.get("deep_pending") and not _held_by_other(c, reviewer)):
                return self.lease(c["id"], reviewer)
        return None

    def decide(self, claim_id: str, reviewer: str, action: str, rev: int | None = None, verdict: str | None = None,
               explanation: str | None = None, sources: list[dict] | None = None, text: str | None = None,
               speaker: str | None = None, force: bool = False, quote: str | None = None) -> dict:
        """Publish (with the reviewer's edits) or mark irrelevant. `rev` is the version the reviewer loaded:
        if someone changed the claim since, nothing is written. Editing the verdict, explanation, sources
        or text makes it a human verdict; publishing as-is keeps the machine's verdict and its origin."""
        entry = self.claims.get(claim_id) if force else self._review_entry(claim_id, reviewer)
        if not entry:
            raise KeyError(claim_id)
        old = entry["claim"]
        if not force and rev is not None and rev != old.get("rev", 0):
            raise ReviewConflict(f"{old.get('reviewed_by') or 'Outra pessoa'} alterou esta análise enquanto você revisava")
        c = {**old}
        if action == "publish":
            verdict = verdict or old["verdict"]
            explanation = old.get("explanation", "") if explanation is None else explanation
            new_text = text.strip() if text and text.strip() and text.strip() != old["text"] else None
            if (verdict != old["verdict"] or explanation != old.get("explanation", "") or new_text is not None
                    or (sources is not None and sources != old.get("sources", []))):
                c.update(verdict=verdict, explanation=explanation, method="human", confidence=1.0, deep_pending=False)
                if sources is not None:
                    c["sources"] = sources
            if new_text:
                c["text"] = new_text
                c["quote"] = None  # viewers see the quote first; an admin rewrite must show instead
            if speaker is not None:
                c["speaker"] = speaker.strip() or None
            if quote is not None and quote.strip() and quote.strip() != (c.get("quote") or c["text"]):
                c["quote"] = quote.strip()  # wording on the site (speaker's own words): not a verdict change
            c["status"] = "published"
        elif action == "irrelevant":
            c["status"] = "irrelevant"
        else:
            raise ValueError(action)
        c.update(reviewed_by=reviewer, reviewed_at=time.time(), rev=old.get("rev", 0) + 1, claimed_by=None, claimed_until=None)
        self._store_claim({**entry, "claim": c}, entry)
        return c

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
                await self.emit({"type": "status", "state": "error", "message": f"Falha no processamento: {e}"})
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
                       text: str | None = None, speaker: str | None = None, reviewer: str = "admin") -> dict | None:
        """Human correction outside the review queue (POST /admin/claim): publishes for every viewer at once."""
        try:
            return self.decide(claim_id, reviewer, "publish", None, verdict, explanation, sources, text, speaker, force=True)
        except KeyError:
            return None

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
        self.failures: dict[str, str] = {}  # video_id -> last resolve error (no channel exists for it)
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
                info = StreamInfo(video_id=vid, title=row["title"], is_live=False, duration=row["duration"],
                                  source=audio.source_of(url), page_url=url)
            else:
                try:
                    info, _media = await audio.resolve_stream(url)
                except Exception as e:  # noqa: BLE001
                    self.failures[vid] = str(e)  # shown on the room until the link opens
                    raise
            self.failures.pop(vid, None)
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
