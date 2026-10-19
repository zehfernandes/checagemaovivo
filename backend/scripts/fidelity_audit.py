"""Audit cached claims against the transcript they came from: does each claim faithfully say WHO did/received WHAT?
Usage: python3 -m scripts.fidelity_audit <snapshot.json> [--out data/fidelity_audit.json] [--concurrency 8]
snapshot.json = {"segs": [...], "claims": [...]} as received over the WebSocket."""
import argparse, asyncio, json, sys, time
from openai import AsyncOpenAI
from app.config import settings

SYSTEM = """You audit an automatic fact-checking pipeline for a Brazilian political debate (Lula vs. Flávio Bolsonaro, 2026).
You get a CLAIM that was extracted automatically and the TRANSCRIPT the claim was extracted from (the claim's own
segment is marked >>>). In a debate "você"/"o senhor" is the opponent being addressed; moderator cues ("candidato X")
and the flow of attacks tell who is speaking to whom.
Judge only whether the CLAIM faithfully restates what the speaker asserted — NOT whether it is true.
Categories:
- faithful: same actors, same direction (who paid/received/did), same figure.
- wrong_subject: the claim attributes the act to the wrong person (e.g. "você pegou X" written as another named person).
- wrong_direction: source and destination of money/action swapped, or receiver/payer inverted.
- not_said: the transcript does not contain this assertion (hallucinated, or opinion/question turned into fact).
- garbled: the figure/name is corrupted by transcription or merging (e.g. "34,134 milhões").
When not faithful, write corrected_claim: the faithful self-contained claim in pt-BR with the real actors
("Flávio Bolsonaro recebeu R$ 134 milhões de Daniel Vorcaro"), or "" if nothing verifiable was asserted.
Respond ONLY with JSON: {"category": "...", "confidence": 0.0-1.0, "corrected_claim": "...", "note": "<=20 words in pt-BR"}"""

def window(segs, start):
    rows = []
    for s in segs:
        if start - 40 <= s["start"] <= start + 8:
            mark = ">>> " if abs(s["start"] - start) < 8 or s["start"] <= start < s["start"] + 8 else "    "
            rows.append(f"{mark}[{int(s['start'])}] {s['text']}")
    return "\n".join(rows)

async def main():
    ap = argparse.ArgumentParser(); ap.add_argument("snapshot"); ap.add_argument("--out", default="data/fidelity_audit.json")
    ap.add_argument("--concurrency", type=int, default=8); ap.add_argument("--model", default="gpt-5-mini")
    a = ap.parse_args()
    snap = json.load(open(a.snapshot)); segs = sorted(snap["segs"], key=lambda s: s["start"])
    client = AsyncOpenAI(api_key=settings.openai_api_key); sem = asyncio.Semaphore(a.concurrency)
    async def one(c):
        async with sem:
            t = time.time()
            r = await client.responses.create(model=a.model, instructions=SYSTEM, reasoning={"effort": "low"},
                input=f"CLAIM: {c['text']}\n\nTRANSCRIPT:\n{window(segs, c['start'])}\n\nAnswer in JSON.",
                text={"format": {"type": "json_object"}})
            out = json.loads(r.output_text)
            out.update(id=c["id"], start=c["start"], text=c["text"], verdict=c["verdict"], latency=round(time.time() - t, 1))
            print(f"{out['category']:>15} {c['start']:8.1f} {c['text'][:70]}", file=sys.stderr); return out
    res = await asyncio.gather(*[one(c) for c in snap["claims"]])
    json.dump(res, open(a.out, "w"), ensure_ascii=False, indent=1)
    from collections import Counter
    print(Counter(r["category"] for r in res))
    for r in res:
        if r["category"] != "faithful": print(f"- [{r['category']}] {r['text']}  ->  {r['corrected_claim']}  ({r['note']})")
if __name__ == "__main__":
    asyncio.run(main())
