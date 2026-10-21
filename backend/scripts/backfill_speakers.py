"""Fill `speaker` and `quote` (the claim in the speaker's own words, "Eu…") on cached claims extracted before the
extractor produced them (or with an older prompt). Claims a human edited are left alone. Restart the backend after --apply: live channels keep
their claims in memory.

Two passes, because a debate turn outlives any short window: (1) walk the transcript in order, carrying who holds the
floor across segments and hand-offs ("Candidato Lula, dois minutos"); (2) for each claim, copy the speaker's own words.

  python3 -m scripts.backfill_speakers [--video Amwukq3hviM] [--apply]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from collections import Counter

from openai import AsyncOpenAI

from app.config import settings
from app.factcheck import _ADDRESSEE, _gen_params, _parse_json, clean_speaker

WALKS = 3

TURNS = """You follow a Brazilian political debate/broadcast segment by segment. Given who held the floor before, the
previous segments and the LATEST segment, answer in JSON:
- "speaker": the NAME of the candidate/politician speaking for most of the LATEST segment; "" when a moderator,
  journalist or reporter speaks (or it cannot be told).
- "speaker_at_end": the candidate who holds the floor at the END of the LATEST segment, after any hand-off in it
  ("Candidato Lula, dois minutos" -> "Lula"); "" if unclear.
A debate turn lasts minutes: the same person keeps speaking until a hand-off ("Candidato X, …", "Obrigada, candidato",
"sua réplica"). Being named or criticised by the speaker is not a hand-off. Write names without roles ("Lula") and fix
obvious transcription slips ("Fábio Bolsonaro" -> "Flávio Bolsonaro")."""

QUOTE = """Given a transcript excerpt, a CLAIM and who said it, answer in JSON {"quote": "..."}: the speaker's OWN WORDS
for that claim, copied from the SEGMENT WHERE THE CLAIM WAS SAID (the text before is only context) (first person stays first person:
"Eu mandei…", "No governo do meu pai…"). Copy, do not rephrase; only cut words outside the claim and replace
"você"/"o senhor"/"a senhora" with the opponent's NAME, and fix obvious transcription slips in names ("Fábio
Bolsonaro" -> "Flávio Bolsonaro"). "" if the words are not in the excerpt."""


async def ask(client: AsyncOpenAI, system: str, user: str) -> dict:
    res = await client.responses.create(
        model=settings.factcheck_model, instructions=system, input=f"{user}\n\nAnswer in JSON.",
        text={"format": {"type": "json_object"}}, **_gen_params(settings.factcheck_model, settings.extract_effort))
    return _parse_json(res.output_text)


async def speakers(client: AsyncOpenAI, segs: list[dict]) -> dict[float, str]:
    """Segment start -> speaker, walking in order (sequential on purpose: each answer feeds the next)."""
    out: dict[float, str] = {}
    floor = ""
    for i, s in enumerate(segs):
        prev = "\n".join(x["text"] for x in segs[max(0, i - 3):i]) or "(none)"
        d = await ask(client, TURNS, f"HELD THE FLOOR BEFORE: {floor or '(unknown)'}\n\nPREVIOUS SEGMENTS:\n{prev}\n\n"
                                     f"LATEST SEGMENT:\n{s['text']}")
        # an empty answer mid-turn means "unsure", not "moderator": claims only come from candidates, so keep the floor
        out[s["start"]] = clean_speaker(str(d.get("speaker") or "")) or floor
        floor = clean_speaker(str(d.get("speaker_at_end") or "")) or out[s["start"]]
    return out


async def quote(client: AsyncOpenAI, sem: asyncio.Semaphore, claim: dict, speaker: str, segs: list[dict]) -> str:
    before = "\n".join(s["text"] for s in segs if claim["end"] - 40 <= s["start"] < claim["start"] - 8)
    said = " ".join(s["text"] for s in segs if claim["start"] - 8 <= s["start"] < claim["end"])
    async with sem:
        d = await ask(client, QUOTE, f"TRANSCRIPT BEFORE:\n{before or '(none)'}\n\nSEGMENT WHERE THE CLAIM WAS SAID:\n{said}\n\n"
                                     f"SPEAKER: {speaker}\n\nCLAIM: {claim['text']}")
    q = str(d.get("quote") or "").strip()
    return "" if _ADDRESSEE.search(q) else q[:1].upper() + q[1:]


async def run(video: str | None, apply: bool) -> None:
    cache = sqlite3.connect(settings.cache_db_path)
    videos = [video] if video else [v for (v,) in cache.execute("SELECT DISTINCT video_id FROM events WHERE kind = 'claim'")]
    client = AsyncOpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url or None)
    sem = asyncio.Semaphore(8)
    for vid in videos:
        rows = cache.execute("SELECT kind, key, json FROM events WHERE video_id = ?", (vid,)).fetchall()
        segs: dict[float, dict] = {}
        for k, _, j in rows:
            if k == "transcript":
                s = json.loads(j)["segment"]
                segs.setdefault(s["start"], s)  # re-runs of the same stretch store duplicate segments
        ordered = [segs[t] for t in sorted(segs)]
        todo = [(key, json.loads(j)) for k, key, j in rows if k == "claim"]
        todo = [(key, ev) for key, ev in todo if ev["claim"].get("method") != "human"]
        print(f"{vid}: {len(todo)} afirmações sem fala/nome, {len(ordered)} trechos de transcrição")
        if not todo:
            continue
        # one walk can drift on a long video; three in parallel, majority per segment
        walks = await asyncio.gather(*(speakers(client, ordered) for _ in range(WALKS)))
        who = {t: Counter(w[t] for w in walks).most_common(1)[0][0] for t in walks[0]}

        def speaker_at(t: float) -> str:
            starts = [s for s in who if s <= t]
            return who[max(starts)] if starts else ""

        named = [(key, ev, speaker_at(ev["claim"]["start"])) for key, ev in todo]
        quotes = await asyncio.gather(*(quote(client, sem, ev["claim"], sp, ordered) if sp else asyncio.sleep(0, "")
                                        for _, ev, sp in named))
        for (key, ev, sp), q in zip(named, quotes):
            c = ev["claim"]
            print(f"  {int(c['start']):>6}s [{sp or '-'}] {q or c['text']}"[:160])
            if apply:
                ev["claim"] = {**c, "speaker": sp or None, "quote": q or None}
                cache.execute("UPDATE events SET json = ? WHERE video_id = ? AND kind = 'claim' AND key = ?",
                              (json.dumps(ev, ensure_ascii=False), vid, key))
        if apply:
            cache.commit()
    print("gravado" if apply else "simulação (use --apply para gravar)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    asyncio.run(run(args.video, args.apply))


if __name__ == "__main__":
    main()
