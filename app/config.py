from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str

    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 60

    POOL_MIN: int = 1
    POOL_MAX: int = 10

    HOLD_DURATION_MINUTES: int = 5
    BOOKING_STRATEGY: Literal["A", "B"] = "A"

    WEBHOOK_HMAC_SECRET: str
    IDEMPOTENCY_IN_PROGRESS_TIMEOUT_SECONDS: int = 30
    IDEMPOTENCY_KEY_RETENTION_DAYS: int = 7

    CORS_ALLOWED_ORIGIN: str = "http://localhost:3000"
    PORT: int = 8000
    ENV: str = "development"
    AUTH_RATE_LIMIT_PER_MINUTE: int = 5


@lru_cache
def get_settings() -> Settings:
    return Settings()
