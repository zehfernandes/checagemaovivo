"""Apply a reviewed speaker curation to the shared cache: each claim gets the speaker (and the quote in their own words)
a person approved, or is taken off the site as irrelevant. The curation comes from scripts.backfill_speakers plus a
person's review (scripts/speaker_curation_<video>.json). On claims a person corrected in /admin (method "human") only the
speaker changes. --apply first copies cache.db next to it (cache.db.bak-<time>). Restart the backend after --apply: live channels
keep their claims in memory.

  python -m scripts.apply_speaker_curation scripts/speaker_curation_Amwukq3hviM.json --video Amwukq3hviM [--apply]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import Counter
from pathlib import Path

from app.config import settings

REVIEWER = "curadoria de falantes"  # shown in /admin as who took a claim off the site


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("curation")
    ap.add_argument("--video", required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    entries = json.loads(Path(a.curation).read_text())
    db_path = Path(settings.cache_db_path)
    cache = sqlite3.connect(db_path)
    rows = {key: json.loads(j) for key, j in
            cache.execute("SELECT key, json FROM events WHERE video_id = ? AND kind = 'claim'", (a.video,))}

    plan: Counter[str] = Counter()
    changes: list[tuple[str, dict]] = []
    now = time.time()
    for e in entries:
        ev = rows.get(e["id"])
        if not ev:
            plan["não encontrada"] += 1
            print(f"  não encontrada: {e['id']} {e['text'][:80]}")
            continue
        c = ev["claim"]
        if c.get("method") == "human":
            # a person's correction: only who said it changes; verdict, text and wording stay as they left them
            if e["action"] != "speaker" or (c.get("speaker") or None) == (e["speaker"] or None):
                plan["editada por pessoa (mantida)"] += 1
                continue
            c = {**c, "speaker": e["speaker"] or None}
            plan[f"editada por pessoa, só o falante: {e['speaker'] or '(nenhum)'}"] += 1
        elif e["action"] == "irrelevant":
            if c.get("status") == "irrelevant":
                plan["já irrelevante"] += 1
                continue
            c = {**c, "status": "irrelevant", "awaiting_review": False, "reviewed_by": REVIEWER, "reviewed_at": now,
                 "rev": c.get("rev", 0) + 1, "claimed_by": None, "claimed_until": None}
            plan["sai do site (irrelevante)"] += 1
        else:
            new = {**c, "speaker": e["speaker"] or None}
            if e.get("quote"):
                new["quote"] = e["quote"]
            if new == c:
                plan["sem mudança"] += 1
                continue
            c = new
            plan[f"falante: {e['speaker'] or '(nenhum)'}"] += 1
        changes.append((e["id"], {**ev, "claim": c}))
    untouched = len(set(rows) - {e["id"] for e in entries})

    for k, n in sorted(plan.items()):
        print(f"{n:>4}  {k}")
    print(f"{untouched:>4}  na base e fora da curadoria (não mexe)")
    if not a.apply:
        print("simulação (use --apply para gravar)")
        return
    backup = db_path.with_name(f"{db_path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    with sqlite3.connect(backup) as b:
        cache.backup(b)
    for key, ev in changes:
        cache.execute("UPDATE events SET json = ? WHERE video_id = ? AND kind = 'claim' AND key = ?",
                      (json.dumps(ev, ensure_ascii=False), a.video, key))
    cache.commit()
    print(f"gravado: {len(changes)} afirmações · cópia de segurança em {backup}")


if __name__ == "__main__":
    main()
