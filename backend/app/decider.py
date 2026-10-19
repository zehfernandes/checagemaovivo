"""Jev (TypeSafe AI "System One" decision model) via Vercel AI Gateway.

Jev does not generate text: it answers typed questions (choice / boolean) about a
state with calibrated probabilities in ~300 ms. We use it for the two pure *decisions*
in the pipeline — "is this a checkable claim?" and "given this evidence, is the claim
fake/valid/inconclusive?" — and keep OpenAI for anything that needs search or prose.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

GATEWAY_URL = "https://ai-gateway.vercel.sh/v4/ai/decision-model"

VERDICT_CRITERIA = {
    "valid": "The evidence directly confirms the specific fact claimed (same entity, figure, date, event).",
    "fake": "The evidence directly contradicts the specific fact claimed. Absence of a fact from the evidence is NOT a contradiction.",
    "inconclusive": "The evidence does not settle it: missing, partial, off-topic, outdated, or the claim is a promise/opinion/too vague.",
}


@dataclass
class Decision:
    verdict: str
    probabilities: dict[str, float]
    confidence: float
    sufficient: float
    latency_ms: int
    relevant: float = 1.0  # evidence is about the same specific subject as the claim


class JevDecider:
    def __init__(self, api_key: str, model: str = "typesafe-ai/jev", timeout: float = 10.0):
        self.model = model
        self.client = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "ai-gateway-protocol-version": "0.0.1",
                "ai-gateway-auth-method": "api-key",
                "ai-decision-model-specification-version": "4",
                "ai-model-id": model,
            },
        )

    async def _ask(self, state: str, questions: dict) -> dict:
        r = await self.client.post(GATEWAY_URL, json={"stateParts": [{"type": "text", "text": state}], "questions": questions})
        r.raise_for_status()
        return r.json()

    async def decide(self, claim: str, evidence_blocks: list[str], today: str) -> Decision:
        import time
        t0 = time.monotonic()
        state = f"TODAY: {today}\nCLAIM: {claim}\n\nEVIDENCE (from news outlets, fact-checkers, official sources):\n\n" + "\n\n".join(evidence_blocks)
        data = await self._ask(state, {
            "verdict": {"type": "choice",
                        "instructions": "Using ONLY the EVIDENCE, is the CLAIM true about the real world? The fact that someone said it is not evidence.",
                        "criteria": VERDICT_CRITERIA},
            "sufficient": {"type": "boolean",
                           "instructions": "The EVIDENCE is specific and reliable enough to decide the CLAIM as fake or valid without further research."},
            "relevant": {"type": "boolean",
                         "instructions": "The EVIDENCE is about the SAME specific subject as the CLAIM (same people, place, event or figure) — not merely a similar topic."},
        })
        a = data["answers"]
        conf = (data.get("providerMetadata", {}).get("typesafe", {}).get("confidence", {}) or {}).get("verdict", 0.0)
        return Decision(a["verdict"]["choice"], a["verdict"].get("probabilities", {}), float(conf),
                        float(a["sufficient"]["probability"]), int((time.monotonic() - t0) * 1000),
                        float(a.get("relevant", {}).get("probability", 1.0)))

    async def checkable(self, claims: list[str], context: str) -> list[float]:
        """Probability that each claim is a verifiable factual statement (fan-out: one request)."""
        if not claims:
            return []
        state = f"TRANSCRIPT CONTEXT: {context[-800:]}\n\nCANDIDATE CLAIMS:\n" + "\n".join(f"[{i}] {c}" for i, c in enumerate(claims))
        questions = {f"c{i}": {"type": "boolean",
                               "instructions": f"Claim [{i}] is a concrete, verifiable factual statement about the real world "
                                               f"(a number, date, past event, law, or what someone did) — NOT a promise, intention, "
                                               f"opinion, insult, question, hypothetical or vague generality. It must be of public interest "
                                               f"(public figures, institutions, statistics, laws, public events) — not a private person's personal story."}
                     for i in range(len(claims))}
        data = await self._ask(state, questions)
        return [float(data["answers"][f"c{i}"]["probability"]) for i in range(len(claims))]
