"""Newsroom access: who may open /admin and what they may do there.

- Owners (OWNER_EMAILS, fixed) can do everything, including managing users.
- Checkers (added by an owner in /admin → Usuários) work on fact-checks (list, correct, re-check), the news DB
  and the glossary. They cannot create, start or stop rooms, export data or manage users.

Sign-in is with Google (GOOGLE_CLIENT_ID): the browser gets an ID token from Google, the backend verifies
it and lets the person in only if their verified e-mail is on the list; it then hands out a signed session
token. The role is looked up on every request, so removing someone cuts them off at once.

Without GOOGLE_CLIENT_ID the old shared password (ADMIN_PASSWORD) still works and makes you an owner;
with neither, everyone is an owner (local development).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import settings

log = logging.getLogger(__name__)

ROLES = ("owner", "checker")
SESSION_DAYS = 7

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  email TEXT PRIMARY KEY, role TEXT NOT NULL, name TEXT, picture TEXT,
  added_by TEXT, added_at REAL, last_login REAL
);
"""


@dataclass
class User:
    email: str
    role: str  # "owner" | "checker"
    name: str = ""
    picture: str = ""

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"

    def view(self) -> dict:
        return asdict(self)


def _norm(email: str) -> str:
    return email.strip().lower()


class UserStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    @staticmethod
    def owners() -> list[str]:
        return [_norm(e) for e in settings.owner_emails.split(",") if e.strip()]

    def get(self, email: str) -> User | None:
        email = _norm(email)
        row = self.conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if email in self.owners():  # fixed in the environment: always an owner, cannot be removed here
            return User(email, "owner", (row["name"] or "") if row else "", (row["picture"] or "") if row else "")
        # "owner" rows only count while the e-mail is in OWNER_EMAILS: taking it out of the env revokes it
        if row and row["role"] == "checker":
            return User(email, "checker", row["name"] or "", row["picture"] or "")
        return None

    def list(self) -> list[dict]:
        rows = {r["email"]: dict(r) for r in self.conn.execute("SELECT * FROM users ORDER BY added_at")}
        out = [{**rows.get(e, {}), "email": e, "role": "owner", "fixed": True} for e in self.owners()]
        out += [{**r, "fixed": False} for e, r in rows.items() if e not in self.owners() and r["role"] == "checker"]
        return out

    def add(self, email: str, added_by: str, role: str = "checker") -> dict:
        """Owners only add checkers; owners themselves come from OWNER_EMAILS."""
        email = _norm(email)
        if role != "checker":
            raise ValueError("Pela interface só é possível cadastrar checadores")
        if "@" not in email or email in self.owners():
            raise ValueError("E-mail inválido" if "@" not in email else "Esse e-mail já é owner")
        self.conn.execute(
            "INSERT INTO users (email, role, added_by, added_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET role = excluded.role", (email, role, added_by, time.time()))
        self.conn.commit()
        return next(u for u in self.list() if u["email"] == email)

    def remove(self, email: str) -> bool:
        cur = self.conn.execute("DELETE FROM users WHERE email = ?", (_norm(email),))
        self.conn.commit()
        return cur.rowcount > 0

    def seen(self, email: str, name: str, picture: str) -> None:
        """Remember the Google name/photo for the list (owners get a row on their first sign-in)."""
        email = _norm(email)
        self.conn.execute(
            "INSERT INTO users (email, role, name, picture, added_at, last_login) VALUES (?, 'env-owner', ?, ?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET name = excluded.name, picture = excluded.picture, last_login = excluded.last_login",
            (email, name, picture, time.time(), time.time()))
        self.conn.commit()


# ---- session tokens (HMAC-signed e-mail + expiry; the role is looked up live) --------------------------
_secret = settings.session_secret.encode() if settings.session_secret else secrets.token_bytes(32)
if not settings.session_secret and settings.google_client_id:
    log.warning("SESSION_SECRET is empty: admin sessions end whenever the backend restarts")


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def make_token(email: str) -> str:
    body = _b64(json.dumps({"e": _norm(email), "x": int(time.time() + SESSION_DAYS * 86400)}).encode())
    return f"{body}.{_b64(hmac.new(_secret, body.encode(), hashlib.sha256).digest())}"


def read_token(token: str) -> str | None:
    try:
        body, sig = token.split(".", 1)
        if not hmac.compare_digest(sig, _b64(hmac.new(_secret, body.encode(), hashlib.sha256).digest())):
            return None
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return data["e"] if data["x"] > time.time() else None
    except Exception:  # noqa: BLE001 — anything malformed is simply not a session
        return None


async def verify_google(credential: str) -> dict:
    """The Google ID token's claims, or ValueError. Checks signature, audience (our client id), expiry, issuer."""
    from google.auth.transport import requests as grequests
    from google.oauth2 import id_token

    claims = await asyncio.to_thread(id_token.verify_oauth2_token, credential, grequests.Request(), settings.google_client_id)
    if not claims.get("email") or not claims.get("email_verified"):
        raise ValueError("E-mail do Google não verificado")
    return claims


class Access:
    """Resolves the credential a browser sends (session token, or the old password) to a User."""

    def __init__(self, users: UserStore):
        self.users = users

    @property
    def mode(self) -> str:
        if settings.google_client_id:
            return "google"
        return "password" if settings.admin_password else "open"

    def user(self, credential: str | None) -> User | None:
        credential = credential or ""
        if self.mode == "google":
            email = read_token(credential)
            return self.users.get(email) if email else None
        if self.mode == "password":
            ok = hmac.compare_digest(credential, settings.admin_password)
            return User("admin", "owner", "Administração") if ok else None
        return User("dev", "owner", "Desenvolvimento")  # open: local development only
