"""Synthetic AI-assistant answers in the shape Bright Data's AI scrapers return.

No starter `history.jsonl` was provided, so this module produces one. It is
also the optional live source when no Bright Data token is configured. Every
record carries `"_synthetic": true`; the loader stores that as `source =
'synthetic'`, so synthetic rows can never be mistaken for real collections.

The data is deliberately messy, the way real scraper output is:
  * six platforms, six record shapes (field names, citation formats, redirect URLs)
  * scraper errors, empty answers, login walls / captchas / HTML pages
  * cut-off answers, answers to the wrong prompt, duplicated deliveries
  * brand names written through aliases, plus common words that look like brands

It also contains planted ground truth (see SHIFTS and OUTAGES) so the insights
stage can be checked: two real changes, one control platform with no change,
and two data problems that must not be reported as trends.
"""

from __future__ import annotations

import argparse
import base64
import json
import random
import zlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from geo.catalog import Brand, Prompt, load_brands, load_prompts

PLATFORMS = ("chatgpt", "perplexity", "gemini", "google_ai_mode", "copilot", "grok")

# Base probability that an answer to a prompt in a category names the brand.
BASE: dict[str, dict[str, float]] = {
    "payments": {"stripe": 0.90, "adyen": 0.45, "braintree": 0.45, "paypal": 0.55, "square": 0.40,
                 "checkout": 0.35},
    "billing": {"stripe": 0.85, "paddle": 0.45, "chargebee": 0.60, "recurly": 0.40, "lemonsqueezy": 0.30},
    "payouts": {"stripe": 0.85, "paypal": 0.60, "adyen": 0.30, "tipalti": 0.50, "payoneer": 0.45,
                "wise": 0.40},
    "fraud": {"stripe": 0.80, "sift": 0.55, "signifyd": 0.50, "riskified": 0.40, "adyen": 0.30,
              "checkout": 0.25},
}

# Small, stable per-platform tilts so assistants differ from each other.
TILT: dict[str, dict[str, float]] = {
    "perplexity": {"adyen": -0.05, "checkout": 0.05},
    "gemini": {"paypal": 0.10, "square": 0.08},
    "google_ai_mode": {"paypal": 0.08, "square": 0.10, "stripe": -0.05},
    "copilot": {"paypal": 0.05, "braintree": 0.05},
    "grok": {"stripe": -0.08, "wise": 0.08},
}


@dataclass(frozen=True)
class Shift:
    """A real, planted change in how often a platform names a brand."""

    platform: str
    brand: str
    categories: tuple[str, ...]
    start_day: int  # day index from the start of history
    p_after: float
    rank_boost: float


@dataclass(frozen=True)
class CitationShift:
    platform: str
    categories: tuple[str, ...]
    start_day: int
    url: str
    p_before: float
    p_after: float


SHIFTS = (
    # Perplexity starts recommending Adyen far more for payments and payouts.
    Shift("perplexity", "adyen", ("payments", "payouts"), 18, 0.78, 0.35),
    # ChatGPT moves billing answers from Stripe toward Paddle (merchant of record).
    Shift("chatgpt", "stripe", ("billing",), 21, 0.50, -0.45),
    Shift("chatgpt", "paddle", ("billing",), 21, 0.85, 0.60),
)

CITATION_SHIFTS = (
    CitationShift("perplexity", ("payments", "payouts"), 18,
                  "https://www.adyen.com/knowledge-hub/marketplace-payments-guide", 0.02, 0.62),
    CitationShift("perplexity", ("payments", "payouts"), 18,
                  "https://www.g2.com/compare/adyen-vs-stripe", 0.05, 0.50),
    CitationShift("chatgpt", ("billing",), 21,
                  "https://www.paddle.com/resources/merchant-of-record", 0.04, 0.64),
    CitationShift("chatgpt", ("billing",), 21,
                  "https://www.reddit.com/r/SaaS/comments/1gq7x2k/stripe_billing_vs_paddle_after_tax_changes/",
                  0.02, 0.45),
    CitationShift("chatgpt", ("billing",), 21, "https://docs.stripe.com/billing", 0.70, 0.18),
)


@dataclass(frozen=True)
class Outage:
    """A data problem, not a trend: the scraper returns junk for a while."""

    platform: str
    start_day: int
    end_day: int  # inclusive; None-like large value means "until now"
    kind: str  # "blocked" | "truncated"
    rate: float


OUTAGES = (
    Outage("grok", 11, 12, "blocked", 0.78),
    Outage("google_ai_mode", 25, 10_000, "truncated", 0.32),
)

# Citation pools per category: (url, base probability).
CITATIONS: dict[str, list[tuple[str, float]]] = {
    "payments": [
        ("https://docs.stripe.com/connect", 0.55), ("https://stripe.com/payments", 0.35),
        ("https://www.nerdwallet.com/article/small-business/best-payment-gateways", 0.30),
        ("https://www.forbes.com/advisor/business/best-payment-gateways/", 0.28),
        ("https://www.adyen.com/payments", 0.15), ("https://www.braintreepayments.com/", 0.18),
        ("https://developer.paypal.com/docs/checkout/", 0.20), ("https://squareup.com/us/en/payments", 0.15),
        ("https://www.reddit.com/r/ecommerce/comments/18xk2pa/which_payment_gateway/", 0.18),
        ("https://en.wikipedia.org/wiki/Payment_gateway", 0.12),
    ],
    "billing": [
        ("https://docs.stripe.com/billing", 0.0),  # driven by CITATION_SHIFTS
        ("https://www.chargebee.com/subscription-billing/", 0.40),
        ("https://www.g2.com/categories/subscription-billing", 0.30),
        ("https://www.paddle.com/billing", 0.20), ("https://recurly.com/", 0.18),
        ("https://www.lemonsqueezy.com/", 0.12), ("https://stripe.com/tax", 0.25),
        ("https://www.capterra.com/subscription-billing-software/", 0.20),
    ],
    "payouts": [
        ("https://docs.stripe.com/connect/payouts-connected-accounts", 0.50),
        ("https://www.tipalti.com/mass-payments/", 0.35), ("https://www.payoneer.com/", 0.28),
        ("https://wise.com/gb/business/api", 0.25), ("https://developer.paypal.com/docs/payouts/", 0.30),
        ("https://www.reddit.com/r/startups/comments/1b2c3d4/paying_contractors_globally/", 0.15),
        ("https://www.adyen.com/platform-payments", 0.10),
    ],
    "fraud": [
        ("https://stripe.com/radar", 0.50), ("https://docs.stripe.com/disputes/prevention", 0.35),
        ("https://sift.com/", 0.35), ("https://www.signifyd.com/", 0.30), ("https://www.riskified.com/", 0.22),
        ("https://www.chargebackgurus.com/blog/card-testing", 0.15),
        ("https://www.checkout.com/fraud-detection", 0.12),
    ],
}

DESCRIPTIONS = {
    "payments": ["broad API coverage and strong developer docs", "good fit for enterprise volume with "
                 "interchange++ pricing", "fast to integrate with hosted checkout", "widely trusted by "
                 "shoppers at checkout", "strong in-person and online payments", "competitive "
                 "cross-border acquiring"],
    "billing": ["handles subscriptions, invoicing and proration", "acts as merchant of record so it "
                "files sales tax and VAT for you", "flexible usage-based pricing models", "strong dunning "
                "and revenue recovery", "simple setup for indie software sellers"],
    "payouts": ["pays out to bank accounts in 100+ countries", "handles seller KYC and onboarding",
                "supports instant payouts to debit cards", "built for mass payouts to contractors",
                "low-cost FX on international transfers"],
    "fraud": ["machine-learning scoring trained on a large network", "chargeback guarantee on "
              "approved orders", "custom rules plus ML risk scores", "blocks card testing with velocity "
              "rules", "manual review queue for risky orders"],
}

INTROS = {
    "payments": ["Here are the strongest options, depending on volume and how you onboard sellers.",
                 "It depends on your volume and geography, but these are the providers most teams compare."],
    "billing": ["These platforms cover subscriptions, invoicing and tax for software companies.",
                "Most B2B SaaS teams pick from a short list. Here is how they compare."],
    "payouts": ["For multi-country payouts, these are the providers worth evaluating.",
                "Paying people across borders is mostly about coverage, KYC and FX costs."],
    "fraud": ["The right choice depends on whether you want rules, ML scoring or a chargeback guarantee.",
              "These are the tools merchants use most to cut fraud and chargebacks."],
}

# Ordinary words that look like brand names. A good parser must not count them.
DECOYS = [
    "Make sure your checkout page loads fast on mobile.",
    "It is wise to test the full flow in sandbox mode first.",
    "You will want to sift through dispute reasons each month.",
    "Keep the fee math fair and square for your sellers.",
    "Don't paddle against your existing tax setup; reuse what works.",
]

BLOCK_PAGES = {
    "chatgpt": "You've reached our limit of messages. Please try again later.",
    "perplexity": "Verify you are human by completing the action below. perplexity.ai needs to review "
                  "the security of your connection before proceeding.",
    "gemini": "Sign in to continue to Gemini",
    "google_ai_mode": "<!DOCTYPE html><html><head><title>Before you continue to Google</title></head>"
                      "<body>We use cookies and data to deliver and maintain Google services</body></html>",
    "copilot": "Sorry, it looks like you've been signed out. Sign in to continue chatting.",
    "grok": "Sign in to X to continue. Grok is available to logged-in users.",
}

ERRORS = [
    ("timeout", "Timed out waiting for the answer to finish rendering"),
    ("dead_page", "Page did not load: net::ERR_CONNECTION_RESET"),
    ("blocked", "Request was blocked by the target site"),
]


def history_start(end: date, days: int) -> date:
    return end - timedelta(days=days - 1)


class Generator:
    def __init__(self, brands: list[Brand], prompts: list[Prompt], start: date, seed: int = 7):
        self.brands = {b.brand_id: b for b in brands}
        self.prompts = prompts
        self.start = start
        self.seed = seed

    def _rng(self, *key: object) -> random.Random:
        return random.Random(zlib.crc32(repr((self.seed, *key)).encode()))

    def day_index(self, when: datetime) -> int:
        return (when.date() - self.start).days

    # ---- what the answer says -------------------------------------------------------------
    def _brands_for(self, rng: random.Random, platform: str, category: str, day: int) -> list[str]:
        scores: list[tuple[float, str]] = []
        for brand_id, p in BASE[category].items():
            p = p + TILT.get(platform, {}).get(brand_id, 0.0)
            boost = 0.0
            for s in SHIFTS:
                if s.platform == platform and s.brand == brand_id and category in s.categories and day >= s.start_day:
                    p, boost = s.p_after, s.rank_boost
            if rng.random() < max(0.0, min(1.0, p)):
                scores.append((p + boost + rng.gauss(0, 0.12), brand_id))
        scores.sort(reverse=True)
        return [b for _, b in scores]

    def _citations_for(self, rng: random.Random, platform: str, category: str, day: int) -> list[str]:
        urls = []
        for url, p in CITATIONS[category]:
            for cs in CITATION_SHIFTS:
                if cs.platform == platform and category in cs.categories and cs.url == url:
                    p = cs.p_after if day >= cs.start_day else cs.p_before
            if rng.random() < p:
                urls.append(url)
        for cs in CITATION_SHIFTS:
            if cs.platform == platform and category in cs.categories and cs.url not in urls:
                if not any(cs.url == u for u, _ in CITATIONS[category]):
                    if rng.random() < (cs.p_after if day >= cs.start_day else cs.p_before):
                        urls.append(cs.url)
        rng.shuffle(urls)
        return urls[:6]

    def _alias(self, rng: random.Random, brand_id: str) -> str:
        b = self.brands[brand_id]
        options = [b.name, b.name, *b.aliases, *b.strict_aliases]
        return rng.choice(options)

    def _render(self, rng: random.Random, platform: str, prompt: Prompt, ranked: list[str],
                cites: list[str]) -> str:
        cat = prompt.category
        lines = [rng.choice(INTROS[cat])]
        if ranked and rng.random() < 0.18:
            lines[0] = f"{self.brands[ranked[0]].name} is the default for many teams, but it depends. " + lines[0]
        if rng.random() < 0.35:
            lines.append(rng.choice(DECOYS))
        fmt = rng.choice(["numbered", "numbered", "bullets", "headings", "table", "prose"])
        descs = DESCRIPTIONS[cat]
        marker = (lambda i: f" [{i + 1}]") if platform == "perplexity" and cites else (lambda i: "")
        if fmt == "numbered":
            for i, b in enumerate(ranked):
                lines.append(f"{i + 1}. **{self._alias(rng, b)}**: {rng.choice(descs)}.{marker(i % max(1, len(cites)))}")
        elif fmt == "bullets":
            for i, b in enumerate(ranked):
                lines.append(f"- {self._alias(rng, b)} – {rng.choice(descs)}.{marker(i % max(1, len(cites)))}")
        elif fmt == "headings":
            for i, b in enumerate(ranked):
                lines.append(f"### {i + 1}. {self._alias(rng, b)}\n{rng.choice(descs).capitalize()}.")
        elif fmt == "table":
            lines.append("| Provider | Best for |\n|---|---|")
            for b in ranked:
                lines.append(f"| {self._alias(rng, b)} | {rng.choice(descs)} |")
        else:
            for i, b in enumerate(ranked):
                lead = "The strongest choice is" if i == 0 else rng.choice(["Another option is", "Also consider",
                                                                             "Teams also use"])
                lines.append(f"{lead} {self._alias(rng, b)}, which offers {rng.choice(descs)}.")
        if ranked and platform == "chatgpt" and rng.random() < 0.3:
            b = self.brands[ranked[0]]
            lines.append(f"See the [{b.name} docs](https://{b.domains[0]}/docs) for setup details.")
        if ranked:
            lines.append(f"Bottom line: start with {self.brands[ranked[0]].name} unless you have a specific "
                         "reason not to.")
        return "\n\n".join(lines)

    # ---- record shapes per platform -------------------------------------------------------
    @staticmethod
    def _bing(url: str) -> str:
        enc = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
        return f"https://www.bing.com/ck/a?!&&p=3f9c1d2e7a&ptn=3&ver=2&hsh=4&u=a1{enc}&ntb=1"

    @staticmethod
    def _google(url: str) -> str:
        return f"https://www.google.com/url?q={quote(url, safe='')}&sa=U&ved=2ahUKEwj&usg=AOvVaw"

    def _shape(self, platform: str, prompt_text: str, text: str, cites: list[str], ts: str,
               truncated_flag: bool | None) -> dict:
        inp = {"url": PLATFORM_URLS[platform], "prompt": prompt_text}
        plain = text.replace("**", "").replace("### ", "")
        if platform == "chatgpt":
            return {"url": inp["url"], "prompt": prompt_text, "answer_text": plain, "answer_text_markdown": text,
                    "citations": [{"url": u, "title": _title(u), "cited": True} for u in cites],
                    "links_attached": [], "model": "gpt-5", "web_search_triggered": bool(cites),
                    "timestamp": ts, "input": inp}
        if platform == "perplexity":
            return {"url": inp["url"], "prompt": prompt_text, "answer_text": text,
                    "sources": [{"link": u, "title": _title(u)} for u in cites],
                    "related_prompts": [], "timestamp": ts, "input": inp}
        if platform == "gemini":
            return {"url": inp["url"], "prompt": prompt_text, "answer_text": plain, "response_markdown": text,
                    "links": cites, "model": "gemini-2.5-pro", "timestamp": ts, "input": inp}
        if platform == "google_ai_mode":
            rec = {"url": f"https://www.google.com/search?udm=50&q={quote(prompt_text)}", "prompt": prompt_text,
                   "aio_text": plain,
                   "aio_citations": [{"url": self._google(u), "title": _title(u)} for u in cites],
                   "timestamp": ts, "input": inp}
            if truncated_flag is not None:
                rec["is_truncated"] = truncated_flag
            return rec
        if platform == "copilot":
            return {"url": inp["url"], "prompt": prompt_text, "answer_text": text,
                    "sources": [{"url": self._bing(u), "title": _title(u)} for u in cites],
                    "timestamp": ts, "input": inp}
        if platform == "grok":
            return {"url": inp["url"], "prompt": prompt_text,
                    "response": {"text": text, "web_results": [{"url": u, "title": _title(u)} for u in cites],
                                 "x_posts": []},
                    "timestamp": ts, "input": inp}
        raise ValueError(platform)

    def record(self, platform: str, prompt: Prompt, window_start: datetime, attempt: int = 0) -> dict:
        """One Bright Data-shaped record for (platform, prompt, window)."""
        rng = self._rng(platform, prompt.prompt_id, window_start.isoformat(), attempt)
        day = self.day_index(window_start)
        ts = (window_start + timedelta(minutes=rng.uniform(1, 12))).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        inp = {"url": PLATFORM_URLS[platform], "prompt": prompt.text}

        outage = next((o for o in OUTAGES if o.platform == platform and o.start_day <= day <= o.end_day), None)
        roll = rng.random()
        if outage and outage.kind == "blocked" and roll < outage.rate:
            if rng.random() < 0.5:
                code, msg = ERRORS[2]
                rec = {"input": inp, "error": msg, "error_code": code, "timestamp": ts}
            else:
                rec = self._shape(platform, prompt.text, BLOCK_PAGES[platform], [], ts, None)
            return {**rec, "_synthetic": True}
        if roll < 0.02:
            code, msg = rng.choice(ERRORS)
            return {"input": inp, "error": msg, "error_code": code, "timestamp": ts, "_synthetic": True}
        if roll < 0.03:
            return {**self._shape(platform, prompt.text, "", [], ts, None), "_synthetic": True}
        if roll < 0.04:
            return {**self._shape(platform, prompt.text, BLOCK_PAGES[platform], [], ts, None), "_synthetic": True}

        answered = prompt
        if roll < 0.043:  # scraper typed a different prompt into the box
            answered = rng.choice([p for p in self.prompts if p.prompt_id != prompt.prompt_id])
        ranked = self._brands_for(rng, platform, answered.category, day)
        cites = self._citations_for(rng, platform, answered.category, day)
        text = self._render(rng, platform, answered, ranked, cites)

        truncated_flag: bool | None = None
        trunc_rate = outage.rate if outage and outage.kind == "truncated" else 0.005
        if rng.random() < trunc_rate:
            cut = int(len(text) * rng.uniform(0.35, 0.7))
            text = text[:cut].rstrip()
            if text and text[-1] in ".!?)":
                text = text[:-1] + " and"
            if platform == "google_ai_mode" and rng.random() < 0.5:
                truncated_flag = True
        elif platform == "google_ai_mode" and rng.random() < 0.5:
            truncated_flag = False
        rec = self._shape(platform, answered.text, text, cites, ts, truncated_flag)
        rec["input"] = inp  # Bright Data echoes what we asked, even if the page answered something else
        return {**rec, "_synthetic": True}


PLATFORM_URLS = {
    "chatgpt": "https://chatgpt.com/",
    "perplexity": "https://www.perplexity.ai/",
    "gemini": "https://gemini.google.com/",
    "google_ai_mode": "https://www.google.com/",
    "copilot": "https://copilot.microsoft.com/",
    "grok": "https://grok.com/",
}


def _title(url: str) -> str:
    host = url.split("/")[2].removeprefix("www.")
    return f"{host} — {url.rstrip('/').split('/')[-1].replace('-', ' ')[:60]}"


def write_history(out: Path, end: date, days: int, runs_per_day: int, seed: int) -> int:
    brands, prompts = load_brands(), load_prompts()
    start = history_start(end, days)
    gen = Generator(brands, prompts, start, seed)
    rng = random.Random(seed)
    n = 0
    step = timedelta(hours=24 / runs_per_day)
    with out.open("w") as f:
        for d in range(days):
            for r in range(runs_per_day):
                window = datetime(start.year, start.month, start.day, tzinfo=UTC) + timedelta(days=d) + r * step
                for platform in PLATFORMS:
                    for prompt in prompts:
                        rec = gen.record(platform, prompt, window)
                        f.write(json.dumps(rec) + "\n")
                        n += 1
                        if rng.random() < 0.005:  # the same answer delivered twice
                            f.write(json.dumps(rec) + "\n")
                            n += 1
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[3] / "data" / "history.jsonl")
    ap.add_argument("--end", type=date.fromisoformat, default=datetime.now(UTC).date() - timedelta(days=1))
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--runs-per-day", type=int, default=4)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    n = write_history(args.out, args.end, args.days, args.runs_per_day, args.seed)
    print(f"wrote {n} records to {args.out}")


if __name__ == "__main__":
    main()
