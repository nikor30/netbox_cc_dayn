"""Application settings, environment-driven (see .env.example)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    netbox_url: str = ""
    netbox_token: str = ""
    netbox_verify_ssl: bool = True
    app_port: int = 8070
    log_level: str = "INFO"
    upload_max_bytes: int = 2 * 1024 * 1024
    session_ttl_seconds: int = 3600


@lru_cache
def get_settings() -> Settings:
    return Settings()
