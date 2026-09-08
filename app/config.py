from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Postgres by default; tests override this with a SQLite URL.
    database_url: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/roadmap"

    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24  # 24h, no refresh flow

    # Roadmap generation (Groq). With no key configured, POST /roadmaps still
    # succeeds and the roadmap simply stays in `pending` — see app/generation.py.
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    groq_timeout_seconds: float = 60.0
    groq_max_retries: int = 2


@lru_cache
def get_settings() -> Settings:
    return Settings()
