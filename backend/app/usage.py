"""Per-pipeline cost meter: token/search/audio counters with a rough US$ estimate.

The current pipeline's accumulator is held in a ContextVar so the shared OpenAI clients can
attribute each call to the run that made it (asyncio tasks inherit the context)."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field

# USD per 1M tokens (input, output); prefix-matched against the model id
PRICES = {
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1": (2.00, 8.00),
    "text-embedding-3-small": (0.02, 0.0),
}
SEARCH_USD = 0.03  # per web_search tool call (approx.; higher with search_context_size=high)
AUDIO_USD_PER_MIN = 0.003  # gpt-4o-mini-transcribe


@dataclass
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0
    searches: int = 0
    audio_seconds: float = 0.0
    calls: int = 0
    usd: float = 0.0
    by_model: dict[str, int] = field(default_factory=dict)

    def add_tokens(self, model: str, tokens_in: int, tokens_out: int) -> None:
        price = next((p for k, p in PRICES.items() if model.startswith(k)), (0.5, 1.5))
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.calls += 1
        self.by_model[model] = self.by_model.get(model, 0) + 1
        self.usd += (tokens_in * price[0] + tokens_out * price[1]) / 1e6

    def add_searches(self, n: int) -> None:
        self.searches += n
        self.usd += n * SEARCH_USD

    def add_audio(self, seconds: float) -> None:
        self.audio_seconds += seconds
        self.usd += seconds / 60 * AUDIO_USD_PER_MIN

    def summary(self) -> dict:
        return {"tokens_in": self.tokens_in, "tokens_out": self.tokens_out, "searches": self.searches,
                "audio_seconds": round(self.audio_seconds), "calls": self.calls, "usd": round(self.usd, 4)}


current: ContextVar[Usage | None] = ContextVar("usage", default=None)


def track(model: str, res) -> None:
    """Record an OpenAI Responses/embeddings result against the current pipeline (no-op outside one)."""
    u = current.get()
    if u is None:
        return
    usage = getattr(res, "usage", None)
    if usage is not None:
        tin = getattr(usage, "input_tokens", None)
        if tin is None:
            tin = getattr(usage, "prompt_tokens", 0) or getattr(usage, "total_tokens", 0)
        u.add_tokens(model, int(tin or 0), int(getattr(usage, "output_tokens", 0) or 0))
    n = sum(1 for o in (getattr(res, "output", None) or []) if getattr(o, "type", "") == "web_search_call")
    if n:
        u.add_searches(n)


def track_audio(seconds: float) -> None:
    u = current.get()
    if u is not None:
        u.add_audio(seconds)
