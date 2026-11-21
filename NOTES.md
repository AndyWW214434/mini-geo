# Notes

## Constraints on this build

- **No starter data.** `prompts.csv`, `brands.csv` and `history.jsonl` were not provided, so I wrote
  the CSVs from the brief and built a generator (`geo/synthetic/generator.py`) for 30 days of
  answers in Bright Data's record shapes, 4 runs a day (14,489 records, 89 of them duplicates). Every
  synthetic record carries `"_synthetic": true` and is stored with `source = 'synthetic'`; the report
  says so at the top. The generator plants known changes and outages, which turned the history into a
  test of the parser and the insights method (results in `docs/METHOD.md`).
- **No cloud account.** Deployed on Render instead of Azure/AWS (see ARCHITECTURE.md, decision 11).
  `render.yaml` is the infrastructure as code; the backend is one Docker image.
- **Bright Data.** Adapters and the client follow Bright Data's dataset API (trigger → progress →
  snapshot). Dataset ids per platform are defaults in the adapters and overridable per environment in
  `config.platforms.settings`; verify them in the Bright Data console. Without `BRIGHTDATA_API_TOKEN`
  the same workflows run against the synthetic source, so the 15-minute schedule still exercises the
  whole path, and every row says where it came from.

## Decisions worth defending

- Exactly-once per window = Temporal child workflow id `collect/<platform>/<window>` with
  `REJECT_DUPLICATE` **and** unique keys in Postgres. The second guard survives Temporal retention.
- Overlap allowed, deadline per run, no late back-fill: an answer collected an hour late is not that
  window's answer.
- `raw.snapshots` keeps Bright Data's response byte-for-byte; `raw.answers` keeps each record as jsonb
  for querying. Parsing never writes to `raw`.
- Metrics recompute whole days from the parsed layer rather than adding deltas: idempotent by
  construction, and a day's numbers are always explainable from `parsed`.
- Visibility is NULL without real answers; data-problem days are excluded from change detection and
  reported separately.
- The chat model is Claude Opus 5.5 with adaptive thinking at medium effort, plus server-side refusal
  fallbacks. The loop is a plain Messages API tool loop in the API rather than a hosted agent, because it
  has to hold the user's session token to call the MCP server as that user.

## What I cut

- Hand-labelled set: I wrote 23 labelled parser cases covering each failure type and platform shape,
  not a large labelled corpus. Truncation detection is heuristic: on the Google AI Mode days with
  ~32 % cut-off answers the parser flags 24.8 %, so recall is roughly 75 %. Mid-word cuts inside short
  list items are the misses.
- Position for prose answers is first-mention order, which is approximate.
- Brand mention sentiment (named favourably or as a warning) is not modelled.
- No row-level security in Postgres; org isolation is enforced in the repository layer (every query
  filters on the token's org) and tested. RLS would be the next layer of defence.
- No partitioning or archival of `raw` (needed well before 100×, see ARCHITECTURE.md).
- Report is a hosted HTML page (print to PDF from the browser); no PDF renderer in the image.
- Chat history is kept in the browser for the session, not persisted.

## What I noticed in the data

- 94 % of answers are real. Failures by type: scraper errors 350, blocked/login/HTML pages 216,
  empty 151, cut off 136, answer to a different prompt 47.
- Two data problems would read as market moves to a naive pipeline: Grok returned login walls and
  blocks for 2 days (22 % real answers), and Google AI Mode started cutting answers off on 16 Nov.
- Copilot cites through Bing redirect links and Google AI Mode through `google.com/url?q=` links;
  without unwrapping, both platforms would show bing.com / google.com as their top cited domain.
- "PayPal Braintree" is a Braintree mention, not PayPal; "checkout", "wise", "sift", "square" appear as
  ordinary words. Both are handled (longest alias wins; strict case-sensitive aliases) and tested.
- Stripe leads 23 of 24 assistant-category pairs in the last week. The exception is billing on
  ChatGPT, where Paddle took the lead from 12 Nov. On Perplexity, Stripe is still named as often, but
  Adyen now comes first in payments and payouts answers.

## Timeline (from commits, PST)

| Stage | First working |
|---|---|
| Parsing + SQL ETL producing daily metrics | 13:47 |
| Collection workflows + schedule, insights, report | 13:55 |
| API + MCP + chat (tested) | 14:00 |
| Web app | 14:08 |
| Deployment (Render) | see the git log and run history |
