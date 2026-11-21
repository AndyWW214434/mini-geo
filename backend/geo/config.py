"""Runtime settings. Everything secret or environment-specific comes from env vars."""

from __future__ import annotations

import json
import os
from datetime import date
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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
    # Accepts a JSON list or a comma-separated string; a typo here must not stop the API booting.
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["http://localhost:3000"])
    public_api_url: str = "http://localhost:8000"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _origins(cls, v: object) -> object:
        if not isinstance(v, str):
            return v
        text = v.strip().strip("'\"").strip()
        if text.startswith("["):
            try:
                return [str(o).strip().rstrip("/") for o in json.loads(text.replace("'", '"'))]
            except ValueError:
                text = text.strip("[]")
        origins = [o.strip().strip("'\"").rstrip("/") for o in text.split(",")]
        return [o for o in origins if o] or ["http://localhost:3000"]

    # Chat
    anthropic_api_key: str | None = None
    chat_model: str = "claude-opus-5-5"
    # The chat reaches the MCP server over HTTP, as the user. Default: this API process over loopback.
    mcp_url_override: str | None = Field(default=None, alias="MCP_URL")

    # Report
    focus_brand: str = "stripe"

    @property
    def mcp_url(self) -> str:
        return self.mcp_url_override or f"http://127.0.0.1:{os.environ.get('PORT', '8000')}/mcp/"

    @property
    def effective_source(self) -> str:
        if self.collector_source != "auto":
            return self.collector_source
        return "brightdata" if self.brightdata_api_token else "synthetic"


@lru_cache
def settings() -> Settings:
    return Settings()
