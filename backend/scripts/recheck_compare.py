"""Re-verify the cached claims of a video with another model (web search) and store the results
side by side with the current verdicts, without touching the cache.

  python3 -m scripts.recheck_compare --video Amwukq3hviM --model gpt-5-mini --effort low --limit 3
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import time
from pathlib import Path

from openai import AsyncOpenAI

from app.config import settings
from app.factcheck import VERIFY_SYSTEM, _add_source, _clean_explanation, _parse_json, _parse_verdict, _today
from app.models import Source

OUT = Path()



def load(video: str) -> tuple[list[dict], list[dict]]:
    con = sqlite3.connect(settings.cache_db_path)
    claims = [json.loads(j)["claim"] for (j,) in con.execute(
        "select json from events where kind='claim' and video_id=? order by start", (video,))]
    segs = [json.loads(j) for (j,) in con.execute(
        "select json from events where kind='transcript' and video_id=? order by start", (video,))]
    return claims, segs


def context_for(claim: dict, segs: list[dict]) -> str:
    start = float(claim.get("start") or 0)
    texts = [s["segment"]["text"] for s in segs if start - 90 <= float(s["segment"]["start"]) <= start]
    return " ".join(t for t in texts if t)[-800:]


async def verify(client: AsyncOpenAI, model: str, effort: str | None, claim: str, context: str) -> dict:
    user = f"TODAY: {_today()}\nCLAIM: {claim}\n\nTRANSCRIPT CONTEXT: {context}"
    kwargs: dict = {}
    if effort:
        kwargs["reasoning"] = {"effort": effort}
    else:
        kwargs["temperature"] = 0
    t0 = time.monotonic()
    res = await client.responses.create(
        model=model, instructions=VERIFY_SYSTEM, input=user,
        tools=[{"type": "web_search_preview", "search_context_size": "low"}], **kwargs)
    latency = time.monotonic() - t0
    data = _parse_json(res.output_text)
    verdict, confidence = _parse_verdict(data)
    sources: list[Source] = []
    for item in data.get("sources", []) or []:
        if isinstance(item, dict) and item.get("url"):
            _add_source(sources, str(item.get("title", "")), str(item["url"]))
    for item in getattr(res, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            for ann in getattr(content, "annotations", []) or []:
                if getattr(ann, "url", None):
                    _add_source(sources, getattr(ann, "title", "") or "", ann.url)
    u = res.usage
    searches = sum(1 for item in getattr(res, "output", []) or [] if getattr(item, "type", "") == "web_search_call")
    return {
        "verdict": verdict.value, "confidence": confidence,
        "explanation": _clean_explanation(str(data.get("explanation", ""))),
        "sources": [s.model_dump() for s in sources], "latency": round(latency, 1),
        "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
        "reasoning_tokens": getattr(getattr(u, "output_tokens_details", None), "reasoning_tokens", 0) or 0,
        "searches": searches,
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--model", default="gpt-5-mini")
    ap.add_argument("--effort", default="low", help="reasoning effort for gpt-5 models; '' to send temperature=0 instead")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--citing", default="", help="only claims whose current sources cite this host (e.g. flaviomentiu.com.br)")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    global OUT
    OUT = Path(args.out) if args.out else Path(settings.news_db_file).parent / f"recheck_{args.model}_{args.effort or 't0'}.json"
    claims, segs = load(args.video)
    if args.citing:
        claims = [c for c in claims if any(args.citing in (s.get("url") or "") for s in c.get("sources") or [])]
    if args.limit:
        claims = claims[: args.limit]
    done: dict[str, dict] = {}
    if OUT.exists():
        prev = json.loads(OUT.read_text())
        if prev.get("model") == args.model and prev.get("effort") == args.effort:
            done = {r["id"]: r for r in prev["rows"]}
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    sem = asyncio.Semaphore(args.concurrency)
    rows: list[dict] = []

    async def one(c: dict) -> None:
        if c["id"] in done:
            rows.append(done[c["id"]])
            return
        async with sem:
            try:
                new = await verify(client, args.model, args.effort or None, c["text"], context_for(c, segs))
            except Exception as e:  # noqa: BLE001
                new = {"error": str(e)[:300]}
        rows.append({"id": c["id"], "start": c.get("start"), "text": c["text"],
                     "old": {k: c.get(k) for k in ("verdict", "confidence", "explanation", "sources", "method")},
                     "new": new})
        n = len(rows)
        if n % 10 == 0:
            print(f"{n}/{len(claims)}", flush=True)
            OUT.write_text(json.dumps({"model": args.model, "effort": args.effort, "rows": rows}, ensure_ascii=False))

    await asyncio.gather(*(one(c) for c in claims))
    rows.sort(key=lambda r: r["start"] or 0)
    OUT.write_text(json.dumps({"model": args.model, "effort": args.effort, "rows": rows}, ensure_ascii=False, indent=1))
    ok = [r for r in rows if "verdict" in r["new"]]
    same = sum(1 for r in ok if r["new"]["verdict"] == r["old"]["verdict"])
    lat = sorted(r["new"]["latency"] for r in ok)
    print(f"done {len(ok)}/{len(rows)} ok; agree {same}/{len(ok)}; "
          f"latency p50={lat[len(lat)//2]}s p90={lat[int(len(lat)*0.9)]}s; "
          f"tokens in={sum(r['new']['input_tokens'] for r in ok)} out={sum(r['new']['output_tokens'] for r in ok)} "
          f"reasoning={sum(r['new']['reasoning_tokens'] for r in ok)} searches={sum(r['new']['searches'] for r in ok)}")
    print("saved", OUT)


if __name__ == "__main__":
    asyncio.run(main())
