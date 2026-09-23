from pydantic import Field
from pydantic_settings import BaseSettings
from typing import List
import os


def _default_db_url() -> str:
    # Vercel Postgres provides POSTGRES_URL_NON_POOLING (direct connection,
    # no PgBouncer) which pairs correctly with SQLAlchemy's NullPool.
    raw = os.getenv("POSTGRES_URL_NON_POOLING") or os.getenv("POSTGRES_URL", "")
    if raw.startswith("postgres://"):
        # SQLAlchemy requires the +asyncpg dialect prefix
        raw = raw.replace("postgres://", "postgresql+asyncpg://", 1)
    return raw or "postgresql+asyncpg://creditsniper:password@localhost:5432/creditsniper"


def _default_upload_dir() -> str:
    # Vercel's filesystem is read-only except /tmp
    return "/tmp/uploads" if os.getenv("VERCEL") else "./uploads"


DEFAULT_ORIGINS = "http://localhost:3000,http://localhost:5173,http://localhost:8080"


class Settings(BaseSettings):
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # Per-tier AI routing overrides (ModelTier.FAST/REASONING/ESCALATION).
    # Empty string means "use the hardcoded default for this tier" — see
    # app/services/ai/provider.py. Lets ops repoint a tier at a different
    # provider/model without a code change.
    ai_fast_provider: str = ""
    ai_fast_model: str = ""
    ai_reasoning_provider: str = ""
    ai_reasoning_model: str = ""
    ai_escalation_provider: str = ""
    ai_escalation_model: str = ""

    database_url: str = _default_db_url()
    redis_url: str = "redis://localhost:6379/0"
    secret_key: str = "dev-secret-key-change-in-production"
    debug: bool = False
    # Kept as a plain comma-separated string (not List[str]): pydantic-settings
    # tries to JSON-decode List[...] env vars, which crashes on the plain
    # comma-separated format documented in .env.example.
    allowed_origins_raw: str = Field(default=DEFAULT_ORIGINS, validation_alias="ALLOWED_ORIGINS")
    upload_dir: str = _default_upload_dir()
    max_file_size_mb: int = 50

    model_config = {"env_file": ".env", "case_sensitive": False, "populate_by_name": True}

    @property
    def allowed_origins(self) -> List[str]:
        return [o.strip() for o in self.allowed_origins_raw.split(",") if o.strip()]


settings = Settings()

os.makedirs(settings.upload_dir, exist_ok=True)
