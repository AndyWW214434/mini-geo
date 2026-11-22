"""Hand-labelled answers covering the ways real scraper output goes wrong."""

from __future__ import annotations

import base64

import pytest

from geo.catalog import load_brands
from geo.parsing.parser import Parser
from geo.parsing.urls import domain_of, normalize_url

P = Parser(load_brands())
Q = "What's the best payments API for a marketplace?"


def chatgpt(md: str, citations: list[str] | None = None, prompt: str = Q, **extra) -> dict:
    return {"url": "https://chatgpt.com/", "prompt": prompt, "answer_text_markdown": md,
            "citations": [{"url": u} for u in citations or []], "input": {"url": "https://chatgpt.com/",
                                                                           "prompt": Q}, **extra}


def brands_in_order(rec: dict, platform: str = "chatgpt") -> list[tuple[str, int]]:
    return [(m.brand_id, m.position) for m in P.parse(platform, rec).mentions]


# ------------------------------------------------------------------------- status
@pytest.mark.parametrize("rec,platform,expected", [
    ({"input": {"url": "https://chatgpt.com/", "prompt": Q}, "error": "Timed out", "error_code": "timeout"},
     "chatgpt", "error"),
    (chatgpt("   "), "chatgpt", "empty"),
    (chatgpt("You've reached our limit of messages. Please try again later."), "chatgpt", "not_answer"),
    ({"url": "https://www.perplexity.ai/", "answer_text": "Verify you are human by completing the action below.",
      "input": {"url": "https://www.perplexity.ai/", "prompt": Q}}, "perplexity", "not_answer"),
    ({"url": "https://www.google.com/", "aio_text": "<!DOCTYPE html><html><body>cookies</body></html>",
      "input": {"url": "https://www.google.com/", "prompt": Q}}, "google_ai_mode", "not_answer"),
    (chatgpt("1. **Stripe**: best docs.", prompt="Best fraud tool for card payments?"), "chatgpt",
     "wrong_prompt"),
    (chatgpt("1. **Stripe**: great for marketplaces.\n2. **Adyen**: strong for enterprise and"),
     "chatgpt", "truncated"),
    (chatgpt("Here is a snippet:\n```python\nimport stripe\n"), "chatgpt", "truncated"),
    ({"url": "https://www.google.com/", "aio_text": "Stripe is a common choice.", "is_truncated": True,
      "input": {"url": "https://www.google.com/", "prompt": Q}}, "google_ai_mode", "truncated"),
    (chatgpt("Use Stripe Connect.\n\n- Stripe\n- Adyen"), "chatgpt", "ok"),
    (chatgpt("Stripe Billing fits most SaaS.\n\nWant a breakdown of Paddle vs Lemon Squeezy?\n\nYes"), "chatgpt", "ok"),
    (chatgpt("Use Stripe.\n\nWhich setup fits you?\n\nCompare top MoR platforms\n\nB2B vs. B2C tax rules"),
     "chatgpt", "ok"),
    (chatgpt("| Provider | Best for |\n|---|---|\n| Stripe | marketplaces |"), "chatgpt", "ok"),
])
def test_status(rec, platform, expected):
    assert P.parse(platform, rec).status == expected


def test_long_answer_mentioning_sign_in_is_still_an_answer():
    text = ("Stripe Connect is the usual choice. " * 20) + "Sellers sign in to continue onboarding later."
    assert P.parse("chatgpt", chatgpt(text)).status == "ok"


# ------------------------------------------------------------------------- mentions & positions
def test_numbered_list_sets_position_even_when_intro_names_another_brand():
    md = ("Stripe is the default for many teams, but it depends.\n\n"
          "1. **Adyen for Platforms**: enterprise pricing.\n2. **Stripe Connect**: fastest to ship.\n"
          "3. **PayPal Braintree**: buyer trust.")
    assert brands_in_order(chatgpt(md)) == [("adyen", 1), ("stripe", 2), ("braintree", 3)]


def test_longest_alias_wins_overlap():
    # "PayPal Braintree" is Braintree, not a PayPal mention.
    assert brands_in_order(chatgpt("Consider PayPal Braintree for card payments.")) == [("braintree", 1)]


def test_common_words_are_not_brands():
    md = ("Make sure your checkout page is fast. It is wise to sift through disputes. "
          "Keep it fair and square. Stripe works well.")
    assert brands_in_order(chatgpt(md)) == [("stripe", 1)]


def test_strict_aliases_match_when_capitalised():
    assert brands_in_order(chatgpt("- Square: in-person payments.\n- Wise: cheap FX.")) == [
        ("square", 1), ("wise", 2)]


def test_links_are_citations_not_mentions():
    md = "Adyen is strong. See https://stripe.com/docs and [the guide](https://stripe.com/connect)."
    assert brands_in_order(chatgpt(md)) == [("adyen", 1)]


def test_table_rows_rank_brands():
    md = "| Provider | Best for |\n|---|---|\n| Checkout.com | cross-border |\n| Stripe | startups |"
    assert brands_in_order(chatgpt(md)) == [("checkout", 1), ("stripe", 2)]


def test_prose_ranks_by_first_mention():
    md = "The strongest choice is Adyen. Another option is Stripe, which offers broad coverage."
    assert brands_in_order(chatgpt(md)) == [("adyen", 1), ("stripe", 2)]


def test_possessive_and_product_names():
    md = "Stripe's Radar handles fraud; Adyen RevenueProtect is the enterprise option."
    assert brands_in_order(chatgpt(md)) == [("stripe", 1), ("adyen", 2)]


# ------------------------------------------------------------------------- citations
def test_bing_and_google_redirects_are_unwrapped():
    target = "https://docs.stripe.com/connect?utm_source=copilot#setup"
    bing = "https://www.bing.com/ck/a?!&&p=abc&u=a1" + base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    google = "https://www.google.com/url?q=https%3A%2F%2Fwww.adyen.com%2Fpayments&sa=U"
    assert normalize_url(bing) == "https://docs.stripe.com/connect"
    assert normalize_url(google) == "https://www.adyen.com/payments"
    assert domain_of("https://docs.stripe.com/connect") == "stripe.com"
    assert domain_of("https://en.wikipedia.org/wiki/Payment_gateway") == "wikipedia.org"


def test_citations_deduplicated_and_ordered():
    rec = chatgpt("Stripe.", ["https://stripe.com/a?utm_medium=x", "https://stripe.com/a", "https://g2.com/x",
                              "not a url"])
    cites = P.parse("chatgpt", rec).citations
    assert [(c.ordinal, c.domain) for c in cites] == [(1, "stripe.com"), (2, "g2.com")]


def test_grok_shape():
    rec = {"url": "https://grok.com/", "prompt": Q, "input": {"url": "https://grok.com/", "prompt": Q},
           "response": {"text": "1. **Stripe**: docs.", "web_results": [{"url": "https://stripe.com"}],
                        "x_posts": [{"url": "https://x.com/stripe/status/1"}]}}
    out = P.parse("grok", rec)
    assert out.status == "ok" and [c.domain for c in out.citations] == ["stripe.com", "x.com"]
