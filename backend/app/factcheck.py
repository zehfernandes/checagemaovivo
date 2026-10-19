"""Two-stage fact-checking.

Stage 1 (fast, no tools): extract check-worthy factual claims from the newest
transcript segment. Stage 2 (slower, web search): verify each claim and label it
fake / valid / inconclusive.
"""
import asyncio
import json
import logging
import random
import re
from typing import Protocol

from openai import AsyncOpenAI

from .config import settings
from .models import Source, Verdict

log = logging.getLogger(__name__)


class FactChecker(Protocol):
    async def extract_claims(self, context: str, latest: str, recent_claims: list[str]) -> list[str]: ...
    async def verify(self, claim: str, context: str) -> tuple[Verdict, float, str, list[Source]]: ...


EXTRACT_SYSTEM = """You are a fact-checking assistant watching a live political debate / broadcast.
You receive a rolling transcript. From the LATEST segment only, extract factual claims about the real
world that can be verified against public evidence: statistics, numbers, dates, past events, laws,
what someone did or did not do, comparisons, cause-effect statements.

Do NOT extract: promises or intentions about the future ("vou fazer", "vamos acabar com"), opinions,
values, insults, questions, greetings, procedural talk, hypotheticals, or anything already in RECENT CLAIMS.
If several sentences make the same point, merge them into ONE claim. Return at most 2 claims; usually 0 or 1.

Write each claim as a short declarative sentence stating the fact itself (never "the speaker says that…"),
in the SAME LANGUAGE as the transcript (e.g. Portuguese stays Portuguese). Resolve pronouns using context
(e.g. replace "ele" by the named person when the context makes it clear).
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
Be fast. "explanation" is 1-2 plain sentences in the SAME LANGUAGE as the claim, without links or
citations (sources go in the "sources" array). Respond ONLY with JSON:
{"verdict": "fake|valid|inconclusive", "confidence": 0.0-1.0, "explanation": "...",
 "sources": [{"title": "...", "url": "https://..."}]}"""


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
    def __init__(self, client: AsyncOpenAI, model: str):
        self.client = client
        self.model = model

    async def extract_claims(self, context: str, latest: str, recent_claims: list[str]) -> list[str]:
        if not latest.strip():
            return []
        user = (
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
        return claims[:2]

    async def verify(self, claim: str, context: str) -> tuple[Verdict, float, str, list[Source]]:
        user = f"CLAIM: {claim}\n\nTRANSCRIPT CONTEXT: {context[-800:]}"
        res = await self.client.responses.create(
            model=self.model,
            instructions=VERIFY_SYSTEM,
            input=user,
            tools=[{"type": "web_search_preview", "search_context_size": "low"}],
            temperature=0,
        )
        data = _parse_json(res.output_text)
        verdict_raw = str(data.get("verdict", "inconclusive")).lower()
        verdict = Verdict(verdict_raw) if verdict_raw in {v.value for v in Verdict} else Verdict.INCONCLUSIVE
        if verdict == Verdict.PENDING:
            verdict = Verdict.INCONCLUSIVE
        try:
            confidence = max(0.0, min(1.0, float(data.get("confidence", 0.5))))
        except (TypeError, ValueError):
            confidence = 0.5
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
        return verdict, confidence, _clean_explanation(str(data.get("explanation", ""))), sources


class MockFactChecker:
    async def extract_claims(self, context: str, latest: str, recent_claims: list[str]) -> list[str]:
        await asyncio.sleep(0.5)
        if "Boa noite" in latest or not latest.strip():
            return []
        return [latest.strip()]

    async def verify(self, claim: str, context: str) -> tuple[Verdict, float, str, list[Source]]:
        await asyncio.sleep(random.uniform(2, 5))
        verdict = random.choice([Verdict.FAKE, Verdict.VALID, Verdict.INCONCLUSIVE])
        explanations = {
            Verdict.FAKE: "Dados oficiais contradizem a afirmação.",
            Verdict.VALID: "Confirmado por fontes oficiais.",
            Verdict.INCONCLUSIVE: "Não há dados suficientes para confirmar ou refutar.",
        }
        return (
            verdict,
            round(random.uniform(0.55, 0.95), 2),
            explanations[verdict],
            [Source(title="Fonte de exemplo", url="https://example.com/fact-check")],
        )


def make_factchecker(client: AsyncOpenAI | None) -> FactChecker:
    if settings.provider == "openai" and client:
        return OpenAIFactChecker(client, settings.factcheck_model)
    return MockFactChecker()
