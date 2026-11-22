"""Raw record -> one shape for every platform.

Bump PARSER_VERSION whenever output can change. Parsed rows are keyed by
(raw_answer_id, parser_version), so a new version parses history alongside the old one and
`ops.activate_parser_version()` switches the metrics over atomically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from geo.catalog import Brand
from geo.parsing import status as st
from geo.parsing.mentions import Mention, MentionMatcher
from geo.parsing.urls import domain_of, host_of, normalize_url
from geo.platforms import adapter

PARSER_VERSION = "1.3.0"  # 1.3: escaped list numbers, ranking levels, closing options


@dataclass(frozen=True)
class Citation:
    ordinal: int
    url: str
    host: str
    domain: str


@dataclass(frozen=True)
class ParsedAnswer:
    status: str
    status_detail: str | None
    answer_chars: int
    mentions: list[Mention] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)


class Parser:
    version = PARSER_VERSION

    def __init__(self, brands: list[Brand]):
        self.matcher = MentionMatcher(brands)

    def parse(self, platform_id: str, record: dict[str, Any]) -> ParsedAnswer:
        try:
            return self._parse(platform_id, record)
        except Exception as exc:  # one malformed record must not stop the batch; it stays re-parseable
            return ParsedAnswer(st.ERROR, f"parse_failed: {type(exc).__name__}: {exc}"[:200], 0)

    def _parse(self, platform_id: str, record: dict[str, Any]) -> ParsedAnswer:
        ex = adapter(platform_id).extract(record)
        status, detail = st.classify(ex)
        text = ex.text or ""
        # Mentions and citations are kept for every status that has text: a cut-off answer's
        # mentions are still useful for debugging, and metrics filter on status = 'ok' anyway.
        mentions = self.matcher.find(text) if status in (st.OK, st.TRUNCATED) else []
        citations = self._citations(ex.citation_urls) if status in (st.OK, st.TRUNCATED) else []
        return ParsedAnswer(status, detail, len(text), mentions, citations)

    @staticmethod
    def _citations(urls: list[str]) -> list[Citation]:
        seen: set[str] = set()
        out: list[Citation] = []
        for raw in urls:
            url = normalize_url(raw)
            if not url or url in seen:
                continue
            seen.add(url)
            out.append(Citation(len(out) + 1, url, host_of(url), domain_of(url)))
        return out
