import os
from typing import List
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

LOCAL_DATABASE_URL = "postgresql+asyncpg://creditsniper:password@localhost:5432/creditsniper"


def normalize_database_url(url: str) -> str:
    """Hosted Postgres (Vercel/Neon/Supabase) hands out postgres:// or
    postgresql:// URLs with libpq's ?sslmode=. SQLAlchemy's async engine needs
    the +asyncpg driver, and asyncpg spells that option ?ssl=."""
    if not url:
        return url
    parts = urlsplit(url)
    scheme = "postgresql+asyncpg" if parts.scheme in ("postgres", "postgresql") else parts.scheme
    query = [("ssl", v) if k == "sslmode" else (k, v) for k, v in parse_qsl(parts.query) if k != "channel_binding"]
    return urlunsplit((scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _default_db_url() -> str:
    # Vercel Postgres provides POSTGRES_URL_NON_POOLING (direct connection,
    # no PgBouncer), which pairs correctly with NullPool.
    return os.getenv("POSTGRES_URL_NON_POOLING") or os.getenv("POSTGRES_URL") or LOCAL_DATABASE_URL


def _default_upload_dir() -> str:
    # Vercel's filesystem is read-only except /tmp
    return "/tmp/uploads" if os.getenv("VERCEL") else "./uploads"


DEFAULT_ORIGINS = "http://localhost:3000,http://localhost:5173,http://localhost:8080"


class Settings(BaseSettings):
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # Per-tier AI routing overrides — empty means the tier's default in
    # app/services/ai/config.py.
    ai_fast_provider: str = ""
    ai_fast_model: str = ""
    ai_fast_effort: str = ""
    ai_reasoning_provider: str = ""
    ai_reasoning_model: str = ""
    ai_reasoning_effort: str = ""
    ai_escalation_provider: str = ""
    ai_escalation_model: str = ""
    ai_escalation_effort: str = ""

    database_url: str = _default_db_url()
    # Required header value for POST /api/migrate; the endpoint is disabled while empty.
    admin_token: str = ""
    debug: bool = False
    # Kept as a plain comma-separated string (not List[str]): pydantic-settings
    # tries to JSON-decode List[...] env vars, which crashes on the plain
    # comma-separated format documented in .env.example.
    allowed_origins_raw: str = Field(default=DEFAULT_ORIGINS, validation_alias="ALLOWED_ORIGINS")
    upload_dir: str = _default_upload_dir()
    max_file_size_mb: int = 50

    model_config = {"env_file": ".env", "case_sensitive": False, "populate_by_name": True}

    @field_validator("database_url")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        return normalize_database_url(value)

    @property
    def allowed_origins(self) -> List[str]:
        return [o.strip() for o in self.allowed_origins_raw.split(",") if o.strip()]


settings = Settings()

os.makedirs(settings.upload_dir, exist_ok=True)
