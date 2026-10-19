"""Orchestrates audio -> transcript -> claims -> verdicts for one viewer session.

Everything downstream of the audio reader runs as independent tasks so a slow
web search never delays the next transcription.
"""
import asyncio
import logging
import uuid
from collections import deque
from collections.abc import Awaitable, Callable

from . import audio
from .config import settings
from .factcheck import FactChecker, VerifyResult
from .models import Claim, StreamInfo, TranscriptSegment, Verdict
from .transcribe import Transcriber

log = logging.getLogger(__name__)

Emit = Callable[[dict], Awaitable[None]]

MAX_CONCURRENT_VERIFICATIONS = 6
CONTEXT_SEGMENTS = 3
RECENT_CLAIMS = 12


class Pipeline:
    def __init__(self, transcriber: Transcriber, checker: FactChecker, emit: Emit):
        self.transcriber = transcriber
        self.checker = checker
        self.emit = emit
        self.tasks: set[asyncio.Task] = set()
        self.verify_tasks: set[asyncio.Task] = set()
        self.context: deque[str] = deque(maxlen=CONTEXT_SEGMENTS)
        self.recent_claims: deque[str] = deque(maxlen=RECENT_CLAIMS)
        self.verify_sem = asyncio.Semaphore(MAX_CONCURRENT_VERIFICATIONS)
        self._stopped = False
        self.run_id = uuid.uuid4().hex[:6]
        self.claims: dict[str, tuple[Claim, str]] = {}

    def _spawn(self, coro, *, verification: bool = False) -> None:
        t = asyncio.create_task(coro)
        bucket = self.verify_tasks if verification else self.tasks
        bucket.add(t)
        t.add_done_callback(bucket.discard)

    async def run(self, url: str, language: str | None, start_seconds: float) -> None:
        await self.emit({"type": "status", "state": "resolving", "message": "Resolving stream…"})
        if settings.provider == "mock" and not url.strip():
            info = StreamInfo(video_id="", title="Mock stream", is_live=True)
        else:
            info, media_url = await audio.resolve_stream(url)
        await self.emit({"type": "info", "info": info.model_dump()})
        await self.emit({"type": "status", "state": "listening", "message": "Listening…"})

        if settings.provider == "mock" and not url.strip():
            chunks = self._mock_chunks()
        else:
            chunks = audio.iter_chunks(media_url, settings.chunk_seconds, info.is_live, start_seconds)

        async for chunk in chunks:
            if self._stopped:
                break
            self._spawn(self._handle_chunk(chunk, language))
        await self.emit({"type": "status", "state": "ended", "message": "Stream ended"})

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
            await self.emit({"type": "error", "message": f"Transcription failed: {e}"})
            return
        if not text:
            return
        seg = TranscriptSegment(id=f"seg-{self.run_id}-{chunk.index}", text=text, start=chunk.start, end=chunk.end)
        log.info("transcript[%s] %.0fs: %s", self.run_id, chunk.start, text)
        await self.emit({"type": "transcript", "segment": seg.model_dump()})

        context = " ".join(self.context)
        self.context.append(text)
        try:
            claims = await self.checker.extract_claims(context, text, list(self.recent_claims))
        except Exception as e:  # noqa: BLE001
            log.exception("claim extraction failed")
            await self.emit({"type": "error", "message": f"Claim extraction failed: {e}"})
            return
        log.info("claims[%s] %.0fs: %s", self.run_id, chunk.start, claims)
        for claim_text in claims:
            self.recent_claims.append(claim_text)
            claim = Claim(
                id=uuid.uuid4().hex[:10],
                text=claim_text,
                verdict=Verdict.PENDING,
                confidence=0,
                start=chunk.start,
                end=chunk.end,
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
                r = VerifyResult(Verdict.INCONCLUSIVE, 0.0, f"Verification failed: {e}")
        self._apply(claim, r)
        if r.verdict == Verdict.INCONCLUSIVE and settings.deep_check:
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
        claim.deep_pending = False
        await self.emit({"type": "claim", "claim": claim.model_dump()})

    async def recheck(self, claim_id: str) -> None:
        """Manual re-check from the UI: run the deep pass on an already-settled claim."""
        entry = self.claims.get(claim_id)
        if not entry:
            await self.emit({"type": "error", "message": "Claim not found for re-check"})
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
