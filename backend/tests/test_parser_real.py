"""Real Bright Data records (collected 21 Nov 2026, page dumps omitted), labelled by hand."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from geo.catalog import load_brands
from geo.etl.pipeline import slim_record
from geo.parsing.parser import Parser
from geo.platforms import adapter_for_record

FIXTURES = Path(__file__).parent / "fixtures" / "brightdata"
P = Parser(load_brands())


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.mark.parametrize("name,expected_mentions,min_citations", [
    # ChatGPT: numbered list "1\\. Stripe Connect, 2\\. Adyen for Platforms, 3\\. Mangopay, 4\\. PayPal ...".
    # Detail bullets ("Useful where PayPal adoption matters") must not outrank the list.
    ("chatgpt", [("stripe", 1), ("adyen", 2), ("paypal", 3)], 8),
    # Gemini ends with follow-up suggestion chips: a complete answer, not a cut-off one.
    ("gemini", [("stripe", 1), ("adyen", 2), ("paypal", 3), ("braintree", 4)], 4),
    # Google AI Mode: citations live in `citations`, not `aio_citations`.
    ("google_ai_mode", [("stripe", 1), ("adyen", 2), ("paypal", 3), ("checkout", 4), ("braintree", 5)], 2),
])
def test_real_records(name, expected_mentions, min_citations):
    rec = load(name)
    ad = adapter_for_record(rec)
    assert ad is not None and ad.platform_id == name
    out = P.parse(name, rec)
    assert out.status == "ok"
    assert [(m.brand_id, m.position) for m in out.mentions] == expected_mentions
    assert len(out.citations) >= min_citations
    assert all("utm_" not in c.url for c in out.citations)


def test_slim_record_keeps_parsed_fields_and_hashes_dumps():
    rec = {"answer_text_markdown": "Stripe.", "answer_html": "<html>" + "x" * 200_000}
    slim = slim_record(rec)
    assert slim["answer_text_markdown"] == "Stripe."
    assert slim["answer_html"]["_omitted"] is True and slim["answer_html"]["bytes"] == 200_006
    assert len(slim["answer_html"]["sha256"]) == 64
