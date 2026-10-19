"""Orchestrates audio -> transcript -> claims -> verdicts for one viewer session.

Everything downstream of the audio reader runs as independent tasks so a slow
web search never delays the next transcription.
"""
import asyncio
import logging
import re
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable

from . import audio, usage
from .config import settings
from .glossary import glossary as _glossary
from .factcheck import FactChecker, VerifyResult
from .models import Claim, StreamInfo, TranscriptSegment, Verdict
from .transcribe import Transcriber

log = logging.getLogger(__name__)

Emit = Callable[[dict], Awaitable[None]]

MAX_CONCURRENT_VERIFICATIONS = 6
CONTEXT_SEGMENTS = 3
SAVE_CONTEXT_SEGMENTS = 2
SAVE_MIN_WORDS = 12  # save mode: don't run extraction on chunks shorter than this
MAX_CONCURRENT_TRANSCRIPTIONS = 6
RECENT_CLAIMS = 12
ENTITY_SEGMENTS = 8  # ~64 s of transcript used to resolve 'ele', 'o cara', 'você'


_NAME = re.compile(r"\b(?:[A-ZÁÉÍÓÚÂÊÔÃÕÇ][\wáéíóúâêôãõç]+)(?:\s+(?:d[aeo]s?\s+)?[A-ZÁÉÍÓÚÂÊÔÃÕÇ][\wáéíóúâêôãõç]+)*")
_STOP = {"Agora", "Então", "Mas", "Porque", "Não", "Sim", "Olha", "Ou", "Eu", "Você", "Nós", "Ele", "Ela", "Isso", "Esse", "Essa",
         "Este", "Esta", "Aqui", "Quando", "Como", "Que", "Quem", "Para", "Por", "Com", "Sem", "Hoje", "Ontem", "Brasil", "Presidente"}


def _entities(text: str) -> list[str]:
    """Proper names mentioned recently (transcript + glossary hits), most recent last, deduplicated."""
    glossary = _glossary.names()
    found: list[str] = []
    low = text.lower()
    for g in glossary:
        if len(g) > 2 and g.lower() in low:
            found.append(g)
    for m in _NAME.finditer(text):
        name = m.group(0).strip()
        if name in _STOP or name.split()[0] in _STOP or len(name) < 4:
            continue
        found.append(name)
    seen: dict[str, None] = {}
    for n in found:
        seen.pop(n, None)
        seen[n] = None
    return list(seen)[-12:]


class Pipeline:
    def __init__(self, transcriber: Transcriber, checker: FactChecker, emit: Emit, save: bool = False):
        self.save = save  # fewer AI calls, same experience
        self.usage = usage.Usage()
        self._claim_times: deque[float] = deque(maxlen=64)
        self.transcriber = transcriber
        self.checker = checker
        self.emit = emit
        self.tasks: set[asyncio.Task] = set()
        self.verify_tasks: set[asyncio.Task] = set()
        self.context: deque[str] = deque(maxlen=SAVE_CONTEXT_SEGMENTS if save else CONTEXT_SEGMENTS)
        self.recent_claims: deque[str] = deque(maxlen=RECENT_CLAIMS)
        self.recent_text: deque[str] = deque(maxlen=ENTITY_SEGMENTS)
        self.verify_sem = asyncio.Semaphore(MAX_CONCURRENT_VERIFICATIONS)
        self.transcribe_sem = asyncio.Semaphore(MAX_CONCURRENT_TRANSCRIPTIONS)
        self._stopped = False
        self.run_id = uuid.uuid4().hex[:6]
        self.claims: dict[str, tuple[Claim, str]] = {}

    def _spawn(self, coro, *, verification: bool = False) -> None:
        t = asyncio.create_task(coro)
        bucket = self.verify_tasks if verification else self.tasks
        bucket.add(t)
        t.add_done_callback(bucket.discard)

    async def run(self, url: str, language: str | None, start_seconds: float, realtime: bool = True) -> None:
        usage.current.set(self.usage)  # child tasks inherit the context -> all calls of this run are metered
        await self.emit({"type": "status", "state": "resolving", "message": "Abrindo o vídeo…"})
        if settings.provider == "mock" and not url.strip():
            info = StreamInfo(video_id="", title="Mock stream", is_live=True)
        else:
            info, media_url = await audio.resolve_stream(url)
        await self.emit({"type": "info", "info": info.model_dump()})
        await self.emit({"type": "status", "state": "listening", "message": "Ouvindo…"})

        if settings.provider == "mock" and not url.strip():
            chunks = self._mock_chunks()
        else:
            chunks = audio.iter_chunks(media_url, settings.chunk_seconds, info.is_live, start_seconds, realtime)

        async for chunk in chunks:
            if self._stopped:
                break
            # backpressure: never hold more than MAX_CONCURRENT_TRANSCRIPTIONS chunks in flight
            await self.transcribe_sem.acquire()
            self._spawn(self._handle_chunk(chunk, language))
        await self.emit({"type": "status", "state": "ended", "message": "Transmissão encerrada"})

    async def _mock_chunks(self):
        i = 0
        while not self._stopped:
            await asyncio.sleep(settings.chunk_seconds / 2)
            s = i * settings.chunk_seconds
            yield audio.AudioChunk(i, s, s + settings.chunk_seconds, b"")
            i += 1

    async def _handle_chunk(self, chunk: audio.AudioChunk, language: str | None) -> None:
        try:
            text = await self.transcriber.transcribe(chunk, language)
        except Exception as e:  # noqa: BLE001
            log.exception("transcription failed")
            await self.emit({"type": "error", "message": f"Falha na transcrição: {e}"})
            return
        finally:
            self.transcribe_sem.release()
        if not text:
            return
        seg = TranscriptSegment(id=f"seg-{self.run_id}-{chunk.index}", text=text, start=chunk.start, end=chunk.end,
                                captured_at=chunk.captured_at or None, transcribed_at=time.time())
        log.info("transcript[%s] %.0fs: %s", self.run_id, chunk.start, text)
        await self.emit({"type": "transcript", "segment": seg.model_dump()})

        context = " ".join(self.context)
        entities = _entities(" ".join(self.recent_text))
        self.context.append(text)
        self.recent_text.append(text)
        if self.save and len(text.split()) < SAVE_MIN_WORDS:
            return
        try:
            claims = await self.checker.extract_claims(context, text, list(self.recent_claims), strict=self.save,
                                                       entities=entities)
        except Exception as e:  # noqa: BLE001
            log.exception("claim extraction failed")
            await self.emit({"type": "error", "message": f"Falha ao extrair afirmações: {e}"})
            return
        log.info("claims[%s] %.0fs: %s", self.run_id, chunk.start, claims)
        for claim_text in claims:
            if self.save:
                now = time.time()
                if sum(1 for t in self._claim_times if now - t < 60) >= settings.save_max_claims_per_min:
                    log.info("save mode: claim cap reached, skipping %s", claim_text[:60])
                    continue
                self._claim_times.append(now)
            self.recent_claims.append(claim_text)
            claim = Claim(
                id=uuid.uuid4().hex[:10],
                text=claim_text,
                verdict=Verdict.PENDING,
                confidence=0,
                start=chunk.start,
                end=chunk.end,
                captured_at=chunk.captured_at or None,
                extracted_at=time.time(),
            )
            self.claims[claim.id] = (claim, f"{context} {text}")
            await self.emit({"type": "claim", "claim": claim.model_dump()})
            self._spawn(self._verify(claim, f"{context} {text}"), verification=True)

    async def _verify(self, claim: Claim, context: str) -> None:
        async with self.verify_sem:
            try:
                r = await self.checker.verify(claim.text, context)
            except Exception as e:  # noqa: BLE001
                log.exception("verification failed")
                r = VerifyResult(Verdict.INCONCLUSIVE, 0.0, f"Falha na verificação: {e}")
        self._apply(claim, r)
        claim.verified_at = claim.verified_at or time.time()  # first settled verdict; deep/re-check keep it
        if r.verdict == Verdict.INCONCLUSIVE and settings.deep_check and not self.save:  # save mode: deep only via Re-check
            claim.deep_pending = True
            self._spawn(self._deep(claim, context), verification=True)
        await self.emit({"type": "claim", "claim": claim.model_dump()})

    async def _deep(self, claim: Claim, context: str) -> None:
        async with self.verify_sem:
            try:
                r = await self.checker.deep_verify(claim.text, context)
            except Exception as e:  # noqa: BLE001
                log.exception("deep verification failed")
                r = VerifyResult(claim.verdict, claim.confidence, claim.explanation, claim.sources, claim.method or "web")
        self._apply(claim, r)
        claim.verified_at = claim.verified_at or time.time()  # first settled verdict; deep/re-check keep it
        claim.deep_pending = False
        await self.emit({"type": "claim", "claim": claim.model_dump()})

    async def recheck(self, claim_id: str) -> None:
        """Manual re-check from the UI: run the deep pass on an already-settled claim."""
        entry = self.claims.get(claim_id)
        if not entry:
            await self.emit({"type": "error", "message": "Afirmação não encontrada"})
            return
        claim, context = entry
        if claim.deep_pending or claim.verdict == Verdict.PENDING:
            return
        claim.deep_pending = True
        await self.emit({"type": "claim", "claim": claim.model_dump()})
        self._spawn(self._deep(claim, context), verification=True)

    @staticmethod
    def _apply(claim: Claim, r: VerifyResult) -> None:
        claim.verdict, claim.confidence, claim.explanation, claim.sources, claim.method = (
            r.verdict, r.confidence, r.explanation, r.sources, r.method)  # type: ignore[assignment]

    async def stop(self, keep_verifications: bool = False) -> None:
        """Stop ingestion. With keep_verifications, in-flight verdicts still get
        delivered (used on re-sync so pending cards never stay 'Checking…')."""
        self._stopped = True
        doomed = list(self.tasks) + ([] if keep_verifications else list(self.verify_tasks))
        for t in doomed:
            t.cancel()
        if doomed:
            await asyncio.gather(*doomed, return_exceptions=True)
