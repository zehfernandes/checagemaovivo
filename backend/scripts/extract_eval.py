"""Re-extract the chunks behind audited claims with several extractor models and judge the fidelity of what comes out.
Usage: python3 -m scripts.extract_eval data/fidelity_audit.json ~/prod_snapshot.json [--n-faithful 40]"""
import argparse, asyncio, json, random, time
from collections import Counter
from openai import AsyncOpenAI
from app.config import settings
import app.factcheck as fc
from scripts.fidelity_audit import SYSTEM as JUDGE, window

NAMES = ["Lula", "Flávio Bolsonaro", "Jair Bolsonaro", "Vorcaro", "Polícia Federal", "Bolsa Família", "Eletrobras", "Petrobras", "Trump", "Maduro"]

async def main():
    ap = argparse.ArgumentParser(); ap.add_argument("audit"); ap.add_argument("snapshot"); ap.add_argument("--n-faithful", type=int, default=40)
    a = ap.parse_args(); random.seed(1)
    audit = json.load(open(a.audit)); segs = sorted(json.load(open(a.snapshot))["segs"], key=lambda s: s["start"])
    bad = [r for r in audit if r["category"] != "faithful"]; good = random.sample([r for r in audit if r["category"] == "faithful"], a.n_faithful)
    cases = []
    for r in bad + good:
        own = [s for s in segs if s["start"] <= r["start"] < s["start"] + 8] or [min(segs, key=lambda s: abs(s["start"] - r["start"]))]
        seg = own[0]; ctx = " ".join(s["text"] for s in segs if seg["start"] - 40 <= s["start"] < seg["start"])
        ents = [n for n in NAMES if n.lower() in (ctx + seg["text"]).lower()]
        cases.append((r, seg, ctx, ents))
    client = AsyncOpenAI(api_key=settings.openai_api_key); jsem = asyncio.Semaphore(8)
    async def judge(claim, start):
        async with jsem:
            res = await client.responses.create(model="gpt-5-mini", instructions=JUDGE, reasoning={"effort": "low"},
                input=f"CLAIM: {claim}\n\nTRANSCRIPT:\n{window(segs, start)}\n\nAnswer in JSON.", text={"format": {"type": "json_object"}})
            return json.loads(res.output_text)["category"]
    for model, effort in [("gpt-4.1-mini", "low"), ("gpt-5-mini", "minimal")]:
        fc._gen_params = (lambda m, e="low", _eff=effort: {"reasoning": {"effort": _eff}} if m.startswith("gpt-5") else {"temperature": 0})
        ck = fc.OpenAIFactChecker(client, model, None, 0.55); sem = asyncio.Semaphore(6); lat = []
        async def one(case):
            r, seg, ctx, ents = case
            async with sem:
                t = time.time(); out = await ck.extract_claims(ctx, seg["text"], [], entities=ents); lat.append(time.time() - t)
            cats = await asyncio.gather(*[judge(c, seg["start"]) for c in out])
            return r, out, list(cats)
        res = await asyncio.gather(*[one(c) for c in cases])
        cnt = Counter(c for _, _, cats in res for c in cats); n = sum(cnt.values())
        lat.sort(); print(f"\n=== {model}/{effort}: {n} claims from {len(cases)} chunks | faithful {cnt['faithful']}/{n} = {cnt['faithful']/max(n,1):.0%} | {dict(cnt)} | latency p50 {lat[len(lat)//2]:.1f}s p90 {lat[int(len(lat)*.9)]:.1f}s")
        for r, out, cats in res:
            if any(c != "faithful" for c in cats): print(f"  {r['category']:>14} {r['start']:8.1f} -> {list(zip(cats, out))}")
asyncio.run(main())
