import asyncio
import base64
import io
import logging
import re
import random
from collections import deque
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Protocol

from openai import AsyncOpenAI

from .audio import BYTES_PER_SAMPLE, AudioChunk
from . import usage
from .config import settings
from .glossary import glossary

log = logging.getLogger(__name__)


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
        if glossary.prompt():
            kwargs["prompt"] = glossary.prompt()
        res = await self.client.audio.transcriptions.create(**kwargs)
        usage.track_audio(chunk.end - chunk.start)
        text = (res if isinstance(res, str) else getattr(res, "text", "")).strip()
        if _echoes_prompt(text, kwargs.get("prompt", "")):
            log.info("transcript dropped: echoes the glossary prompt (music/noise chunk): %s", text[:80])
            return ""
        return glossary.apply(text)


RT_RATE = 24000  # the Realtime API only takes 24 kHz PCM
RT_FRAME_SECONDS = 0.1


class RealtimeTranscriber(OpenAITranscriber):
    """Streams the audio to an OpenAI Realtime transcription session. Server VAD ends a segment at a
    pause, so sentences aren't cut in half and the text arrives ~0.5 s after the cut. Broadcast audio
    (music beds, a candidate who never pauses) can keep VAD "speaking" for good, so a segment is also
    committed by hand after REALTIME_MAX_SEGMENT_S. `transcribe` (inherited) stays for unpaced
    VOD_FULL_SPEED runs."""

    async def segments(self, frames: AsyncIterator[AudioChunk], language: str | None) -> AsyncIterator[tuple[AudioChunk, str]]:
        """Yield (chunk with the segment's media span and capture time, text) per spoken segment.
        Sessions have a max duration: on close it reconnects and resends the audio read meanwhile;
        the segment being spoken at that moment is lost."""
        conn = None
        backlog: list[bytes] = []  # audio read while (re)connecting
        sent = 0.0  # seconds appended to sessions so far
        origin: float | None = None  # media time of the first frame
        last_cap = 0.0  # wall clock of the newest frame
        speaking, seg_start = False, 0.0  # VAD state; seg_start = where the uncommitted speech began
        cuts: deque[list[float]] = deque()  # [start, end] of manual commits awaiting their item_id
        ended = asyncio.Event()

        async def send(pcm: bytes) -> None:
            nonlocal sent
            await conn.input_audio_buffer.append(audio=base64.b64encode(pcm).decode())
            sent += len(pcm) / (RT_RATE * BYTES_PER_SAMPLE)

        async def pump() -> None:
            nonlocal origin, last_cap, seg_start
            async for f in frames:
                origin = f.start if origin is None else origin
                last_cap = f.captured_at
                if conn is None:
                    backlog.append(f.pcm)
                    continue
                try:
                    await send(f.pcm)
                    if speaking and sent - seg_start >= settings.realtime_max_segment_s:
                        cuts.append([seg_start, sent])
                        seg_start = sent
                        await conn.input_audio_buffer.commit()
                except Exception:  # noqa: BLE001 - session closing; resent on the next one
                    backlog.append(f.pcm)

        async def finish(c, items: dict) -> None:
            await ended.wait()
            with suppress(Exception):
                await c.input_audio_buffer.commit()  # trailing speech with no pause after it
            for _ in range(100):  # up to 10 s for the last transcripts
                await asyncio.sleep(0.1)
                if not items:
                    break
            await c.close()

        pump_task = asyncio.create_task(pump())
        pump_task.add_done_callback(lambda _: ended.set())
        n, fails = 0, 0
        try:
            while not ended.is_set() or backlog:
                items: dict[str, list] = {}  # item_id -> [start, end] in seconds since the first frame
                try:
                    async with self.client.realtime.connect(extra_query={"intent": "transcription"}) as c:
                        await c.session.update(session=self._session(language))
                        offset = seg_start = sent
                        speaking = False
                        cuts.clear()
                        conn = c
                        while backlog:
                            await send(backlog.pop(0))
                        closer = asyncio.create_task(finish(c, items))
                        try:
                            async for ev in c:
                                if ev.type == "input_audio_buffer.speech_started":
                                    speaking, seg_start = True, offset + ev.audio_start_ms / 1000
                                    items[ev.item_id] = [seg_start, None]
                                elif ev.type == "input_audio_buffer.speech_stopped":
                                    speaking = False
                                    items.setdefault(ev.item_id, [seg_start, None])[1] = offset + ev.audio_end_ms / 1000
                                elif ev.type == "input_audio_buffer.committed":
                                    if cuts and items.get(ev.item_id, [0, None])[1] is None:
                                        items[ev.item_id] = cuts.popleft()
                                    items.setdefault(ev.item_id, [seg_start, None])
                                elif ev.type == "conversation.item.input_audio_transcription.completed":
                                    start, end = items.pop(ev.item_id, (seg_start, None))
                                    end = end or sent
                                    fails = 0
                                    usage.track_audio(end - start)
                                    text = self._clean(ev.transcript)
                                    if text:
                                        o = origin or 0.0
                                        yield AudioChunk(n, o + start, o + end, b"", last_cap - (sent - end)), text
                                        n += 1
                                elif ev.type == "conversation.item.input_audio_transcription.failed":
                                    items.pop(ev.item_id, None)
                                    log.warning("realtime transcription failed: %s", ev.error)
                                elif ev.type == "error":
                                    log.warning("realtime error: %s", ev.error.message)
                        finally:
                            conn = None
                            closer.cancel()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    fails += 1
                    if fails > 5:
                        raise
                    log.warning("realtime session dropped (%s), reconnecting", e)
                    await asyncio.sleep(min(2 ** fails, 10))
                else:
                    if not ended.is_set():
                        log.info("realtime session closed at %.0fs of audio, reconnecting", sent)
        finally:
            pump_task.cancel()
            await asyncio.gather(pump_task, return_exceptions=True)

    def _session(self, language: str | None) -> dict:
        transcription = {"model": self.model}
        if language:
            transcription["language"] = language
        if glossary.prompt():
            transcription["prompt"] = glossary.prompt()
        return {"type": "transcription", "audio": {"input": {
            "format": {"type": "audio/pcm", "rate": RT_RATE},
            "transcription": transcription,
            "turn_detection": {"type": "server_vad", "silence_duration_ms": settings.realtime_silence_ms},
        }}}

    @staticmethod
    def _clean(text: str) -> str:
        text = (text or "").strip()
        if text and not re.search(r"[A-Za-zÀ-ÿ]", text):  # noise segments come back as "ます。" and the like
            log.info("transcript dropped: no latin letters (noise): %s", text[:40])
            return ""
        if _echoes_prompt(text, glossary.prompt()):
            log.info("transcript dropped: echoes the glossary prompt (music/noise): %s", text[:80])
            return ""
        return glossary.apply(text)


def _echoes_prompt(text: str, prompt: str, threshold: float = 0.6) -> bool:
    """On music/noise chunks the transcription model sometimes returns the glossary prompt itself
    instead of speech; drop a transcript whose words mostly come from the prompt."""
    if not prompt or not text:
        return False
    words = [w for w in re.findall(r"\w+", text.lower()) if len(w) > 2]
    if len(words) < 4:
        return False
    vocab = set(re.findall(r"\w+", prompt.lower()))
    return sum(w in vocab for w in words) / len(words) >= threshold


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
        cls = RealtimeTranscriber if settings.transcribe_mode == "realtime" else OpenAITranscriber
        return cls(client, settings.transcribe_model)
    return MockTranscriber()
