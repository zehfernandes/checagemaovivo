"""Apply a fidelity audit (scripts.fidelity_audit) to the shared cache: claims that asserted nothing verifiable
are deleted, the others get the faithful wording and are re-verified (web) with the current checker.
Usage: python3 -m scripts.fidelity_publish data/fidelity_audit.json ~/prod_snapshot.json --video Amwukq3hviM [--apply] [--out data/fidelity_fixes.json]
Dry run prints the plan; --apply writes cache.db. The resulting JSON can be re-applied on another cache (e.g. Fly) with --from-fixes."""
import argparse, asyncio, json, sqlite3, time
from pathlib import Path
from openai import AsyncOpenAI
from app.config import settings
from app.factcheck import OpenAIFactChecker

def window(segs, start):
    return " ".join(s["text"] for s in segs if start - 40 <= s["start"] <= start + 8)

async def main():
    ap = argparse.ArgumentParser(); ap.add_argument("audit"); ap.add_argument("snapshot")
    ap.add_argument("--video", required=True); ap.add_argument("--apply", action="store_true")
    ap.add_argument("--out", default="data/fidelity_fixes.json"); ap.add_argument("--from-fixes", help="reuse verdicts from a previous run")
    ap.add_argument("--curate", help="JSON list of {match, action|text, verdict?, confidence?, explanation?, method?} applied on top (human review)")
    a = ap.parse_args()
    audit = [r for r in json.loads(Path(a.audit).read_text()) if r["category"] != "faithful"]
    segs = sorted(json.loads(Path(a.snapshot).read_text())["segs"], key=lambda s: s["start"])
    fixes = {}
    if a.from_fixes:
        fixes = {f["id"]: f for f in json.loads(Path(a.from_fixes).read_text())}
    else:
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        checker = OpenAIFactChecker(client, settings.factcheck_model, None,
                                    settings.news_min_similarity, deep_model=settings.deep_model, verdict_model=settings.verdict_model,
                                    verdict_effort=settings.verdict_effort, deep_effort=settings.deep_effort)
        sem = asyncio.Semaphore(6)
        async def one(r):
            if not r["corrected_claim"].strip():
                return {"id": r["id"], "action": "delete", "old": r["text"], "category": r["category"]}
            async with sem:
                t = time.time()
                v = await checker._verify_with_web(r["corrected_claim"], window(segs, r["start"]))
            return {"id": r["id"], "action": "rewrite", "old": r["text"], "text": r["corrected_claim"], "category": r["category"],
                    "verdict": v.verdict.value, "confidence": v.confidence, "explanation": v.explanation,
                    "sources": [{"title": s.title, "url": s.url} for s in v.sources], "method": "web", "latency": round(time.time() - t, 1)}
        res = await asyncio.gather(*[one(r) for r in audit])
        fixes = {f["id"]: f for f in res}
        Path(a.out).write_text(json.dumps(list(fixes.values()), ensure_ascii=False, indent=1))
    if a.curate:
        for cur in json.loads(Path(a.curate).read_text()):
            hits = [f for f in fixes.values() if cur["match"] in f["old"]]
            assert len(hits) == 1, (cur["match"], len(hits))
            f = hits[0]
            if cur.get("action") == "delete":
                f.clear(); f.update(id=hits[0].get("id"), action="delete", old=cur["match"], category="curated")
                continue
            if f["action"] == "delete":
                raise SystemExit(f"curation rewrites a claim the audit deleted: {cur['match']}")
            f["text"] = cur["text"]
            for k in ("verdict", "confidence", "explanation", "method"):
                if k in cur: f[k] = cur[k]
            if cur.get("method") == "human": f["sources"] = f.get("sources") or []
    for f in fixes.values():
        if f["action"] == "delete": print(f"DELETE  [{f['category']}] {f['old']}")
        else: print(f"REWRITE [{f['category']}] {f['old']}\n     -> {f['text']}\n        {f['verdict']} {f['confidence']:.2f} {f['explanation'][:140]}")
    print(f"\n{sum(f['action']=='delete' for f in fixes.values())} deletions, {sum(f['action']=='rewrite' for f in fixes.values())} rewrites")
    if not a.apply:
        return
    cache = sqlite3.connect(settings.cache_db_path); n = 0
    for key, js in cache.execute("select key, json from events where kind='claim' and video_id=?", (a.video,)).fetchall():
        ev = json.loads(js); c = ev["claim"]; f = fixes.get(c["id"])
        if not f or c.get("method") == "human":
            continue
        if f["action"] == "delete":
            cache.execute("delete from events where video_id=? and kind='claim' and key=?", (a.video, key))
        else:
            c.update(text=f["text"], verdict=f["verdict"], confidence=f["confidence"], explanation=f["explanation"], sources=f["sources"],
                     method=f["method"], model=settings.verdict_model, verified_at=time.time(), deep_pending=False, fidelity=f["category"])
            cache.execute("update events set json=? where video_id=? and kind='claim' and key=?", (json.dumps(ev, ensure_ascii=False), a.video, key))
        n += 1
    cache.commit(); print(f"applied {n} changes to {settings.cache_db_path}")
asyncio.run(main())
