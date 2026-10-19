"""Publish the verdicts from a recheck_*.json into the shared cache (and the verdict memory),
keeping human-edited claims untouched.

  python3 -m scripts.recheck_publish data/recheck_gpt-5-mini_low.json --video Amwukq3hviM [--apply]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

from app.config import settings


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--video", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    d = json.loads(Path(args.file).read_text())
    new = {r["id"]: r["new"] for r in d["rows"] if "verdict" in r["new"]}
    cache = sqlite3.connect(settings.cache_db_path)
    news = sqlite3.connect(settings.news_db_file)
    changed = skipped_human = same = 0
    now = time.time()
    for key, js in cache.execute("select key, json from events where kind='claim' and video_id=?", (args.video,)).fetchall():
        ev = json.loads(js)
        c = ev["claim"]
        n = new.get(c["id"])
        if n is None:
            continue
        if c.get("method") == "human":
            skipped_human += 1
            continue
        if (c["verdict"], c.get("explanation")) == (n["verdict"], n["explanation"]):
            same += 1
            continue
        c.update(verdict=n["verdict"], confidence=n["confidence"], explanation=n["explanation"],
                 sources=n["sources"], method="web", verified_at=now, deep_pending=False)
        c["model"] = d["model"]
        changed += 1
        if args.apply:
            cache.execute("update events set json=? where video_id=? and kind='claim' and key=?",
                          (json.dumps(ev, ensure_ascii=False), args.video, key))
            news.execute("update verdicts set verdict=?, confidence=?, explanation=?, sources=?, method=?, ts=? where claim=?",
                         (n["verdict"], n["confidence"], n["explanation"], json.dumps(n["sources"], ensure_ascii=False),
                          "web", now, c["text"]))
    if args.apply:
        cache.commit(); news.commit()
    print(f"{'applied' if args.apply else 'dry-run'}: changed={changed} unchanged={same} human_kept={skipped_human} "
          f"memory_rows_matched={news.execute('select count(*) from verdicts where claim in (%s)' % ','.join('?' * len(new)), [r['text'] for r in d['rows'] if r['id'] in new]).fetchone()[0]}")


if __name__ == "__main__":
    main()
