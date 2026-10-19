"""Pull audio from a YouTube URL (live or VOD) as fixed-size 16 kHz mono PCM chunks."""
import asyncio
import glob
import importlib.util
import io
import json
import logging
import os
import re
import shlex
import shutil
import sys
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


def _ffmpeg_path() -> str:
    # An ffmpeg installed into this interpreter's environment (e.g. conda-forge in .venv) wins over PATH.
    return shutil.which("ffmpeg", path=os.path.dirname(sys.executable)) or "ffmpeg"


def _ytdlp_cmd() -> list[str]:
    # Prefer the yt-dlp installed with this interpreter (requirements.txt): one found on PATH may be an
    # older copy (e.g. Anaconda's) that lacks options we pass, like --js-runtimes.
    if importlib.util.find_spec("yt_dlp"):
        return [sys.executable, "-m", "yt_dlp"]
    exe = shutil.which("yt-dlp")
    return [exe] if exe else ["python3", "-m", "yt_dlp"]


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
            msg = e.raw if isinstance(e, StreamError) else str(e)
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
    node = _node_path()
    cmd = [*_ytdlp_cmd(), *(["--js-runtimes", f"node:{node}"] if node else []), *shlex.split(settings.ytdlp_args),
           *(["-f", fmt] if fmt else []), "-j", "--no-playlist", "--no-warnings", url]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raw = err.decode(errors="ignore").strip()[-500:]
        raise StreamError(f"yt-dlp failed: {raw}", friendly_error(raw))
    info = json.loads(out)
    media_url = info.get("url") or info.get("manifest_url")
    if not media_url and info.get("requested_formats"):
        media_url = info["requested_formats"][0].get("url")
    if not media_url:
        raise StreamError("yt-dlp returned no media URL", "Este link não tem um áudio que consigamos ler.")
    live_status = info.get("live_status")
    if str(info.get("protocol", "")).startswith("http_dash_segments") and "m3u8" not in media_url:
        # ffmpeg can't read a segmented-DASH URL directly (yields a single ~2s segment). Typical
        # for a live that just ended: YouTube hasn't finished turning it into a normal recording.
        hint = "this live stream just ended and YouTube is still processing the recording — try again later" \
            if live_status == "post_live" else "YouTube only offers segmented DASH audio for this video"
        friendly = ("A live acabou de terminar e o YouTube ainda está processando a gravação. Tente mais tarde."
                    if live_status == "post_live" else "O YouTube só oferece este vídeo em um formato de áudio que não conseguimos ler.")
        raise StreamError(f"No directly readable audio stream: {hint}", friendly)
    meta = StreamInfo(
        video_id=info.get("id", ""),
        title=info.get("title", ""),
        is_live=bool(info.get("is_live")) or live_status == "is_live",
        duration=info.get("duration"),
        source=source_of(url),
        page_url=url,
    )
    return meta, media_url


class StreamError(RuntimeError):
    """yt-dlp could not open the URL. `raw` keeps yt-dlp's words for logs/retry logic; str() is the
    pt-BR message shown in the newsroom and to viewers."""

    def __init__(self, raw: str, friendly: str):
        super().__init__(friendly)
        self.raw = raw
        self.friendly = friendly


# yt-dlp stderr fragment -> what the newsroom/viewer should read (first match wins)
_FRIENDLY: list[tuple[str, str]] = [
    ("is not a valid URL", "Link inválido."),
    ("Unsupported URL", "Este link não é de um vídeo suportado (YouTube, g1/Globo…)."),
    ("DRM", "Este vídeo tem proteção DRM (Widevine): não é possível ler o áudio."),
    ("Private video", "Vídeo privado."),
    ("members-only", "Vídeo exclusivo para membros."),
    ("Sign in to confirm", "O YouTube bloqueou o servidor (verificação anti-bot)."),
    ("Sign in", "Este vídeo exige login."),
    ("401", "Este vídeo exige login ou assinatura."),
    ("403", "Acesso ao vídeo negado (login, assinatura ou região)."),
    ("404", "Vídeo não encontrado neste link."),
    ("will begin in", "A live ainda não começou."),
    ("Premieres in", "A live ainda não começou."),
    ("not available in your country", "Vídeo indisponível na região do servidor."),
    ("This video has been removed", "Vídeo removido."),
    ("Video unavailable", "Vídeo indisponível."),
    ("Requested format is not available", "Este vídeo não tem uma faixa de áudio que consigamos ler."),
]


def friendly_error(stderr: str) -> str:
    low = stderr.lower()
    for needle, text in _FRIENDLY:
        if needle.lower() in low:
            return text
    return "Não conseguimos ler o áudio deste link."


def source_of(url: str) -> str:
    """'youtube' or a short name for the site hosting the page (g1, globoplay, ge, <hostname>)."""
    from urllib.parse import urlparse
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return "other"
    if host.endswith(("youtube.com", "youtu.be")):
        return "youtube"
    host = host.removeprefix("www.")
    if host.endswith(".globo.com"):
        return host.split(".")[0]
    return host or "other"


def is_supported_url(url: str) -> bool:
    from urllib.parse import urlparse
    try:
        u = urlparse(url.strip())
    except ValueError:
        return False
    return u.scheme in ("http", "https") and bool(u.hostname and "." in u.hostname)


def extract_video_id(url: str) -> str | None:
    from urllib.parse import parse_qs, urlparse
    try:
        u = urlparse(url)
    except ValueError:
        return None
    if u.hostname and u.hostname.endswith("youtu.be"):
        return u.path.strip("/").split("/")[0] or None
    if u.hostname and u.hostname.endswith("globo.com"):
        # g1/globoplay pages end in the Globo video id (…-14963827.ghtml, /v/14963827/), which is
        # also the id yt-dlp reports, so cache rows, rooms and channels line up
        found = re.findall(r"(\d{7,})", u.path)
        return found[-1] if found else None
    if u.path.startswith("/live/") or u.path.startswith("/shorts/"):
        parts = u.path.split("/")
        return parts[2] if len(parts) > 2 and parts[2] else None
    return (parse_qs(u.query).get("v") or [None])[0]


async def iter_chunks(
    media_url: str, chunk_seconds: float, is_live: bool, start_seconds: float = 0, realtime: bool = True,
    sample_rate: int = SAMPLE_RATE,
) -> AsyncIterator[AudioChunk]:
    """Decode with ffmpeg to raw PCM and yield one chunk every `chunk_seconds`.

    Live streams are read from the live edge. VOD is decoded at real-time speed
    (-re) so verdicts stay roughly in sync with the embedded player.
    """
    cmd = [_ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-nostdin"]
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
        # a network drop can leave ffmpeg blocked on a read forever (no audio, no exit); erroring out
        # after 20 s ends the stream so the channel's "Áudio caiu — reconectando" retry takes over
        "-rw_timeout", "20000000",
        "-i", media_url,
        "-vn", "-ac", "1", "-ar", str(sample_rate), "-f", "s16le", "pipe:1",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    assert proc.stdout
    bytes_per_second = sample_rate * BYTES_PER_SAMPLE
    chunk_bytes = int(bytes_per_second * chunk_seconds)
    index = 0
    try:
        while True:
            try:
                pcm = await proc.stdout.readexactly(chunk_bytes)
            except asyncio.IncompleteReadError as e:
                pcm = e.partial
                if len(pcm) > min(bytes_per_second, chunk_bytes // 2):  # keep tail if > 1s (or half a short frame)
                    start = start_seconds + index * chunk_seconds
                    yield AudioChunk(index, start, start + len(pcm) / bytes_per_second, pcm, time.time())
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
