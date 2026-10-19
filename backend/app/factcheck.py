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
You receive a rolling transcript. Extract ONLY from the LATEST segment the factual, checkable claims
(statistics, dates, events, attributions, cause-effect statements about the real world).
Skip opinions, promises, questions, greetings, rhetoric, and anything already present in RECENT CLAIMS.
Rewrite each claim as a short self-contained sentence in the transcript's language, resolving pronouns
with the surrounding context when possible. Return at most 3 claims.
Respond with JSON: {"claims": ["...", "..."]}"""

VERIFY_SYSTEM = """You are a rigorous, neutral fact-checker. Verify the CLAIM using web search.
Prefer primary sources (official statistics, government, academic, established fact-checkers and news outlets).
Classify as:
- "fake": the claim is false or substantially misleading according to reliable evidence.
- "valid": the claim is accurate or essentially accurate.
- "inconclusive": evidence is insufficient, contradictory, outdated, or the claim is too vague to verify.
Be fast and concise. The explanation must be 1-2 sentences in the same language as the claim.
Respond ONLY with JSON:
{"verdict": "fake|valid|inconclusive", "confidence": 0.0-1.0, "explanation": "...",
 "sources": [{"title": "...", "url": "https://..."}]}"""


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
            text={"format": {"type": "json_object"}},
            temperature=0,
        )
        data = _parse_json(res.output_text)
        claims = [c.strip() for c in data.get("claims", []) if isinstance(c, str) and c.strip()]
        return claims[:3]

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
        sources = []
        for s in data.get("sources", [])[:4]:
            if isinstance(s, dict) and s.get("url"):
                sources.append(Source(title=str(s.get("title", "")), url=str(s["url"])))
        # Also harvest citations attached by the web search tool.
        for item in getattr(res, "output", []) or []:
            for content in getattr(item, "content", []) or []:
                for ann in getattr(content, "annotations", []) or []:
                    url = getattr(ann, "url", None)
                    if url and all(src.url != url for src in sources) and len(sources) < 4:
                        sources.append(Source(title=getattr(ann, "title", "") or "", url=url))
        return verdict, confidence, str(data.get("explanation", "")).strip(), sources


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
