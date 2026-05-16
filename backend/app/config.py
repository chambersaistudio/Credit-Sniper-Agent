from pydantic_settings import BaseSettings
from typing import List
import os


class Settings(BaseSettings):
    anthropic_api_key: str = ""
    database_url: str = "postgresql+asyncpg://creditsniper:password@localhost:5432/creditsniper"
    sync_database_url: str = "postgresql://creditsniper:password@localhost:5432/creditsniper"
    redis_url: str = "redis://localhost:6379/0"
    secret_key: str = "dev-secret-key-change-in-production"
    debug: bool = True
    allowed_origins: List[str] = ["http://localhost:3000", "http://localhost:5173", "http://localhost:8080"]
    upload_dir: str = "./uploads"
    max_file_size_mb: int = 50

    model_config = {"env_file": ".env", "case_sensitive": False}


settings = Settings()

os.makedirs(settings.upload_dir, exist_ok=True)
