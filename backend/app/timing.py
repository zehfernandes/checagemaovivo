"""Place a claim inside the audio chunk it was extracted from.

Chunks are CHUNK_SECONDS long and the extractor only tells us *which* chunk a claim came from, so
until now every claim was stamped with the chunk start and the viewer revealed it up to 8 s before
the sentence was actually spoken. We approximate the moment the sentence ends by locating the
claim's content words in the chunk transcript and mapping the last match's character offset to time.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import unicodedata

_WORD = re.compile(r"\w+", re.UNICODE)
_STOP = {"que", "com", "para", "uma", "dos", "das", "não", "nao", "mais", "esse", "essa", "este", "esta",
         "isso", "ele", "ela", "eles", "elas", "você", "voce", "nós", "nos", "por", "como", "mas", "foi",
         "ser", "tem", "está", "esta", "são", "sao", "aos", "pelo", "pela", "até", "ate", "seu", "sua"}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s.lower())
    return "".join(ch for ch in s if unicodedata.category(ch) != "Mn")


def _content_words(s: str) -> list[str]:
    return [w for w in _WORD.findall(_norm(s)) if len(w) >= 4 and w not in _STOP]


def locate(claim: str, text: str, start: float, end: float) -> float:
    """Estimated time (seconds) at which the claim finished being said, within [start, end].

    Falls back to `start` when the claim's words are not in this chunk (it was extracted from the
    rolling context, i.e. said in an earlier chunk)."""
    if end <= start or not text:
        return start
    words = _content_words(claim)
    if not words:
        return start
    norm_text = _norm(text)
    hits = [m.end() for w in set(words) for m in re.finditer(r"\b" + re.escape(w), norm_text)]
    if len(hits) < max(2, len(set(words)) // 3):
        return start
    hits.sort()
    # last matched word ≈ end of the sentence; ignore a stray late match of a common word
    anchor = hits[-1] if len(hits) < 3 else hits[-2]
    frac = min(1.0, anchor / max(1, len(norm_text)))
    return round(start + frac * (end - start), 1)


def retime_cache(path: str, dry: bool = False) -> int:
    """Re-stamp cached claims with `locate` using the cached transcript of the same chunk."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    segs: dict[tuple[str, float], dict] = {}
    for r in conn.execute("SELECT video_id, json FROM events WHERE kind = 'transcript'"):
        seg = json.loads(r["json"])["segment"]
        segs[(r["video_id"], round(seg["start"], 1))] = seg
    moved = 0
    for r in conn.execute("SELECT rowid, video_id, key, start, json FROM events WHERE kind = 'claim'").fetchall():
        ev = json.loads(r["json"])
        c = ev["claim"]
        seg = segs.get((r["video_id"], round(c["start"], 1)))
        if seg is None:
            continue
        new = locate(c["text"], seg["text"], seg["start"], seg["end"])
        if new == c["start"]:
            continue
        moved += 1
        print(f"{c['start']:8.1f} -> {new:8.1f}  {c['text'][:70]}")
        if not dry:
            c["start"] = new
            conn.execute("UPDATE events SET start = ?, json = ? WHERE rowid = ?",
                         (new, json.dumps(ev, ensure_ascii=False), r["rowid"]))
    if not dry:
        conn.commit()
    return moved


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] != "retime":
        print("usage: python -m app.timing retime <cache.db> [--dry]")
        sys.exit(2)
    n = retime_cache(sys.argv[2], dry="--dry" in sys.argv)
    print(f"{'would move' if '--dry' in sys.argv else 'moved'} {n} claims")
