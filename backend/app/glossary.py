"""Transcription glossary: names/terms passed as the speech model's prompt plus post-transcription
term fixes ("Bocaro" -> "Vorcaro"). Editable by the admin at runtime and persisted next to the cache DB."""
import json
import logging
import re
import time
from pathlib import Path

from .config import settings

log = logging.getLogger(__name__)

DEFAULT_REPLACEMENTS: dict[str, str] = {
    "Bocaro": "Vorcaro", "Vocaro": "Vorcaro", "Borcaro": "Vorcaro", "Vorkaro": "Vorcaro", "vocário": "Vorcaro",
}


def _path() -> Path:
    return Path(settings.cache_db_path).parent / "glossary.json"


class Glossary:
    def __init__(self) -> None:
        self.terms = settings.transcribe_glossary.strip()
        self.replacements = dict(DEFAULT_REPLACEMENTS)
        self.updated_at: float | None = None
        self._rx: re.Pattern[str] | None = None
        self._by_lower: dict[str, str] = {}
        self.load()

    def load(self) -> None:
        p = _path()
        if p.exists():
            try:
                d = json.loads(p.read_text())
                self.terms = str(d.get("terms", self.terms))
                self.replacements = {str(k): str(v) for k, v in (d.get("replacements") or {}).items()}
                self.updated_at = d.get("updated_at")
            except Exception:  # noqa: BLE001
                log.exception("glossary file unreadable, using defaults")
        self._compile()

    def save(self, terms: str, replacements: dict[str, str]) -> dict:
        self.terms = terms.strip()
        self.replacements = {k.strip(): v.strip() for k, v in replacements.items() if k.strip() and v.strip()}
        self.updated_at = time.time()
        self._compile()
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"terms": self.terms, "replacements": self.replacements, "updated_at": self.updated_at}, ensure_ascii=False, indent=1))
        return self.dump()

    def _compile(self) -> None:
        keys = sorted(self.replacements, key=len, reverse=True)
        self._by_lower = {k.lower(): v for k, v in self.replacements.items()}
        self._rx = re.compile(r"(?<!\w)(" + "|".join(map(re.escape, keys)) + r")(?!\w)", re.IGNORECASE) if keys else None

    def prompt(self) -> str:
        return self.terms

    def apply(self, text: str) -> str:
        if not text or not self._rx:
            return text
        return self._rx.sub(lambda m: self._by_lower.get(m.group(0).lower(), m.group(0)), text)

    def names(self) -> list[str]:
        return [g.strip() for g in self.terms.replace(".", ",").split(",") if g.strip()]

    def dump(self) -> dict:
        return {"terms": self.terms, "replacements": [{"from": k, "to": v} for k, v in self.replacements.items()], "updated_at": self.updated_at}


glossary = Glossary()
