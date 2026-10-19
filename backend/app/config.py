from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "../.env"), extra="ignore")

    provider: str = "mock"
    openai_api_key: str = ""
    openai_base_url: str = ""
    transcribe_model: str = "gpt-4o-mini-transcribe"
    factcheck_model: str = "gpt-4.1-mini"
    chunk_seconds: int = 8
    language: str = ""
    cors_origins: str = "http://localhost:5173"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
