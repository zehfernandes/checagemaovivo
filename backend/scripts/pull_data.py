"""Download production's data files into backend/data so a local clone has the same cache.

    cd backend && python -m scripts.pull_data https://www.checagemaovivo.org ADMIN_PASSWORD

Stop the local backend first (it keeps the SQLite files open). Pass file names after the
password to pull a subset (default: cache.db news.db glossary.json).
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx

from app.config import settings

DEFAULT = ["cache.db", "news.db", "glossary.json"]


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    base, password, names = argv[0].rstrip("/"), argv[1], argv[2:] or DEFAULT
    dest = Path(settings.cache_db_path).resolve().parent
    dest.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=300, follow_redirects=True) as c:
        for name in names:
            r = c.get(f"{base}/admin/export/{name}", headers={"x-admin-password": password})
            if r.status_code != 200:
                print(f"{name}: HTTP {r.status_code} {r.text[:200]}")
                return 1
            (dest / name).write_bytes(r.content)
            print(f"{name}: {len(r.content) / 1e6:.1f} MB -> {dest / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
