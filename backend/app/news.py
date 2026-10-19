"""Local evidence store: trusted RSS feeds -> article text -> OpenAI embeddings -> SQLite.

Verification looks here first (cosine search over an in-memory matrix, ~ms) and only
falls back to live web search when nothing relevant is found. Pages found by web
search are written back so the next similar claim becomes a DB hit.
"""
import asyncio
import calendar
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import feedparser
import httpx
import numpy as np
import trafilatura
from openai import AsyncOpenAI

from . import usage

log = logging.getLogger(__name__)
logging.getLogger("trafilatura").setLevel(logging.CRITICAL)

DEFAULT_FEEDS: dict[str, str] = {
    "Lupa": "https://lupa.uol.com.br/feed",
    "g1 Fato ou Fake": "https://g1.globo.com/rss/g1/fato-ou-fake/",
    "g1 Política": "https://g1.globo.com/rss/g1/politica/",
    "Agência Brasil": "https://agenciabrasil.ebc.com.br/rss/ultimasnoticias/feed.xml",
    "Agência Brasil Política": "https://agenciabrasil.ebc.com.br/rss/politica/feed.xml",
    "Poder360": "https://www.poder360.com.br/feed/",
    "Folha Poder": "https://feeds.folha.uol.com.br/poder/rss091.xml",
    "CNN Brasil": "https://www.cnnbrasil.com.br/feed/",
    "E-Farsas": "https://www.e-farsas.com/feed",
    "Boatos.org": "https://www.boatos.org/feed",
}

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
CHUNK_CHARS = 900
MAX_CHUNKS_PER_DOC = 6
EMBED_BATCH = 64
_BOILERPLATE = re.compile(r"^(Siga a Lupa no Google|Veja nossas checagens|Leia também|Compartilhe|Assine)", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
  id INTEGER PRIMARY KEY, url TEXT UNIQUE, source TEXT, title TEXT,
  published REAL, kind TEXT, added REAL);
CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY, doc_id INTEGER REFERENCES docs(id) ON DELETE CASCADE,
  idx INTEGER, text TEXT, embedding BLOB);
CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS verdicts (
  id INTEGER PRIMARY KEY, claim TEXT, verdict TEXT, confidence REAL, explanation TEXT,
  sources TEXT, method TEXT, ts REAL, embedding BLOB);
"""


@dataclass
class Evidence:
    url: str
    source: str
    title: str
    published: float | None
    text: str
    score: float
    kind: str = "feed"


def _clean_text(text: str) -> str:
    lines, seen = [], set()
    for line in (ln.strip() for ln in text.splitlines()):
        if not line or _BOILERPLATE.match(line) or line in seen:
            continue
        seen.add(line)
        lines.append(line)
    return "\n".join(lines)


def _chunk(text: str) -> list[str]:
    chunks, cur = [], ""
    for para in text.split("\n"):
        if len(cur) + len(para) + 1 > CHUNK_CHARS and cur:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n{para}".strip() if cur else para
        if len(chunks) >= MAX_CHUNKS_PER_DOC:
            break
    if cur and len(chunks) < MAX_CHUNKS_PER_DOC:
        chunks.append(cur)
    return [c for c in chunks if len(c) > 80]


def _published(entry) -> float | None:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    return float(calendar.timegm(t)) if t else None


class NewsStore:
    def __init__(self, client: AsyncOpenAI, path: str | Path, embedding_model: str, feeds: dict[str, str] | None = None):
        self.client = client
        self.model = embedding_model
        self.feeds = feeds or DEFAULT_FEEDS
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self._refresh_lock = asyncio.Lock()
        self._mat = np.zeros((0, 1), dtype=np.float32)
        self._rows: list[tuple] = []  # doc_id, url, source, title, published, text, kind
        self._vmat = np.zeros((0, 1), dtype=np.float32)  # verdict memory: settled claims + their verdicts
        self._vrows: list[dict] = []
        self._load()
        self._load_verdicts()

    # ---------- storage ----------
    def _load(self) -> None:
        cur = self.db.execute(
            "SELECT d.id, d.url, d.source, d.title, d.published, c.text, d.kind, c.embedding "
            "FROM chunks c JOIN docs d ON d.id = c.doc_id ORDER BY c.id")
        rows, vecs = [], []
        for doc_id, url, source, title, published, text, kind, emb in cur:
            rows.append((doc_id, url, source, title, published, text, kind))
            vecs.append(np.frombuffer(emb, dtype=np.float32))
        self._rows = rows
        self._mat = np.vstack(vecs) if vecs else np.zeros((0, 1), dtype=np.float32)

    def _load_verdicts(self) -> None:
        cur = self.db.execute(
            "SELECT claim, verdict, confidence, explanation, sources, method, ts, embedding FROM verdicts ORDER BY id")
        rows, vecs = [], []
        for claim, verdict, conf, expl, sources, method, ts, emb in cur:
            rows.append({"claim": claim, "verdict": verdict, "confidence": conf, "explanation": expl,
                         "sources": json.loads(sources or "[]"), "method": method, "ts": ts})
            vecs.append(np.frombuffer(emb, dtype=np.float32))
        self._vrows = rows
        self._vmat = np.vstack(vecs) if vecs else np.zeros((0, 1), dtype=np.float32)

    # ---------- verdict memory (claim -> settled verdict, reused across videos) ----------
    def remember_verdict(self, claim: str, vec: np.ndarray, verdict: str, confidence: float,
                         explanation: str, sources: list[dict], method: str) -> None:
        ts = time.time()
        vec = vec.astype(np.float32)
        self.db.execute(
            "INSERT INTO verdicts (claim, verdict, confidence, explanation, sources, method, ts, embedding) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (claim, verdict, confidence, explanation, json.dumps(sources), method, ts, vec.tobytes()))
        self.db.commit()
        self._vrows.append({"claim": claim, "verdict": verdict, "confidence": confidence, "explanation": explanation,
                            "sources": sources, "method": method, "ts": ts})
        self._vmat = vec[None, :] if len(self._vrows) == 1 else np.vstack([self._vmat, vec[None, :]])

    def recall_verdict(self, vec: np.ndarray, min_similarity: float = 0.9,
                       max_age_days: float = 7) -> tuple[dict, float] | None:
        """Most similar remembered claim above `min_similarity` that is not older than `max_age_days`."""
        if not self._vrows:
            return None
        sims = self._vmat @ vec
        cutoff = time.time() - max_age_days * 86400
        for i in np.argsort(-sims):
            s = float(sims[i])
            if s < min_similarity:
                return None
            if self._vrows[i]["ts"] >= cutoff:
                return self._vrows[i], s
        return None

    def verdict_count(self) -> int:
        return len(self._vrows)

    def has_url(self, url: str) -> bool:
        return self.db.execute("SELECT 1 FROM docs WHERE url = ?", (url,)).fetchone() is not None

    def stats(self) -> dict:
        docs = self.db.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        chunks = self.db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        by_source = dict(self.db.execute("SELECT source, COUNT(*) FROM docs GROUP BY source ORDER BY 2 DESC"))
        last = self.db.execute("SELECT value FROM meta WHERE key = 'last_refresh'").fetchone()
        return {"docs": docs, "chunks": chunks, "sources": by_source,
                "last_refresh": float(last[0]) if last else None}

    async def embed(self, texts: list[str]) -> list[np.ndarray]:
        """Public: unit-normalized embeddings for arbitrary texts."""
        return await self._embed(texts)

    async def _embed(self, texts: list[str]) -> list[np.ndarray]:
        out: list[np.ndarray] = []
        for i in range(0, len(texts), EMBED_BATCH):
            res = await self.client.embeddings.create(model=self.model, input=texts[i:i + EMBED_BATCH])
            usage.track(self.model, res)
            for item in res.data:
                v = np.asarray(item.embedding, dtype=np.float32)
                out.append(v / (np.linalg.norm(v) or 1.0))
        return out

    async def _insert_docs(self, docs: list[dict]) -> int:
        """docs: {url, source, title, published, kind, text}. Returns chunks inserted."""
        texts, owners = [], []
        for d in docs:
            d["chunks"] = _chunk(_clean_text(d["text"]))
            for c in d["chunks"]:
                texts.append(f"{d['title']}\n{c}" if d["title"] else c)
                owners.append(d)
        if not texts:
            return 0
        vecs = await self._embed(texts)
        now = time.time()
        with self.db:
            ids: dict[str, int] = {}
            for d in docs:
                if not d["chunks"] or d["url"] in ids:
                    continue
                cur = self.db.execute(
                    "INSERT OR IGNORE INTO docs(url, source, title, published, kind, added) VALUES (?,?,?,?,?,?)",
                    (d["url"], d["source"], d["title"], d["published"], d["kind"], now))
                if cur.lastrowid and cur.rowcount:
                    ids[d["url"]] = cur.lastrowid
            counters: dict[str, int] = {}
            rows = []
            for d, text, vec in zip(owners, texts, vecs):
                doc_id = ids.get(d["url"])
                if doc_id is None:
                    continue
                idx = counters.get(d["url"], 0)
                counters[d["url"]] = idx + 1
                rows.append((doc_id, idx, text.split("\n", 1)[1] if d["title"] else text, vec.tobytes()))
            self.db.executemany("INSERT INTO chunks(doc_id, idx, text, embedding) VALUES (?,?,?,?)", rows)
        self._load()
        return len(rows)

    # ---------- ingest ----------
    async def _fetch_article(self, http: httpx.AsyncClient, url: str) -> tuple[str, str]:
        """Return (final_url, text)."""
        r = await http.get(url)
        r.raise_for_status()
        text = await asyncio.to_thread(
            trafilatura.extract, r.text, include_comments=False, include_tables=False, favor_recall=True)
        return str(r.url), text or ""

    async def refresh(self, max_age_days: int = 14, concurrency: int = 8) -> dict:
        async with self._refresh_lock:
            t0 = time.time()
            cutoff = t0 - max_age_days * 86400
            limits = httpx.Limits(max_connections=concurrency)
            async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=25, follow_redirects=True, limits=limits) as http:
                feeds = await asyncio.gather(*(http.get(u) for u in self.feeds.values()), return_exceptions=True)
                candidates: list[dict] = []
                seen: set[str] = set()
                for (source, feed_url), res in zip(self.feeds.items(), feeds):
                    if isinstance(res, Exception):
                        log.warning("feed %s failed: %s", source, res)
                        continue
                    parsed = feedparser.parse(res.content)
                    for e in parsed.entries:
                        link = (e.get("link") or "").strip()
                        if "*http" in link:  # folha redirector
                            link = "http" + link.split("*http", 1)[1]
                        pub = _published(e)
                        if not link or link in seen or (pub and pub < cutoff) or self.has_url(link):
                            continue
                        seen.add(link)
                        candidates.append({"url": link, "source": source, "title": (e.get("title") or "").strip(),
                                           "published": pub, "kind": "feed", "summary": e.get("summary") or ""})
                sem = asyncio.Semaphore(concurrency)

                async def grab(c: dict) -> dict | None:
                    async with sem:
                        try:
                            final, text = await self._fetch_article(http, c["url"])
                        except Exception as ex:  # noqa: BLE001
                            log.debug("article %s failed: %s", c["url"], ex)
                            final, text = c["url"], ""
                    if len(text) < 200:
                        text = trafilatura.html2txt(c["summary"]) if c["summary"] else ""
                    if len(text) < 120:
                        return None
                    c["text"] = text
                    return c

                fetched = [d for d in await asyncio.gather(*(grab(c) for c in candidates)) if d]
            inserted = 0
            for i in range(0, len(fetched), 40):
                inserted += await self._insert_docs(fetched[i:i + 40])
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('last_refresh', ?)", (str(time.time()),))
            result = {"candidates": len(candidates), "docs_added": len(fetched), "chunks_added": inserted,
                      "seconds": round(time.time() - t0, 1), **self.stats()}
            log.info("news refresh: %s", result)
            return result

    async def add_url(self, url: str, title: str = "", source: str = "web", fallback_text: str = "") -> bool:
        """Write-back for pages discovered by web search fallback.

        If the page can't be fetched (paywall / bot block), store `fallback_text` (the
        search-based summary) instead, flagged as unverified so the verifier treats it as weaker.
        """
        if self.has_url(url):
            return False
        text, final = "", url
        try:
            async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=20, follow_redirects=True) as http:
                final, text = await self._fetch_article(http, url)
        except Exception as ex:  # noqa: BLE001
            log.debug("write-back fetch %s failed: %s", url, ex)
        kind = "web"
        if len(text) < 200:
            if len(fallback_text) < 60:
                return False
            text, kind, final = fallback_text, "web-summary", url
        if self.has_url(final):
            return False
        n = await self._insert_docs([{"url": final, "source": source, "title": title, "published": time.time(),
                                      "kind": kind, "text": text}])
        log.info("write-back %s (%s): %d chunks", final, kind, n)
        return n > 0

    # ---------- query ----------
    async def search(self, query: str, k: int = 5, vec: np.ndarray | None = None) -> list[Evidence]:
        if not self._rows:
            return []
        q = vec if vec is not None else (await self._embed([query]))[0]
        sims = self._mat @ q
        best: dict[int, tuple[float, int]] = {}
        for i in np.argsort(-sims)[: k * 4]:
            doc_id = self._rows[i][0]
            if doc_id not in best:
                best[doc_id] = (float(sims[i]), int(i))
        out = []
        for score, i in sorted(best.values(), reverse=True)[:k]:
            doc_id, url, source, title, published, text, kind = self._rows[i]
            out.append(Evidence(url=url, source=source, title=title, published=published, text=text,
                                score=score, kind=kind or "feed"))
        return out


def _cli() -> None:  # python -m app.news refresh|stats|search "query"
    import json
    import sys

    from .config import settings

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    client = AsyncOpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url or None)
    store = NewsStore(client, settings.news_db_file, settings.embedding_model)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if cmd == "refresh":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else settings.news_max_age_days
        print(json.dumps(asyncio.run(store.refresh(days)), ensure_ascii=False, indent=2))
    elif cmd == "search":
        for ev in asyncio.run(store.search(" ".join(sys.argv[2:]))):
            print(f"{ev.score:.3f} [{ev.source}] {ev.title}\n   {ev.url}\n   {ev.text[:160]!r}")
    else:
        print(json.dumps(store.stats(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _cli()
