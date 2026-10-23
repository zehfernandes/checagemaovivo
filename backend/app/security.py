"""Security headers and per-IP limits for the public site.

- Headers: a Content-Security-Policy that only lets the page load scripts, frames and connections from this site,
  YouTube and Globo (players), Google (admin login) and Plausible (analytics); no framing by other sites (clickjacking of the
  admin); HSTS on the production host.
- Limits: requests per minute and open WebSockets per client IP, so one script cannot take the single machine (and
  the checking that runs on it) down. Generous on purpose: a newsroom or an event venue puts many people behind one
  IP. Both are settings (RATE_LIMIT_PER_MINUTE, WS_PER_IP; 0 turns a limit off).
"""
from __future__ import annotations

import base64
import hashlib
import re
import time
from collections import defaultdict, deque
from pathlib import Path

from starlette.requests import HTTPConnection

_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)


def client_ip(conn: HTTPConnection) -> str:
    """Fly puts the visitor's address in Fly-Client-IP; locally it is the socket's peer."""
    return conn.headers.get("fly-client-ip") or (conn.client.host if conn.client else "") or "?"


def inline_script_hashes(index_html: Path) -> list[str]:
    """CSP hashes of the built page's inline scripts (the analytics init), so no 'unsafe-inline' is needed."""
    if not index_html.is_file():
        return []
    return [f"'sha256-{base64.b64encode(hashlib.sha256(s.encode()).digest()).decode()}'"
            for s in _INLINE_SCRIPT.findall(index_html.read_text())]


def csp(script_hashes: list[str]) -> str:
    youtube = "https://www.youtube.com https://www.youtube-nocookie.com https://s.ytimg.com"
    google = "https://accounts.google.com"
    return "; ".join([
        "default-src 'self'",
        f"script-src 'self' {' '.join(script_hashes)} {youtube} {google}/gsi/client https://plausible.io".replace("  ", " "),
        f"style-src 'self' 'unsafe-inline' {google}/gsi/style",  # React's style={…} and Google's button
        "img-src 'self' data: https:",  # speaker photos (data:), outlet logos, Google avatars, YouTube
        "font-src 'self' data:",
        f"connect-src 'self' wss: {google}/gsi/ https://plausible.io",
        f"frame-src {youtube} {google} https://s.videos.globo.com",  # Globo's iframe player (g1 rooms)
        "frame-ancestors 'none'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
    ])


def security_headers(policy: str, hsts: bool) -> dict[str, str]:
    h = {
        "Content-Security-Policy": policy,
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    }
    if hsts:
        h["Strict-Transport-Security"] = "max-age=31536000"
    return h


class RateLimiter:
    """Sliding one-minute window of requests per IP, plus a count of open WebSockets per IP. In memory: the app runs
    on one machine, and a restart forgetting the counts is fine."""

    def __init__(self, per_minute: int, ws_per_ip: int):
        self.per_minute = per_minute
        self.ws_per_ip = ws_per_ip
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._ws: dict[str, int] = defaultdict(int)
        self._swept = time.monotonic()

    def allow(self, ip: str) -> bool:
        if not self.per_minute:
            return True
        now = time.monotonic()
        hits = self._hits[ip]
        while hits and hits[0] <= now - 60:
            hits.popleft()
        if len(hits) >= self.per_minute:
            return False
        hits.append(now)
        if now - self._swept > 300:  # forget quiet IPs now and then
            self._swept = now
            for k in [k for k, v in self._hits.items() if not v or v[-1] <= now - 60]:
                del self._hits[k]
        return True

    def ws_open(self, ip: str) -> bool:
        if self.ws_per_ip and self._ws[ip] >= self.ws_per_ip:
            return False
        self._ws[ip] += 1
        return True

    def ws_close(self, ip: str) -> None:
        self._ws[ip] -= 1
        if self._ws[ip] <= 0:
            del self._ws[ip]
