"""Rooms: named broadcasts (a debate, a live interview…) an admin opens for the audience.

Each room points at one YouTube URL. Viewers pick a room instead of pasting URLs; the admin
starts/stops processing for it from /admin. Stored in the shared cache DB (table `rooms`).
"""
from __future__ import annotations

import secrets
import sqlite3
import time
from pathlib import Path

from . import audio

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
  id TEXT PRIMARY KEY, name TEXT, url TEXT, video_id TEXT, kind TEXT,
  active INTEGER DEFAULT 1, created REAL, updated REAL
);
"""
# columns added after the table existed: (name, declaration)
MIGRATIONS = [("human_review", "INTEGER DEFAULT 1")]


class RoomStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(rooms)")}
        for name, decl in MIGRATIONS:
            if name not in have:
                self.conn.execute(f"ALTER TABLE rooms ADD COLUMN {name} {decl}")
        self.conn.commit()

    def list(self, active_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM rooms" + (" WHERE active = 1" if active_only else "") + " ORDER BY created ASC"
        return [dict(r) for r in self.conn.execute(sql)]

    def get(self, room_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
        return dict(r) if r else None

    def create(self, name: str, url: str, kind: str = "debate") -> dict:
        vid = audio.extract_video_id(url) or url
        now = time.time()
        room_id = secrets.token_urlsafe(6)
        self.conn.execute("INSERT INTO rooms (id, name, url, video_id, kind, active, created, updated) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                          (room_id, name.strip(), url.strip(), vid, kind, now, now))
        self.conn.commit()
        return self.get(room_id)  # type: ignore[return-value]

    def update(self, room_id: str, **fields) -> dict | None:
        allowed = {k: v for k, v in fields.items() if k in ("name", "url", "kind", "active", "human_review") and v is not None}
        if "url" in allowed:
            allowed["video_id"] = audio.extract_video_id(allowed["url"]) or allowed["url"]
        for flag in ("active", "human_review"):
            if flag in allowed:
                allowed[flag] = int(bool(allowed[flag]))
        if allowed:
            allowed["updated"] = time.time()
            sets = ", ".join(f"{k} = ?" for k in allowed)
            self.conn.execute(f"UPDATE rooms SET {sets} WHERE id = ?", (*allowed.values(), room_id))
            self.conn.commit()
        return self.get(room_id)

    def holds_for_review(self, video_id: str) -> bool:
        """Whether new machine analyses of this video wait for a person before reaching the site:
        yes unless its room(s) turned "Aguardar checagem humana" off (no room = the default, wait)."""
        rows = self.conn.execute("SELECT human_review FROM rooms WHERE video_id = ?", (video_id,)).fetchall()
        return not rows or any(r["human_review"] for r in rows)

    def delete(self, room_id: str) -> bool:
        cur = self.conn.execute("DELETE FROM rooms WHERE id = ?", (room_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]
