"""Runtime settings. Everything secret or environment-specific comes from env vars."""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "../.env"), extra="ignore")

    database_url: str = "postgresql://postgres:postgres@localhost:54329/geo"
    db_pool_max: int = 10

    # Temporal Cloud: address like "<ns>.<acct>.tmprl.cloud:7233" plus an API key.
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_api_key: str | None = None
    temporal_task_queue: str = "geo-collection"

    # Collection
    brightdata_api_token: str | None = None
    collector_source: Literal["auto", "brightdata", "synthetic"] = "auto"
    collection_interval_minutes: int = 15
    collection_deadline_minutes: int = 50   # after this a window's run is marked timed_out
    brightdata_poll_seconds: int = 20
    # Day 0 of the synthetic generator's timeline (matches data/history.jsonl.gz).
    synthetic_epoch: date = date(2026, 10, 22)

    # Auth / product
    workos_api_key: str | None = None
    workos_client_id: str | None = None
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    public_api_url: str = "http://localhost:8000"

    # Chat
    anthropic_api_key: str | None = None
    chat_model: str = "claude-sonnet-5-5"
    mcp_url: str = "http://localhost:8000/mcp/"

    # Report
    focus_brand: str = "stripe"

    @property
    def effective_source(self) -> str:
        if self.collector_source != "auto":
            return self.collector_source
        return "brightdata" if self.brightdata_api_token else "synthetic"


@lru_cache
def settings() -> Settings:
    return Settings()
