"""Platform adapters: the only code that knows what one assistant's records look like.

Each AI platform returns a different shape and fails in its own ways. An adapter turns one
raw Bright Data record into `Extracted` (text, citation URLs, error, the prompt the page
actually answered). Everything downstream (parser, ETL, insights) is platform-agnostic.

Adding a platform = one subclass (see adapters.py), plus a row in config.platforms (the
seed does that from the registry). Subclasses register themselves.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar
from urllib.parse import urlsplit

REGISTRY: dict[str, PlatformAdapter] = {}


@dataclass(frozen=True)
class Extracted:
    text: str | None
    citation_urls: list[str] = field(default_factory=list)
    error: str | None = None
    asked_prompt: str | None = None      # what we asked (input.prompt)
    answered_prompt: str | None = None   # what the page shows it answered (record.prompt)
    truncated_flag: bool | None = None   # some platforms say so explicitly


class PlatformAdapter(ABC):
    platform_id: ClassVar[str]
    name: ClassVar[str]
    hosts: ClassVar[tuple[str, ...]]
    target_url: ClassVar[str]
    # Bright Data dataset id for this platform's AI scraper. Overridable per environment via
    # config.platforms.settings->>'dataset_id' (verify ids in the Bright Data console).
    default_dataset_id: ClassVar[str]

    def __init_subclass__(cls, **kw: Any) -> None:
        super().__init_subclass__(**kw)
        if not getattr(cls, "__abstractmethods__", None):
            REGISTRY[cls.platform_id] = cls()

    # ---- collection side
    def build_input(self, prompt_text: str) -> dict[str, Any]:
        return {"url": self.target_url, "prompt": prompt_text}

    def matches(self, record: dict[str, Any]) -> bool:
        url = (record.get("input") or {}).get("url") or record.get("url") or ""
        host = (urlsplit(url).hostname or "").removeprefix("www.")
        return host in self.hosts

    # ---- parsing side
    @abstractmethod
    def extract(self, record: dict[str, Any]) -> Extracted: ...

    @staticmethod
    def common_error(record: dict[str, Any]) -> str | None:
        """Bright Data marks failed collections with error / error_code fields."""
        if record.get("error") or record.get("error_code"):
            return f"{record.get('error_code') or 'error'}: {record.get('error') or ''}".strip()
        return None

    @staticmethod
    def prompts(record: dict[str, Any]) -> tuple[str | None, str | None]:
        return (record.get("input") or {}).get("prompt"), record.get("prompt")


def adapter(platform_id: str) -> PlatformAdapter:
    return REGISTRY[platform_id]


def adapter_for_record(record: dict[str, Any]) -> PlatformAdapter | None:
    return next((a for a in REGISTRY.values() if a.matches(record)), None)


def url_list(items: Any, *keys: str) -> list[str]:
    """Pull URLs out of a list of strings or dicts, trying each key in order."""
    out: list[str] = []
    for item in items or []:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            for k in keys:
                if isinstance(item.get(k), str):
                    out.append(item[k])
                    break
    return out
