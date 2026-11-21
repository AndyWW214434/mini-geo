"""Brand mentions and recommendation position.

Matching:
  * aliases match case-insensitively on word boundaries ("stripe's", "Stripe Connect");
  * strict aliases (common words such as "Square", "Wise", "Sift") match case-sensitively only;
  * URLs and markdown link targets are blanked first, so a cited stripe.com link is a citation,
    not a mention;
  * overlapping matches resolve to the longest: "PayPal Braintree" is Braintree, not PayPal.

Position: answers usually rank brands as a list, headings or a table. A brand's position is
the order in which it first heads a list item / heading / table row; brands named only in
prose follow, in order of first mention. With no list at all, position is first-mention order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from geo.catalog import Brand

URL_RE = re.compile(r"\]\([^)]*\)|https?://\S+|www\.\S+")
ITEM_RE = re.compile(r"^[ \t]*(?:(?:\d+[.)]|[-*•+])[ \t]+|#{1,6}[ \t]+|\|)", re.MULTILINE)
TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}")
HEAD_END = re.compile(r"\*\*(?=\s*[:\-–—])|\*\*\s*$|:|\s[-–—]\s|\|")


@dataclass(frozen=True)
class Mention:
    brand_id: str
    position: int
    matched_alias: str
    char_offset: int


@dataclass(frozen=True)
class _Hit:
    brand_id: str
    start: int
    end: int
    text: str


class MentionMatcher:
    def __init__(self, brands: list[Brand]):
        self._patterns: list[tuple[str, re.Pattern[str]]] = []
        for b in brands:
            loose = sorted({b.name, *b.aliases} - set(b.strict_aliases), key=len, reverse=True)
            if loose:
                self._patterns.append((b.brand_id, re.compile(
                    r"(?<![\w])(?:" + "|".join(re.escape(a) for a in loose) + r")(?![\w-])", re.IGNORECASE)))
            if b.strict_aliases:
                self._patterns.append((b.brand_id, re.compile(
                    r"(?<![\w])(?:" + "|".join(re.escape(a) for a in b.strict_aliases) + r")(?![\w-])")))

    @staticmethod
    def _blank_urls(text: str) -> str:
        return URL_RE.sub(lambda m: " " * len(m.group(0)), text)

    def _hits(self, text: str) -> list[_Hit]:
        hits = [_Hit(bid, m.start(), m.end(), m.group(0))
                for bid, pat in self._patterns for m in pat.finditer(text)]
        hits.sort(key=lambda h: (h.start, -(h.end - h.start)))
        kept: list[_Hit] = []
        for h in hits:  # drop matches inside a longer match (of any brand)
            if kept and h.start < kept[-1].end:
                continue
            kept.append(h)
        return kept

    @staticmethod
    def _heads(text: str) -> list[tuple[int, int]]:
        """Spans of list-item / heading / table-row heads, in document order."""
        spans = []
        for m in ITEM_RE.finditer(text):
            line_end = text.find("\n", m.end())
            line_end = len(text) if line_end == -1 else line_end
            line = text[m.end():line_end]
            if TABLE_SEPARATOR.match(text[m.start():line_end]):
                continue
            end = HEAD_END.search(line, 2)
            head_len = end.start() if end else min(len(line), 80)
            spans.append((m.end(), m.end() + head_len))
        return spans

    def find(self, text: str) -> list[Mention]:
        clean = self._blank_urls(text or "")
        hits = self._hits(clean)
        if not hits:
            return []
        first: dict[str, _Hit] = {}
        for h in hits:
            first.setdefault(h.brand_id, h)

        order: list[str] = []
        for start, end in self._heads(clean):
            for h in hits:
                if start <= h.start < end and h.brand_id not in order:
                    order.append(h.brand_id)
                    break  # one brand per item: the item's subject
        for h in sorted(first.values(), key=lambda x: x.start):
            if h.brand_id not in order:
                order.append(h.brand_id)
        return [Mention(bid, i + 1, first[bid].text, first[bid].start) for i, bid in enumerate(order)]
