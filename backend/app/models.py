from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Verdict(str, Enum):
    PENDING = "pending"
    FAKE = "fake"
    VALID = "valid"
    INCONCLUSIVE = "inconclusive"


class Source(BaseModel):
    title: str = ""
    url: str


class Claim(BaseModel):
    id: str
    text: str
    verdict: Verdict
    confidence: float = Field(ge=0, le=1)
    explanation: str = ""
    sources: list[Source] = []
    start: float
    end: float
    speaker: str | None = None
    method: Literal["cache", "db", "web", "deep"] | None = None
    deep_pending: bool = False
    # latency instrumentation (server wall clock, seconds since epoch)
    captured_at: float | None = None  # audio of this claim finished arriving from YouTube
    extracted_at: float | None = None  # claim emitted as pending
    verified_at: float | None = None  # first settled verdict emitted


class TranscriptSegment(BaseModel):
    id: str
    text: str
    start: float
    end: float
    captured_at: float | None = None
    transcribed_at: float | None = None


class StreamInfo(BaseModel):
    video_id: str
    title: str
    is_live: bool
    duration: float | None = None


class StartRequest(BaseModel):
    type: Literal["start"]
    url: str
    language: str | None = None
    start_seconds: float = 0
    fresh: bool = False  # private pipeline: ignore the shared channel and cache (latency testing)
