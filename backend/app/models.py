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


class TranscriptSegment(BaseModel):
    id: str
    text: str
    start: float
    end: float


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
