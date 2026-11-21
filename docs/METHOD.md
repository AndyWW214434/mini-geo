# Insights method

Code: `backend/geo/insights/detect.py`. Output: `mart.insights` (one batch per run), read by the report.

## 1. Data problems before trends

For every platform and day, `mart.daily_platform_health` holds how many answers were real
(`ok`) versus errors, empty, blocked pages (`not_answer`), cut off (`truncated`) or answers to a
different prompt (`wrong_prompt`).

A platform-day is a **data issue** when its real-answer rate is below 75 %, or more than 12 points
under that platform's median. Consecutive flagged days become one issue, labelled with the dominant
failure. Flagged days are reported (report §4) and **excluded from change detection on that platform**.

Why: when a scraper starts returning login walls, a naive count shows every brand's visibility
collapsing on the same day. Excluding the junk answers fixes the level, but the remaining sample is
small and biased toward whatever got through, so those days are dropped rather than trusted.

## 2. Change detection

Grain: (platform, prompt category, brand). Category matters because changes usually live in one
category ("billing on ChatGPT") and are diluted in the brand-level number.

For each series of clean days with counts k_d (answers naming the brand) and n_d (real answers):

1. **Change day.** Choose the split t (at least 4 days each side) that maximises the two-segment
   binomial log-likelihood over the one-segment model.
2. **Size.** |after − before| ≥ 10 points.
3. **Significance.** Two-proportion z-test on the pooled before/after counts. The threshold is
   Bonferroni-corrected for the number of series tested (0.05 / 138 ≈ 3.6e-4 on this data).
4. **Persistence.** At least 70 % of days on each side sit on their side of the midpoint between
   the two rates. This rejects a short spike that the likelihood search would otherwise latch onto.
5. **Breadth.** The rate moved by at least 5 points in the same direction on at least 2 separate
   prompts. A change driven by a single prompt is fragile and often a prompt-specific artefact.

**Confidence**
- *High*: p below threshold / 100, persistence ≥ 85 %, ≥ 3 prompts, and no data issue within 2 days
  of the change day.
- *Medium*: passes the reporting bar (p below threshold, persistence ≥ 70 %, ≥ 2 prompts).
- *Low*: large and significant but not persistent or broad: listed under "also seen", not acted on.

Known limits: the split search makes the nominal p-value optimistic (it is the best of ~20 splits);
the size, persistence and breadth gates are what keep false positives down. One change per series
in the window; a change and a reversal within 30 days would be reported as the larger move.

## 3. Why it changed

For the platform and category of each change, citation rates per domain before vs after the change
day (from `mart.daily_citations`, same clean days). Domains moving by ≥ 10 points are listed, labelled
`started` / `stopped` / `up` / `down`, with the page on that domain whose rate moved most.

To separate cause from coincidence, for the top domains we also compare, after the change, how often
the brand is named in answers that cite the domain versus answers that don't. Only a gap of ≥ 10 points,
in the direction of the change and significant (two-proportion p < 0.01), is reported as tying the
citation to the brand within single answers. Otherwise the report says the site and the change are
co-timed, not proven cause.

## 4. Validation on the synthetic history

The generator plants two real changes, one control platform and two data problems:

| Planted | Detected |
|---|---|
| Perplexity: Adyen up in payments and payouts from day 18 (9 Nov), with adyen.com and a G2 comparison newly cited | yes, both categories, 9 Nov, high |
| ChatGPT: Stripe down / Paddle up in billing from day 21 (12 Nov), Paddle MoR page and a Reddit thread cited, docs.stripe.com/billing less | yes, 12 Nov, high |
| Gemini: noise only | no finding |
| Grok: 2 days of login walls / blocks (2–3 Nov) | data issue, excluded |
| Google AI Mode: ~32 % cut-off answers from 16 Nov | data issue (16–19 Nov), excluded |

No false positives across the 138 series tested. In the synthetic data, citations are drawn
independently of brand mentions within an answer, so the within-answer test should report every
citation shift as co-timed. An earlier version reported a 12-point gap for paddle.com as causal;
with ~50 answers not citing paddle.com that gap is noise, which is why the significance check exists.
