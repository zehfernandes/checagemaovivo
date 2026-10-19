"""Summarise recheck_compare.json: agreement matrix, latency/cost, and the disagreements side by side."""
from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

from app.config import settings

PT = {"fake": "Falso", "valid": "Verdadeiro", "inconclusive": "Inconclusivo"}
d = json.loads(Path(next((a for a in sys.argv[1:] if a.endswith(".json")), Path(settings.news_db_file).parent / "recheck_compare.json")).read_text())
rows = [r for r in d["rows"] if "verdict" in r["new"]]
errs = [r for r in d["rows"] if "verdict" not in r["new"]]
matrix = collections.Counter((r["old"]["verdict"], r["new"]["verdict"]) for r in rows)
print(f"modelo novo: {d['model']} (effort={d['effort']}); {len(rows)} comparadas, {len(errs)} erros\n")
print("antigo \\ novo      Falso  Verdadeiro  Inconclusivo")
for o in ("fake", "valid", "inconclusive"):
    print(f"{PT[o]:<16}" + "".join(f"{matrix[(o, n)]:>12}" for n in ("fake", "valid", "inconclusive")))
same = sum(matrix[(v, v)] for v in PT)
print(f"\nconcordam: {same}/{len(rows)} ({100*same/len(rows):.0f}%)")
flips = [r for r in rows if {r['old']['verdict'], r['new']['verdict']} == {"fake", "valid"}]
to_inc = [r for r in rows if r["old"]["verdict"] != "inconclusive" and r["new"]["verdict"] == "inconclusive"]
from_inc = [r for r in rows if r["old"]["verdict"] == "inconclusive" and r["new"]["verdict"] != "inconclusive"]
print(f"inversões Falso<->Verdadeiro: {len(flips)}; viraram Inconclusivo: {len(to_inc)}; saíram de Inconclusivo: {len(from_inc)}")
lat = sorted(r["new"]["latency"] for r in rows)
print(f"latência novo: p50={lat[len(lat)//2]}s p90={lat[int(len(lat)*.9)]}s max={lat[-1]}s")
ti = sum(r["new"]["input_tokens"] for r in rows); to = sum(r["new"]["output_tokens"] for r in rows)
srch = sum(r["new"]["searches"] for r in rows)
cost = ti / 1e6 * 0.25 + to / 1e6 * 2.0 + srch * 0.025
print(f"custo novo ≈ US${cost:.2f} (in={ti} out={to} buscas={srch})")
if "--full" in sys.argv:
    print("\n=== DISCORDÂNCIAS ===")
    for r in sorted(rows, key=lambda r: r["start"] or 0):
        if r["old"]["verdict"] == r["new"]["verdict"]:
            continue
        t = int(r["start"] or 0)
        print(f"\n[{t//3600}:{(t%3600)//60:02d}:{t%60:02d}] {r['text']}")
        for tag, v in (("antigo", r["old"]), ("novo  ", r["new"])):
            print(f"  {tag} {PT[v['verdict']]:<12} {v.get('confidence', 0):.2f}  {v.get('explanation', '')[:260]}")
            for s in (v.get("sources") or [])[:2]:
                print(f"         - {s.get('url')}")
