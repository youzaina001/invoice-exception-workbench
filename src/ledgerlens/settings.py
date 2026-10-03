"""Runtime settings. Setup-only admin credentials never live here."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", case_sensitive=False
    )

    intake_dsn: str | None = None
    read_dsn: str | None = None
    audit_dsn: str | None = None
    # Local operator journey stays unsigned. Hosted mode requires a Reviewer.
    hosted: bool = False
