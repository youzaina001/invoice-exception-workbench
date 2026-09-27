"""Runtime settings. Setup-only admin credentials never live here."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", case_sensitive=False
    )

    warehouse_dsn: str = "postgresql+psycopg://dq:dq@localhost:5433/warehouse"
    intake_dsn: str | None = None
    read_dsn: str | None = None
    audit_dsn: str | None = None
    app_host: str = "127.0.0.1"
    app_port: int = 8101
