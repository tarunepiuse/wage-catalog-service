"""Application settings, loaded from environment variables (prefix WTC_) and an optional .env file."""

import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_prefix="WTC_", extra="ignore")

    # --- Auth ---
    jwt_secret: str = Field(..., min_length=32, description="HMAC secret for signing JWTs")
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    access_token_minutes: int = Field(30, ge=1, le=24 * 60)
    login_max_failures: int = Field(5, ge=1)          # per username + client IP
    login_max_failures_per_ip: int = Field(20, ge=1)  # per client IP, any username
    login_lockout_minutes: int = Field(15, ge=1)
    api_key_max_days: int = Field(365, ge=1)

    # --- Storage ---
    user_db_path: Path = BASE_DIR / "data" / "users.db"
    wage_code_map_path: Path = BASE_DIR / "data" / "wage_codes.csv"
    job_dir: Path = Path(tempfile.gettempdir()) / "wage-catalog-jobs"

    # --- Limits & job lifecycle ---
    max_upload_mb: int = Field(10, ge=1, le=200)
    max_pending_jobs_per_user: int = Field(5, ge=1)
    max_concurrent_processing: int = Field(4, ge=1)
    processing_queue_timeout_seconds: float = Field(30, gt=0)
    job_ttl_minutes: int = Field(60, ge=1)
    cleanup_interval_seconds: int = Field(300, ge=5)

    # --- HTTP ---
    cors_origins: str = ""  # comma-separated; empty disables CORS
    enable_docs: bool = True

    # --- Logging ---
    log_level: str = "INFO"
    log_format: Literal["text", "json"] = "text"

    @field_validator("jwt_secret")
    @classmethod
    def _reject_placeholder(cls, v: str) -> str:
        if v.lower().startswith("change-me"):
            raise ValueError("WTC_JWT_SECRET is still the placeholder value; generate a real secret")
        return v

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def max_request_bytes(self) -> int:
        # Two files (xlsx, or line-items + master CSV) plus multipart framing.
        return self.max_upload_bytes * 2 + 64 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
