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
    ytdlp_args: str = ""  # extra yt-dlp args, e.g. "--cookies-from-browser chrome:/path/to/profile" when YouTube asks to "confirm you're not a bot"
    transcribe_glossary: str = (
        "Debate presidencial 2026 na Band. Lula, Flávio Bolsonaro, Jair Bolsonaro, Haddad, Galípolo, "
        "Daniel Vorcaro, Banco Master, André Mendonça, STF, TCU, Polícia Federal, Banco Central, "
        "Rui Costa, Jaques Wagner, Lulinha, Tarcísio, Michelle Bolsonaro, CACs, Pix, Bolsa Família, "
        "Dark Horse, fundo partidário, PL, PT, Alckmin, Vorcaro negou, R$ 134 milhões."
    )
    factcheck_model: str = "gpt-4.1-mini"
    # Verdict model for the web-search pass; gpt-5 models take a reasoning effort instead of temperature.
    verdict_model: str = "gpt-5-mini"
    verdict_effort: str = "low"
    # Deeper second pass for claims the fast pass left inconclusive (stronger model, more searches).
    deep_model: str = "gpt-5-mini"
    deep_effort: str = "medium"
    deep_check: bool = True
    # "openai" (gpt-4.1-mini decides from DB evidence) or "jev" (TypeSafe Jev via Vercel AI Gateway, ~0.3 s).
    decider: str = "openai"
    ai_gateway_api_key: str = ""
    jev_model: str = "typesafe-ai/jev"
    claim_gate_min: float = 0.35  # with decider=jev: drop candidate claims below this "checkable" probability
    claim_gate_min_save: float = 0.5  # save mode: stricter gate -> fewer verifications
    save_max_claims_per_min: int = 6
    # Empty = everyone is admin (dev). Set it in production: only admins can start processing,
    # re-check, refresh the news DB or edit verdicts; visitors only watch cached/ongoing runs.
    admin_password: str = ""
    # First room created on an empty DB (viewers pick rooms instead of pasting URLs); empty = none
    default_room_url: str = "https://www.youtube.com/watch?v=Amwukq3hviM"
    default_room_name: str = "Debate Band — 2º turno"
    # Only process/play a slice of given recordings: "VIDEO_ID=start-end,VIDEO_ID2=start-end" (seconds)
    video_clips: str = ""

    @property
    def clip_map(self) -> dict[str, tuple[float, float]]:
        out: dict[str, tuple[float, float]] = {}
        for part in self.video_clips.split(","):
            if "=" in part and "-" in part:
                vid, rng = part.strip().split("=", 1)
                a, b = rng.split("-", 1)
                out[vid] = (float(a), float(b))
        return out
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
    # verdict memory: a claim >= this similar to one settled in the last N days reuses that verdict (no AI calls)
    verdict_cache_similarity: float = 0.9
    verdict_cache_days: float = 7
    verdict_cache_min_confidence: float = 0.8  # don't replay shaky verdicts across videos
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
