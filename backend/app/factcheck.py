"""Two-stage fact-checking.

Stage 1 (fast, no tools): extract check-worthy factual claims from the newest
transcript segment. Stage 2: verify each claim and label it fake / valid /
inconclusive — first against the local news DB (fast, curated), falling back to
live web search when the DB has nothing conclusive; pages found by web search are
written back into the DB.
"""
import asyncio
import json
import logging
import random
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlparse

import numpy as np
from openai import AsyncOpenAI

from .config import settings
from .decider import JevDecider
from .models import Source, Verdict
from .news import Evidence, NewsStore

log = logging.getLogger(__name__)


@dataclass
class VerifyResult:
    verdict: Verdict
    confidence: float
    explanation: str
    sources: list[Source] = field(default_factory=list)
    method: str = "web"


class FactChecker(Protocol):
    async def extract_claims(self, context: str, latest: str, recent_claims: list[str]) -> list[str]: ...
    async def verify(self, claim: str, context: str) -> VerifyResult: ...
    async def deep_verify(self, claim: str, context: str) -> VerifyResult: ...


DUPLICATE_SIMILARITY = 0.90
RECENT_VEC_WINDOW = 40

EXTRACT_SYSTEM = """You are a fact-checking assistant watching a live political debate / broadcast.
You receive a rolling transcript. From the LATEST segment only, extract factual claims about the real
world that can be verified against public evidence: statistics, numbers, dates, past events, laws,
what someone did or did not do, comparisons, cause-effect statements.

Do NOT extract: promises or intentions about the future ("vou fazer", "vamos acabar com"), opinions,
values, insults, questions, greetings, procedural talk, hypotheticals, or anything already in RECENT CLAIMS.
If several sentences make the same point, merge them into ONE claim. Return at most 2 claims; usually 0 or 1.

Write each claim as a short declarative sentence stating the fact itself (never "the speaker says that…"),
in the SAME LANGUAGE as the transcript (e.g. Portuguese stays Portuguese).
Every claim must be SELF-CONTAINED: resolve pronouns and vague references ("ele", "esse governo",
"o governo mencionado", "naquela época", "esse número") to the concrete person, government, period or
figure using the context. If the reference cannot be resolved from the context, do NOT extract that claim.
A claim that merely restates, narrows or extends one in RECENT CLAIMS (same fact, different wording,
"part of", "more than") is a duplicate — skip it. Only extract a second claim when it is about a
genuinely different fact.
Respond with JSON: {"claims": ["...", "..."]}"""

VERIFY_SYSTEM = """You are a rigorous, neutral fact-checker working live during a political debate.
Verify whether the CLAIM is TRUE ABOUT THE REAL WORLD using web search. The transcript context is only
there to disambiguate the claim; the fact that someone said it is NOT evidence. Never answer "valid"
merely because the transcript contains the statement.
Prefer primary sources (official statistics, government, courts, academic work, established fact-checkers
and major news outlets). Classify as:
- "fake": the claim is false or substantially misleading according to reliable evidence.
- "valid": the claim is accurate or essentially accurate according to reliable evidence.
- "inconclusive": evidence is insufficient, contradictory, outdated, the claim is a promise/opinion,
  or it is too vague to verify.
Relative time expressions ("faltam sete dias", "ontem", "este ano") must be interpreted against TODAY,
given in the input. Be fast. "explanation" is 1-2 plain sentences in the SAME LANGUAGE as the claim, without links or
citations (sources go in the "sources" array). Respond ONLY with JSON:
{"verdict": "fake|valid|inconclusive", "confidence": 0.0-1.0, "explanation": "...",
 "sources": [{"title": "...", "url": "https://..."}]}"""


DEEP_SYSTEM = """You are a senior fact-checker doing a SECOND, DEEPER pass on a claim from a political debate
that a fast first pass left inconclusive. Work carefully:
1. Restate the claim precisely: who, what figure/event, which period. Use the transcript context only to
   disambiguate; the fact that someone said it is NOT evidence.
2. Run at least 3 different web searches: (a) in Portuguese with the key entities and figure; (b) targeting
   fact-checkers and official sources (lupa.uol.com.br, aosfatos.org, g1.globo.com/fato-ou-fake,
   projetocomprova.com.br, gov.br, ibge.gov.br, tse.jus.br, camara.leg.br, senado.leg.br, stf.jus.br,
   bcb.gov.br); (c) alternative wording, synonyms or the underlying statistic.
3. Weigh the results. Answer "fake" or "valid" only with at least two independent reliable sources that
   address the specific fact; otherwise "inconclusive" and say in the explanation exactly what evidence
   would settle it (e.g. which official series or document).
Interpret relative time expressions against TODAY. "explanation" is 2-3 plain sentences in the SAME
LANGUAGE as the claim, no links or citations (sources go in the array). Respond ONLY with JSON:
{"verdict": "fake|valid|inconclusive", "confidence": 0.0-1.0, "explanation": "...",
 "sources": [{"title": "...", "url": "https://..."}]}"""


VERIFY_DB_SYSTEM = """You are a rigorous, neutral fact-checker working live during a political debate.
Decide whether the CLAIM is TRUE ABOUT THE REAL WORLD using ONLY the numbered EVIDENCE excerpts
(from trusted news outlets, fact-checkers and official sources). The transcript context only
disambiguates the claim; the fact that someone said it is NOT evidence.
Classify as:
- "fake": an excerpt DIRECTLY contradicts the specific fact claimed (the number, date, person, event).
  Absence of a fact from the evidence is NOT contradiction — never answer "fake" because the
  excerpts do not mention the figure or event.
- "valid": an excerpt directly confirms the specific fact claimed.
- "inconclusive": the evidence does not settle it (off-topic, partial, outdated, only loosely related,
  or the claim is a promise/opinion/too vague).
When in doubt between fake/valid and inconclusive, choose inconclusive with "sufficient": false so a
deeper check can run. Interpret relative time expressions against TODAY, given in the input.
Set "sufficient": true only when the evidence genuinely supports your fake/valid decision; otherwise
false (a web search will then be performed). "used" lists the evidence numbers you relied on.
"explanation" is 1-2 plain sentences in the SAME LANGUAGE as the claim, no links. Respond ONLY with JSON:
{"verdict": "fake|valid|inconclusive", "confidence": 0.0-1.0, "sufficient": true|false,
 "used": [1, 3], "explanation": "..."}"""


CLAIMS_SCHEMA = {
    "type": "json_schema",
    "name": "claims",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {"claims": {"type": "array", "items": {"type": "string"}}},
        "required": ["claims"],
        "additionalProperties": False,
    },
}


_MD_LINK = re.compile(r"\s*\(?\[([^\]]*)\]\((https?://[^)\s]+)\)\)?")
_BARE_URL = re.compile(r"\s*\(?https?://\S+\)?")


def _clean_explanation(text: str) -> str:
    text = _MD_LINK.sub("", text)
    text = _BARE_URL.sub("", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _add_source(sources: list[Source], title: str, url: str, limit: int = 4) -> None:
    if not url.startswith(("http://", "https://")) or len(sources) >= limit:
        return
    key = url.split("?utm_source=")[0].rstrip("/")
    if any(src.url.split("?utm_source=")[0].rstrip("/") == key for src in sources):
        return
    sources.append(Source(title=title.strip(), url=key))


def _today() -> str:
    return time.strftime("%Y-%m-%d (%A)", time.gmtime())


def _parse_verdict(data: dict) -> tuple[Verdict, float]:
    verdict_raw = str(data.get("verdict", "inconclusive")).lower()
    verdict = Verdict(verdict_raw) if verdict_raw in {v.value for v in Verdict} else Verdict.INCONCLUSIVE
    if verdict == Verdict.PENDING:
        verdict = Verdict.INCONCLUSIVE
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.5))))
    except (TypeError, ValueError):
        confidence = 0.5
    return verdict, confidence


def _parse_json(text: str) -> dict:
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        text = m.group(0)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


class OpenAIFactChecker:
    def __init__(self, client: AsyncOpenAI, model: str, store: NewsStore | None = None,
                 min_similarity: float = 0.55, deep_model: str | None = None):
        self.client = client
        self.model = model
        self.deep_model = deep_model or model
        self.jev: JevDecider | None = None
        self.store = store
        self.min_similarity = min_similarity
        self._bg: set[asyncio.Task] = set()
        self._recent_vecs: deque[np.ndarray] = deque(maxlen=RECENT_VEC_WINDOW)

    async def extract_claims(self, context: str, latest: str, recent_claims: list[str]) -> list[str]:
        if not latest.strip():
            return []
        user = (
            f"TODAY: {_today()}\n"
            f"CONTEXT (previous segments):\n{context or '(none)'}\n\n"
            f"LATEST SEGMENT:\n{latest}\n\n"
            f"RECENT CLAIMS (already extracted):\n" + ("\n".join(f"- {c}" for c in recent_claims) or "(none)")
        )
        res = await self.client.responses.create(
            model=self.model,
            instructions=EXTRACT_SYSTEM,
            input=user,
            text={"format": CLAIMS_SCHEMA},
            temperature=0,
        )
        data = _parse_json(res.output_text)
        claims = [c.strip() for c in data.get("claims", []) if isinstance(c, str) and c.strip()]
        claims = await self._gate(claims[:2], f"{context} {latest}")
        return await self._drop_duplicates(claims)

    async def _gate(self, claims: list[str], context: str) -> list[str]:
        """Jev claim gate: drop candidates that are not checkable facts (promises, opinions...)."""
        if not claims or self.jev is None:
            return claims
        try:
            probs = await self.jev.checkable(claims, context)
        except Exception:  # noqa: BLE001
            log.exception("jev gate failed")
            return claims
        kept = []
        for c, p in zip(claims, probs):
            if p < settings.claim_gate_min:
                log.info("gate[jev]: dropped (%.2f) %s", p, c[:80])
            else:
                kept.append(c)
        return kept

    async def _drop_duplicates(self, claims: list[str]) -> list[str]:
        """Semantic dedup: skip claims whose embedding is near a recently emitted one
        (the extractor sometimes re-emits the same fact across consecutive chunks)."""
        if not claims or self.store is None:
            return claims
        try:
            vecs = await self.store.embed(claims)
        except Exception:  # noqa: BLE001
            log.exception("dedup embedding failed")
            return claims
        kept: list[str] = []
        for text, v in zip(claims, vecs):
            sim = max((float(v @ r) for r in self._recent_vecs), default=0.0)
            if sim >= DUPLICATE_SIMILARITY:
                log.info("dedup: dropped near-duplicate (%.2f) %s", sim, text[:80])
                continue
            kept.append(text)
            self._recent_vecs.append(v)
        return kept

    async def verify(self, claim: str, context: str) -> VerifyResult:
        """First level: local news DB. Second level (fallback): live web search + write-back."""
        t0 = time.monotonic()
        if self.store is not None:
            try:
                evidence = [e for e in await self.store.search(claim, k=5) if e.score >= self.min_similarity]
            except Exception:  # noqa: BLE001
                log.exception("news DB search failed")
                evidence = []
            if evidence:
                result = await self._verify_with_evidence(claim, context, evidence)
                if result is not None:
                    log.info("verify[db] %.1fs top=%.2f %s -> %s", time.monotonic() - t0,
                             evidence[0].score, claim[:60], result.verdict.value)
                    return result
        result = await self._verify_with_web(claim, context)
        log.info("verify[web] %.1fs %s -> %s", time.monotonic() - t0, claim[:60], result.verdict.value)
        self._write_back(claim, result)
        return result

    async def deep_verify(self, claim: str, context: str) -> VerifyResult:
        """Third level: slower, stronger pass for claims left inconclusive (or a manual re-check)."""
        t0 = time.monotonic()
        result = await self._verify_with_web(claim, context, model=self.deep_model, system=DEEP_SYSTEM,
                                             context_size="high", method="deep")
        log.info("verify[deep] %.1fs %s -> %s", time.monotonic() - t0, claim[:60], result.verdict.value)
        self._write_back(claim, result)
        return result

    def _write_back(self, claim: str, result: VerifyResult) -> None:
        if self.store is None or result.verdict == Verdict.INCONCLUSIVE:
            return
        summary = f"{claim}\n\nVeredito da busca na web: {result.verdict.value} — {result.explanation}"
        for src in result.sources:
            self._spawn(self.store.add_url(src.url, src.title, source=urlparse(src.url).netloc,
                                           fallback_text=summary))

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def _verify_with_evidence(self, claim: str, context: str, evidence: list[Evidence]) -> VerifyResult | None:
        blocks = []
        for i, e in enumerate(evidence, 1):
            when = time.strftime("%Y-%m-%d", time.gmtime(e.published)) if e.published else "date unknown"
            flag = " [unverified summary from an earlier web search — weak evidence]" if e.kind == "web-summary" else ""
            blocks.append(f"[{i}] {e.source} — {e.title} ({when}){flag}\n{e.text[:1200]}")
        if self.jev is not None:
            jev_result, skip_llm = await self._decide_with_jev(claim, evidence, blocks)
            if jev_result is not None:
                return jev_result
            if skip_llm:
                return None  # Jev is confident the DB evidence cannot settle it -> straight to web
        user = (f"TODAY: {_today()}\nCLAIM: {claim}\n\nTRANSCRIPT CONTEXT: {context[-600:]}\n\n"
                f"EVIDENCE:\n\n" + "\n\n".join(blocks))
        res = await self.client.responses.create(
            model=self.model, instructions=VERIFY_DB_SYSTEM, input=user, temperature=0)
        data = _parse_json(res.output_text)
        verdict, confidence = _parse_verdict(data)
        if verdict == Verdict.INCONCLUSIVE or not data.get("sufficient", True):
            return None
        used = [int(i) for i in data.get("used", []) if str(i).isdigit() and 1 <= int(i) <= len(evidence)]
        picked = [evidence[i - 1] for i in used] or evidence[:2]
        sources: list[Source] = []
        for e in picked:
            _add_source(sources, e.title or e.source, e.url)
        return VerifyResult(verdict, confidence, _clean_explanation(str(data.get("explanation", ""))), sources, "db")

    async def _decide_with_jev(self, claim: str, evidence: list[Evidence], blocks: list[str]) -> tuple[VerifyResult | None, bool]:
        """Jev decides from the evidence in ~0.3 s. Returns (result, skip_llm): result is None when
        Jev is not confident; skip_llm=True when it is confident the evidence is inconclusive, so the
        slower LLM pass over the same evidence is pointless and we go straight to web search."""
        try:
            d = await self.jev.decide(claim, blocks, _today())
        except Exception:  # noqa: BLE001
            log.exception("jev decide failed; falling back to OpenAI")
            return None, False
        log.info("decide[jev] %dms %s p=%.2f suff=%.2f conf=%.2f %s", d.latency_ms, d.verdict,
                 d.probabilities.get(d.verdict, 0), d.sufficient, d.confidence, claim[:60])
        if d.verdict == "inconclusive":
            return None, d.probabilities.get("inconclusive", 0) >= 0.6
        if d.sufficient < 0.45 or d.probabilities.get(d.verdict, 0) < 0.7:
            return None, False
        verdict = Verdict(d.verdict)
        picked = evidence[:3]
        sources: list[Source] = []
        for e in picked:
            _add_source(sources, e.title or e.source, e.url)
        titles = "; ".join(f"{e.source}: {e.title}" for e in picked if e.title)
        word = "confirmada" if verdict == Verdict.VALID else "contradita"
        explanation = f"Afirmação {word} pelas evidências da base de notícias ({titles})."
        return VerifyResult(verdict, round(d.probabilities.get(d.verdict, 0), 2), explanation, sources, "db"), False

    async def _verify_with_web(self, claim: str, context: str, *, model: str | None = None,
                               system: str = VERIFY_SYSTEM, context_size: str = "low",
                               method: str = "web") -> VerifyResult:
        user = f"TODAY: {_today()}\nCLAIM: {claim}\n\nTRANSCRIPT CONTEXT: {context[-800:]}"
        res = await self.client.responses.create(
            model=model or self.model,
            instructions=system,
            input=user,
            tools=[{"type": "web_search_preview", "search_context_size": context_size}],
            temperature=0,
        )
        data = _parse_json(res.output_text)
        verdict, confidence = _parse_verdict(data)
        sources: list[Source] = []
        for item in data.get("sources", []) or []:
            if isinstance(item, dict) and item.get("url"):
                _add_source(sources, str(item.get("title", "")), str(item["url"]))
        # Also harvest citations attached by the web search tool.
        for item in getattr(res, "output", []) or []:
            for content in getattr(item, "content", []) or []:
                for ann in getattr(content, "annotations", []) or []:
                    url = getattr(ann, "url", None)
                    if url:
                        _add_source(sources, getattr(ann, "title", "") or "", url)
        return VerifyResult(verdict, confidence, _clean_explanation(str(data.get("explanation", ""))), sources, method)


class MockFactChecker:
    async def extract_claims(self, context: str, latest: str, recent_claims: list[str]) -> list[str]:
        await asyncio.sleep(0.5)
        if "Boa noite" in latest or not latest.strip():
            return []
        return [latest.strip()]

    async def deep_verify(self, claim: str, context: str) -> VerifyResult:
        r = await self.verify(claim, context)
        r.method = "deep"
        return r

    async def verify(self, claim: str, context: str) -> VerifyResult:
        await asyncio.sleep(random.uniform(2, 5))
        verdict = random.choice([Verdict.FAKE, Verdict.VALID, Verdict.INCONCLUSIVE])
        explanations = {
            Verdict.FAKE: "Dados oficiais contradizem a afirmação.",
            Verdict.VALID: "Confirmado por fontes oficiais.",
            Verdict.INCONCLUSIVE: "Não há dados suficientes para confirmar ou refutar.",
        }
        return VerifyResult(
            verdict,
            round(random.uniform(0.55, 0.95), 2),
            explanations[verdict],
            [Source(title="Fonte de exemplo", url="https://example.com/fact-check")],
            random.choice(["db", "web"]),
        )


def make_factchecker(client: AsyncOpenAI | None, store: NewsStore | None = None) -> FactChecker:
    if settings.provider == "openai" and client:
        checker = OpenAIFactChecker(client, settings.factcheck_model, store, settings.news_min_similarity, deep_model=settings.deep_model)
        if settings.decider == "jev" and settings.ai_gateway_api_key:
            checker.jev = JevDecider(settings.ai_gateway_api_key, settings.jev_model)
        return checker
    return MockFactChecker()
