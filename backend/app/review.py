"""Offline review loop: batch-process a whole recording, let a human label every claim,
export a golden set and evaluate the live pipeline against it.

    python -m app.review batch <run-name> <dir-with-whisper-json|transcript.jsonl> [--from S] [--to S]
    python -m app.review eval <run-name> [--rerun]
    python -m app.review export <run-name> > golden.jsonl
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import sys
import time
import uuid
from pathlib import Path

from .config import BACKEND_DIR, settings
from .factcheck import FactChecker, VerifyResult
from .models import Verdict

log = logging.getLogger(__name__)

LABELS = ("correct", "wrong", "not_claim", "unsure")
WINDOW_SECONDS = 8
CONTEXT_WINDOWS = 3
RECENT_CLAIMS = 12
EXTRACT_CONCURRENCY = 6
VERIFY_CONCURRENCY = 6

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (name TEXT PRIMARY KEY, title TEXT, created REAL, windows INTEGER, items INTEGER);
CREATE TABLE IF NOT EXISTS segments (run TEXT, idx INTEGER, start REAL, end REAL, text TEXT, PRIMARY KEY (run, idx));
CREATE TABLE IF NOT EXISTS items (
  id TEXT PRIMARY KEY, run TEXT, start REAL, end REAL, segment TEXT, claim TEXT,
  verdict TEXT, confidence REAL, explanation TEXT, sources TEXT, method TEXT, created REAL
);
CREATE INDEX IF NOT EXISTS items_run ON items(run, start);
CREATE TABLE IF NOT EXISTS labels (
  item_id TEXT PRIMARY KEY, label TEXT, correct_verdict TEXT, note TEXT, source_url TEXT, updated REAL
);
CREATE TABLE IF NOT EXISTS missed (
  id TEXT PRIMARY KEY, run TEXT, start REAL, segment TEXT, claim TEXT, note TEXT, created REAL
);
"""


def _db_path() -> Path:
    return settings.news_db_file.parent / "review.db"


class ReviewStore:
    def __init__(self, path: Path | None = None):
        self.path = path or _db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # ---- reads -------------------------------------------------------------------------------
    def runs(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT r.*, (SELECT COUNT(*) FROM items i JOIN labels l ON l.item_id = i.id WHERE i.run = r.name) AS labelled
               FROM runs r ORDER BY created DESC"""
        ).fetchall()
        return [dict(r) for r in rows]

    def items(self, run: str) -> list[dict]:
        rows = self.conn.execute(
            """SELECT i.*, l.label, l.correct_verdict, l.note, l.source_url
               FROM items i LEFT JOIN labels l ON l.item_id = i.id WHERE i.run = ? ORDER BY i.start""",
            (run,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["sources"] = json.loads(d["sources"] or "[]")
            out.append(d)
        return out

    def segments(self, run: str, start: float = 0, end: float = 1e12) -> list[dict]:
        rows = self.conn.execute(
            "SELECT idx, start, end, text FROM segments WHERE run = ? AND start >= ? AND start < ? ORDER BY idx",
            (run, start, end),
        ).fetchall()
        return [dict(r) for r in rows]

    def missed(self, run: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM missed WHERE run = ? ORDER BY start", (run,))]

    # ---- writes ------------------------------------------------------------------------------
    def label(self, item_id: str, label: str, correct_verdict: str | None = None,
              note: str = "", source_url: str = "") -> dict:
        if label not in LABELS:
            raise ValueError(f"label must be one of {LABELS}")
        self.conn.execute(
            """INSERT INTO labels (item_id, label, correct_verdict, note, source_url, updated)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(item_id) DO UPDATE SET label=excluded.label, correct_verdict=excluded.correct_verdict,
               note=excluded.note, source_url=excluded.source_url, updated=excluded.updated""",
            (item_id, label, correct_verdict, note, source_url, time.time()),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT claim, verdict FROM items WHERE id = ?", (item_id,)).fetchone()
        return dict(row) if row else {}

    def add_missed(self, run: str, start: float, segment: str, claim: str, note: str = "") -> str:
        mid = uuid.uuid4().hex[:10]
        self.conn.execute("INSERT INTO missed VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (mid, run, start, segment, claim, note, time.time()))
        self.conn.commit()
        return mid

    def delete_missed(self, mid: str) -> None:
        self.conn.execute("DELETE FROM missed WHERE id = ?", (mid,))
        self.conn.commit()

    # ---- golden set / metrics ----------------------------------------------------------------
    def golden(self, run: str) -> list[dict]:
        """Labelled items as evaluation records: gold verdict is the stored one when 'correct',
        the human-corrected one when 'wrong'; 'not_claim' marks extraction false positives."""
        out = []
        for it in self.items(run):
            if not it.get("label") or it["label"] == "unsure":
                continue
            gold = None
            if it["label"] == "correct":
                gold = it["verdict"]
            elif it["label"] == "wrong":
                gold = it.get("correct_verdict") or None
            out.append({
                "id": it["id"], "run": run, "start": it["start"], "segment": it["segment"], "claim": it["claim"],
                "system_verdict": it["verdict"], "system_method": it["method"], "label": it["label"],
                "gold_verdict": gold, "note": it.get("note") or "", "source_url": it.get("source_url") or "",
            })
        for m in self.missed(run):
            out.append({"id": m["id"], "run": run, "start": m["start"], "segment": m["segment"], "claim": m["claim"],
                        "system_verdict": None, "system_method": None, "label": "missed", "gold_verdict": None,
                        "note": m.get("note") or "", "source_url": ""})
        return out

    def metrics(self, run: str) -> dict:
        items = self.items(run)
        labelled = [i for i in items if i.get("label")]
        n = {lab: sum(1 for i in labelled if i["label"] == lab) for lab in LABELS}
        missed = len(self.missed(run))
        real = n["correct"] + n["wrong"]
        per_method: dict[str, dict] = {}
        for i in labelled:
            if i["label"] in ("correct", "wrong"):
                m = per_method.setdefault(i["method"] or "?", {"correct": 0, "wrong": 0})
                m[i["label"]] += 1
        confusion: dict[str, dict[str, int]] = {}
        for i in labelled:
            if i["label"] == "wrong" and i.get("correct_verdict"):
                confusion.setdefault(i["verdict"], {}).setdefault(i["correct_verdict"], 0)
                confusion[i["verdict"]][i["correct_verdict"]] += 1
        return {
            "items": len(items), "labelled": len(labelled), "labels": n, "missed": missed,
            "extraction_precision": round(real / (real + n["not_claim"]), 3) if real + n["not_claim"] else None,
            "extraction_recall": round(real / (real + missed), 3) if real + missed else None,
            "verdict_accuracy": round(n["correct"] / real, 3) if real else None,
            "per_method": {k: {**v, "accuracy": round(v["correct"] / (v["correct"] + v["wrong"]), 3)}
                           for k, v in per_method.items()},
            "confusion": confusion,
        }


# ---- batch processing ------------------------------------------------------------------------

def load_transcript(path: Path) -> list[dict]:
    """Whisper verbose_json dumps (dir of part_*.json) or a jsonl of {start,end,text}."""
    segs: list[dict] = []
    if path.is_dir():
        for p in sorted(path.glob("*.json")):
            segs.extend(json.load(open(p)))
    else:
        for line in open(path):
            if line.strip():
                segs.append(json.loads(line))
    segs.sort(key=lambda s: s["start"])
    return segs


def windows(segs: list[dict], seconds: float = WINDOW_SECONDS) -> list[dict]:
    """Merge transcript segments into ~chunk-sized windows (mimics the live 8 s chunks)."""
    out: list[dict] = []
    cur: dict | None = None
    for s in segs:
        text = s["text"].strip()
        if not text:
            continue
        if cur is None:
            cur = {"start": s["start"], "end": s["end"], "text": text}
        else:
            cur["end"] = s["end"]
            cur["text"] += " " + text
        if cur["end"] - cur["start"] >= seconds:
            out.append(cur)
            cur = None
    if cur:
        out.append(cur)
    return out


async def run_batch(store: ReviewStore, checker: FactChecker, name: str, title: str, segs: list[dict],
                    t_from: float = 0, t_to: float = 1e12) -> dict:
    segs = [s for s in segs if t_from <= s["start"] < t_to]
    wins = windows(segs)
    store.conn.execute("DELETE FROM segments WHERE run = ?", (name,))
    store.conn.executemany("INSERT INTO segments VALUES (?, ?, ?, ?, ?)",
                           [(name, i, w["start"], w["end"], w["text"]) for i, w in enumerate(wins)])
    store.conn.execute("INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?, ?)", (name, title, time.time(), len(wins), 0))
    store.conn.commit()

    recent: list[str] = []
    verify_sem = asyncio.Semaphore(VERIFY_CONCURRENCY)
    extract_sem = asyncio.Semaphore(EXTRACT_CONCURRENCY)
    pending: set[asyncio.Task] = set()
    items = 0
    t0 = time.monotonic()

    async def verify(item_id: str, claim: str, context: str) -> None:
        nonlocal items
        async with verify_sem:
            try:
                r = await checker.verify(claim, context)
                if r.verdict == Verdict.INCONCLUSIVE and settings.deep_check:
                    r = await checker.deep_verify(claim, context)
            except Exception as e:  # noqa: BLE001
                log.exception("verify failed")
                r = VerifyResult(Verdict.INCONCLUSIVE, 0.0, f"Verification failed: {e}")
        store.conn.execute(
            "UPDATE items SET verdict=?, confidence=?, explanation=?, sources=?, method=? WHERE id=?",
            (r.verdict.value, r.confidence, r.explanation,
             json.dumps([s.model_dump() for s in r.sources]), r.method, item_id))
        store.conn.commit()
        items += 1

    async def extract(i: int, w: dict) -> None:
        context = " ".join(x["text"] for x in wins[max(0, i - CONTEXT_WINDOWS):i])
        async with extract_sem:
            try:
                claims = await checker.extract_claims(context, w["text"], list(recent[-RECENT_CLAIMS:]))
            except Exception:  # noqa: BLE001
                log.exception("extract failed at %.0fs", w["start"])
                return
        for c in claims:
            recent.append(c)
            item_id = uuid.uuid4().hex[:10]
            store.conn.execute(
                "INSERT INTO items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (item_id, name, w["start"], w["end"], w["text"], c, Verdict.PENDING.value, 0, "", "[]", None, time.time()))
            store.conn.commit()
            t = asyncio.create_task(verify(item_id, c, f"{context} {w['text']}"))
            pending.add(t)
            t.add_done_callback(pending.discard)

    for i in range(0, len(wins), EXTRACT_CONCURRENCY):
        await asyncio.gather(*(extract(j, wins[j]) for j in range(i, min(i + EXTRACT_CONCURRENCY, len(wins)))))
        if i % (EXTRACT_CONCURRENCY * 10) == 0:
            log.info("batch %s: window %d/%d, %d claims, %.0fs elapsed", name, i, len(wins), items, time.monotonic() - t0)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    store.conn.execute("UPDATE runs SET items = ? WHERE name = ?", (items, name))
    store.conn.commit()
    return {"run": name, "windows": len(wins), "items": items, "seconds": round(time.monotonic() - t0, 1)}


async def run_eval(store: ReviewStore, checker: FactChecker | None, run: str, rerun: bool) -> dict:
    """Metrics from labels; with --rerun, re-verify labelled claims with the current pipeline and
    score it against the gold verdicts (so prompt/threshold changes can be measured)."""
    report = store.metrics(run)
    if rerun and checker:
        gold = [g for g in store.golden(run) if g["gold_verdict"] in ("fake", "valid", "inconclusive")]
        sem = asyncio.Semaphore(VERIFY_CONCURRENCY)
        results: dict[str, int] = {"agree": 0, "disagree": 0}
        per_gold: dict[str, dict[str, int]] = {}
        disagreements = []

        async def one(g: dict) -> None:
            async with sem:
                r = await checker.verify(g["claim"], g["segment"])
                if r.verdict == Verdict.INCONCLUSIVE and settings.deep_check:
                    r = await checker.deep_verify(g["claim"], g["segment"])
            ok = r.verdict.value == g["gold_verdict"]
            results["agree" if ok else "disagree"] += 1
            per_gold.setdefault(g["gold_verdict"], {"agree": 0, "disagree": 0})["agree" if ok else "disagree"] += 1
            if not ok:
                disagreements.append({"claim": g["claim"], "gold": g["gold_verdict"], "got": r.verdict.value,
                                      "method": r.method, "explanation": r.explanation[:200]})

        await asyncio.gather(*(one(g) for g in gold))
        total = results["agree"] + results["disagree"]
        report["rerun"] = {"n": total, "accuracy": round(results["agree"] / total, 3) if total else None,
                           "per_gold": per_gold, "disagreements": disagreements}
    return report


def _cli() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)
    cmd, rest = args[0], args[1:]
    store = ReviewStore()
    if cmd == "export":
        for rec in store.golden(rest[0]):
            print(json.dumps(rec, ensure_ascii=False))
        return
    from .main import client, store as news_store  # noqa: PLC0415  (avoids import cycle)
    from .factcheck import make_factchecker
    checker = make_factchecker(client, news_store)
    if cmd == "batch":
        name, src = rest[0], Path(rest[1])
        t_from = float(rest[rest.index("--from") + 1]) if "--from" in rest else 0
        t_to = float(rest[rest.index("--to") + 1]) if "--to" in rest else 1e12
        title = rest[rest.index("--title") + 1] if "--title" in rest else name
        segs = load_transcript(src)
        print(json.dumps(asyncio.run(run_batch(store, checker, name, title, segs, t_from, t_to))))
    elif cmd == "eval":
        print(json.dumps(asyncio.run(run_eval(store, checker, rest[0], "--rerun" in rest)), ensure_ascii=False, indent=2))
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    _cli()
