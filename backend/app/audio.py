"""Pull audio from a YouTube URL (live or VOD) as fixed-size 16 kHz mono PCM chunks."""
import asyncio
import glob
import io
import json
import logging
import os
import shlex
import shutil
import time
import wave
from collections.abc import AsyncIterator
from dataclasses import dataclass

from .config import settings
from .models import StreamInfo

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2


@dataclass
class AudioChunk:
    index: int
    start: float
    end: float
    pcm: bytes
    captured_at: float = 0.0  # wall clock when the last byte of this chunk arrived from the source

    def to_wav(self) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(BYTES_PER_SAMPLE)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(self.pcm)
        return buf.getvalue()


def _node_path() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    candidates = sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/node")))
    return candidates[-1] if candidates else None


def _ytdlp_cmd() -> list[str]:
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    return ["python3", "-m", "yt_dlp"]


async def resolve_stream(url: str, attempts: int = 3) -> tuple[StreamInfo, str]:
    """Return stream metadata and a direct audio URL ffmpeg can read.
    YouTube intermittently answers "No video formats found!" / transient 403s; retry briefly."""
    last: Exception | None = None
    fmt = 0
    for i in range(attempts + len(FORMATS) - 1):
        try:
            return await _resolve_once(url, FORMATS[fmt])
        except RuntimeError as e:
            last = e
            msg = str(e)
            if "not a bot" in msg or "Private video" in msg or "unavailable" in msg.lower():
                raise
            log.warning("resolve_stream attempt %d (format %r) failed: %s", i + 1, FORMATS[fmt], msg[-160:])
            if "Requested format is not available" in msg and fmt < len(FORMATS) - 1:
                fmt += 1  # YouTube sometimes offers only some formats: relax the selector, no backoff
                continue
            if i >= attempts - 1 + fmt:
                break
            await asyncio.sleep(1.5 * (i + 1))
    raise last  # type: ignore[misc]


# audio-only HLS first (live edge friendly), then any audio, then anything ffmpeg can read
FORMATS = ["bestaudio[protocol^=m3u8]/bestaudio/best", "bestaudio*/best*", None]


async def _resolve_once(url: str, fmt: str | None = FORMATS[0]) -> tuple[StreamInfo, str]:
    cmd = [*_ytdlp_cmd(), *shlex.split(settings.ytdlp_args), *(["-f", fmt] if fmt else []),
           "-j", "--no-playlist", "--no-warnings", url]
    node = _node_path()
    if node:
        cmd[1:1] = ["--js-runtimes", f"node:{node}"]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp failed: {err.decode(errors='ignore')[-500:]}")
    info = json.loads(out)
    media_url = info.get("url") or info.get("manifest_url")
    if not media_url and info.get("requested_formats"):
        media_url = info["requested_formats"][0].get("url")
    if not media_url:
        raise RuntimeError("yt-dlp returned no media URL")
    live_status = info.get("live_status")
    if str(info.get("protocol", "")).startswith("http_dash_segments") and "m3u8" not in media_url:
        # ffmpeg can't read a segmented-DASH URL directly (yields a single ~2s segment). Typical
        # for a live that just ended: YouTube hasn't finished turning it into a normal recording.
        hint = "this live stream just ended and YouTube is still processing the recording — try again later" \
            if live_status == "post_live" else "YouTube only offers segmented DASH audio for this video"
        raise RuntimeError(f"No directly readable audio stream: {hint}")
    meta = StreamInfo(
        video_id=info.get("id", ""),
        title=info.get("title", ""),
        is_live=bool(info.get("is_live")) or live_status == "is_live",
        duration=info.get("duration"),
    )
    return meta, media_url


def extract_video_id(url: str) -> str | None:
    from urllib.parse import parse_qs, urlparse
    try:
        u = urlparse(url)
    except ValueError:
        return None
    if u.hostname and u.hostname.endswith("youtu.be"):
        return u.path.strip("/").split("/")[0] or None
    if u.path.startswith("/live/") or u.path.startswith("/shorts/"):
        parts = u.path.split("/")
        return parts[2] if len(parts) > 2 and parts[2] else None
    return (parse_qs(u.query).get("v") or [None])[0]


async def iter_chunks(
    media_url: str, chunk_seconds: int, is_live: bool, start_seconds: float = 0, realtime: bool = True
) -> AsyncIterator[AudioChunk]:
    """Decode with ffmpeg to raw PCM and yield one chunk every `chunk_seconds`.

    Live streams are read from the live edge. VOD is decoded at real-time speed
    (-re) so verdicts stay roughly in sync with the embedded player.
    """
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if is_live:
        if "m3u8" in media_url or "/hls" in media_url:
            cmd += ["-live_start_index", "-1"]  # HLS only: start at the live edge
    else:
        if realtime:
            cmd += ["-re"]
        if start_seconds > 0:
            cmd += ["-ss", str(start_seconds)]
    cmd += [
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
        "-i", media_url,
        "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "pipe:1",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    assert proc.stdout
    chunk_bytes = SAMPLE_RATE * BYTES_PER_SAMPLE * chunk_seconds
    index = 0
    try:
        while True:
            try:
                pcm = await proc.stdout.readexactly(chunk_bytes)
            except asyncio.IncompleteReadError as e:
                pcm = e.partial
                if len(pcm) > SAMPLE_RATE * BYTES_PER_SAMPLE:  # keep tail if > 1s
                    start = start_seconds + index * chunk_seconds
                    yield AudioChunk(index, start, start + len(pcm) / (SAMPLE_RATE * BYTES_PER_SAMPLE), pcm, time.time())
                break
            start = start_seconds + index * chunk_seconds
            yield AudioChunk(index, start, start + chunk_seconds, pcm, time.time())
            index += 1
    finally:
        if proc.returncode is None:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                pass
        if proc.stderr:
            err = (await proc.stderr.read()).decode(errors="ignore").strip()
            if err:
                log.warning("ffmpeg: %s", err[-500:])
