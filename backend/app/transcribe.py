import asyncio
import io
import random
from typing import Protocol

from openai import AsyncOpenAI

from .audio import AudioChunk
from .config import settings


class Transcriber(Protocol):
    async def transcribe(self, chunk: AudioChunk, language: str | None) -> str: ...


class OpenAITranscriber:
    def __init__(self, client: AsyncOpenAI, model: str):
        self.client = client
        self.model = model

    async def transcribe(self, chunk: AudioChunk, language: str | None) -> str:
        if _is_silent(chunk.pcm):
            return ""
        f = io.BytesIO(chunk.to_wav())
        f.name = f"chunk-{chunk.index}.wav"
        kwargs = {"model": self.model, "file": f, "response_format": "text"}
        if language:
            kwargs["language"] = language
        res = await self.client.audio.transcriptions.create(**kwargs)
        return (res if isinstance(res, str) else getattr(res, "text", "")).strip()


_MOCK_LINES = [
    "O desemprego no Brasil caiu para 6,2% no último trimestre, o menor da série histórica.",
    "A Amazônia perdeu 40% da sua floresta nos últimos dez anos.",
    "Nós construímos mais de duas mil escolas em todo o país durante o meu governo.",
    "A vacina contra a COVID-19 altera o DNA das pessoas, isso é comprovado.",
    "O salário mínimo hoje compra menos cestas básicas do que em 2010.",
    "A Terra tem aproximadamente 4,5 bilhões de anos.",
    "Boa noite a todos, obrigado por acompanharem este debate.",
    "O Brasil é o maior produtor de café do mundo.",
]


class MockTranscriber:
    async def transcribe(self, chunk: AudioChunk, language: str | None) -> str:
        await asyncio.sleep(0.3)
        return random.choice(_MOCK_LINES)


def _is_silent(pcm: bytes, threshold: int = 300) -> bool:
    import array

    samples = array.array("h", pcm[: len(pcm) - len(pcm) % 2])
    if not samples:
        return True
    step = max(1, len(samples) // 4000)
    peak = max(abs(s) for s in samples[::step])
    return peak < threshold


def make_transcriber(client: AsyncOpenAI | None) -> Transcriber:
    if settings.provider == "openai" and client:
        return OpenAITranscriber(client, settings.transcribe_model)
    return MockTranscriber()
