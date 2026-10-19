from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "../.env"), extra="ignore")

    provider: str = "mock"
    openai_api_key: str = ""
    openai_base_url: str = ""
    transcribe_model: str = "gpt-4o-mini-transcribe"
    # Names/terms the speech model should spell correctly (passed as the transcription prompt).
    transcribe_glossary: str = (
        "Debate presidencial 2026 na Band. Lula, Flávio Bolsonaro, Jair Bolsonaro, Haddad, Galípolo, "
        "Daniel Vorcaro, Banco Master, André Mendonça, STF, TCU, Polícia Federal, Banco Central, "
        "Rui Costa, Jaques Wagner, Lulinha, Tarcísio, Michelle Bolsonaro, CACs, Pix, Bolsa Família."
    )
    factcheck_model: str = "gpt-4.1-mini"
    # Deeper second pass for claims the fast pass left inconclusive (stronger model, more searches).
    deep_model: str = "gpt-4.1"
    deep_check: bool = True
    # "openai" (gpt-4.1-mini decides from DB evidence) or "jev" (TypeSafe Jev via Vercel AI Gateway, ~0.3 s).
    decider: str = "openai"
    ai_gateway_api_key: str = ""
    jev_model: str = "typesafe-ai/jev"
    claim_gate_min: float = 0.35  # with decider=jev: drop candidate claims below this "checkable" probability
    chunk_seconds: int = 8
    language: str = ""
    cors_origins: str = "http://localhost:5173"
    # News/evidence DB (first-level lookup before web search)
    news_db_path: str = "data/news.db"
    cache_db_path: str = "data/cache.db"
    # Recorded videos: process at playback speed while viewers are connected (default, cheap) or
    # run through the whole video at full speed once (one-off cost, instant seeks for everyone).
    vod_full_speed: bool = False
    embedding_model: str = "text-embedding-3-small"
    news_min_similarity: float = 0.55
    news_max_age_days: int = 14
    news_refresh_minutes: int = 30  # 0 disables the background refresh
    news_feeds: str = ""  # optional override: "Name=https://feed,Name2=https://feed2"

    @property
    def news_db_file(self) -> Path:
        p = Path(self.news_db_path)
        return p if p.is_absolute() else BACKEND_DIR / p

    @property
    def news_feed_map(self) -> dict[str, str] | None:
        if not self.news_feeds.strip():
            return None
        return dict(item.split("=", 1) for item in self.news_feeds.split(",") if "=" in item)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
