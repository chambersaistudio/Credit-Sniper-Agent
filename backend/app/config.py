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


def _default_origins() -> List[str]:
    raw = os.getenv("ALLOWED_ORIGINS", "")
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    return ["http://localhost:3000", "http://localhost:5173", "http://localhost:8080"]


class Settings(BaseSettings):
    anthropic_api_key: str = ""
    database_url: str = _default_db_url()
    redis_url: str = "redis://localhost:6379/0"
    secret_key: str = "dev-secret-key-change-in-production"
    debug: bool = False
    allowed_origins: List[str] = _default_origins()
    upload_dir: str = _default_upload_dir()
    max_file_size_mb: int = 50

    model_config = {"env_file": ".env", "case_sensitive": False}


settings = Settings()

os.makedirs(settings.upload_dir, exist_ok=True)
