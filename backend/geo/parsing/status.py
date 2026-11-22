"""Run status: is this a real answer, and if not, why not?

Order matters: a scraper error beats everything, then emptiness, then pages that are not
answers (login walls, captchas, rate limits, raw HTML), then answers to a different prompt,
then answers that were cut off. Only `ok` counts toward visibility.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from geo.platforms import Extracted

OK, ERROR, EMPTY, NOT_ANSWER, TRUNCATED, WRONG_PROMPT = (
    "ok", "error", "empty", "not_answer", "truncated", "wrong_prompt")

# Short pages that are the site talking, not the assistant answering.
NOT_ANSWER_PATTERNS = [
    (re.compile(r"<!doctype html|<html[\s>]", re.IGNORECASE), "html_page"),
    (re.compile(r"verify you are human|unusual traffic|captcha|security of your connection", re.IGNORECASE), "captcha"),
    (re.compile(r"(log|sign)[ -]?in to (continue|x\b|chatgpt|gemini)|you've been signed out|"
                r"available to logged-in users", re.IGNORECASE), "login_wall"),
    (re.compile(r"reached our limit|hit your limit|too many requests|try again later", re.IGNORECASE), "rate_limited"),
    (re.compile(r"before you continue to google|we use cookies and data", re.IGNORECASE), "consent_page"),
    (re.compile(r"something went wrong|an error occurred|network error", re.IGNORECASE), "site_error"),
]
NOT_ANSWER_MAX_CHARS = 600  # a long real answer can mention "sign in" without being a login wall

DANGLING = {"and", "or", "the", "a", "an", "with", "for", "to", "of", "which", "is", "are", "in", "on",
            "but", "that", "by", "from", "as", "at"}
TERMINAL = set(".!?)\"'”’»*|`]:")  # ':' is caught separately below when it is the very end
LIST_ITEM = re.compile(r"^\s*(?:\d+[.)]|[-*•+])\s+")


def _norm_prompt(p: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", p.lower()).strip()


def prompt_mismatch(asked: str | None, answered: str | None) -> bool:
    if not asked or not answered:
        return False
    a, b = _norm_prompt(asked), _norm_prompt(answered)
    return a != b and SequenceMatcher(None, a, b).ratio() < 0.85


def looks_truncated(text: str) -> str | None:
    """Return why the answer looks cut off, or None. Heuristic; tuned on labelled answers."""
    if text.count("```") % 2 == 1:
        return "unclosed_code_block"
    stripped = text.rstrip()
    if stripped.endswith(("...", "…")):
        return "ends_with_ellipsis"
    if stripped[-1] in ",:;-–—(":
        return "ends_mid_clause"
    last_word = re.findall(r"[A-Za-z']+$", stripped)
    if last_word and last_word[0].lower() in DANGLING:
        return "ends_on_connector"
    last_line = stripped.splitlines()[-1].strip()
    if stripped[-1] in TERMINAL or last_line.startswith(("#", "|")):
        return None
    # Assistants often end with follow-up options ("To narrow it down:\n\nCompare X vs Y"):
    # a final run of short lines after a line ending in ':' is complete, not cut off.
    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    colon = max((i for i, ln in enumerate(lines) if ln.endswith(":")), default=None)
    if colon is not None and 0 < len(lines) - colon - 1 <= 6 and all(len(ln.split()) <= 12 for ln in lines[colon + 1:]):
        return None
    # A short list item ("- Square") legitimately has no full stop; a long one usually does.
    if LIST_ITEM.match(last_line) and len(last_line.split()) <= 8:
        return None
    if stripped[-1].isalnum():
        return "no_terminal_punctuation"
    return None


def classify(ex: Extracted) -> tuple[str, str | None]:
    if ex.error:
        return ERROR, ex.error[:200]
    text = (ex.text or "").strip()
    if not text:
        return EMPTY, None
    if len(text) <= NOT_ANSWER_MAX_CHARS or text.lstrip().lower().startswith(("<!doctype", "<html")):
        for pattern, reason in NOT_ANSWER_PATTERNS:
            if pattern.search(text):
                return NOT_ANSWER, reason
    if prompt_mismatch(ex.asked_prompt, ex.answered_prompt):
        return WRONG_PROMPT, f"answered: {ex.answered_prompt[:120]}"
    if ex.truncated_flag is True:
        return TRUNCATED, "platform_flag"
    why = looks_truncated(text)
    if why:
        return TRUNCATED, why
    return OK, None
