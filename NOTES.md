# Notes

## Live

| What | Where |
|---|---|
| Dashboards app (sign up, ask the chat) | https://geo-web-yqux.onrender.com |
| Stripe report (hosted) | https://geo-api-91rz.onrender.com/reports/stripe |
| Are the 15-minute runs keeping up? | https://geo-api-91rz.onrender.com/status |
| Static copy of the report | [reports/stripe-report.html](reports/stripe-report.html) |

## Constraints on this build

- **No starter data.** `prompts.csv`, `brands.csv` and `history.jsonl` were not provided, so I wrote
  the CSVs from the brief and built a generator (`geo/synthetic/generator.py`) for 30 days of answers
  in Bright Data's record shapes, 4 runs a day (14,489 records, 89 of them duplicates). Every synthetic
  record carries `"_synthetic": true` and is stored with `source = 'synthetic'`; the report says so at
  the top. The generator plants known changes and outages, which turned the history into a test of the
  parser and the insights method (results in `docs/METHOD.md`).
- **No cloud account.** Deployed on Render instead of Azure/AWS (ARCHITECTURE.md, decision 11).
  `render.yaml` is the infrastructure as code; the backend is one Docker image.
- **Bright Data, live since 16:45 PST on 21 Nov.** Collection runs on Bright Data's AI scrapers for
  ChatGPT, Gemini and Google AI Mode. Copilot has no scraper in Bright Data's library for this account;
  "Grok" there is Grokipedia (an encyclopedia collected by URL, not the assistant answering prompts);
  Bright Data's Perplexity scraper currently fails upstream (`wait_element_timeout` after ~30 minutes,
  recorded by us as an `error` answer). All three are switched off in `config.platforms`
  (`active = false`): one row each, no code change, re-enabled with one update when the scraper works.
- **Claude** is not on the brief's list. With a Bright Data prompt scraper it would be one adapter class;
  through the Anthropic API it would measure the API with web search, not claude.ai, at a real cost.

## Real answers vs synthetic history

The 30-day history is synthetic; live answers are real. They are never blended: every parsed answer
carries its source, and `parsed.metric_answers` (active parser version + `config.metrics_sources`) is
the only definition of the answers that count for metrics, insights, the report and the dashboards.
The marts stay on the synthetic history until there are enough real days for change detection (at
least 8). Switching is one statement, which recomputes every day from real answers only:

```sql
select ops.set_metrics_sources(array['brightdata']);
```

## What the first real answers showed (and what changed because of them)

- **ChatGPT numbers its lists `1\.` (markdown-escaped).** The ranked list was missed, and a detail
  bullet under item 4 ("Useful for marketplaces where PayPal adoption matters") made PayPal rank #1 on
  an answer that recommends Stripe Connect first. Parser 1.3.0 accepts escaped numbers and ranks by the
  top-level structure (numbered items and headings, then tables, then bullets).
- **Gemini ends with follow-up suggestion chips** ("Compare Stripe Connect Express vs. Custom
  accounts"), which looked like a cut-off answer. A closing run of short options after a colon is now
  treated as complete.
- **Field names differ from what I modelled:** Gemini and Google AI Mode answers are read from
  `answer_text_markdown`; AI Mode citations are in `citations`, not `aio_citations`.
- **Records carry 1–4 MB page dumps** (`answer_html`, `response_raw`): about 20 GB a day at this
  cadence. String fields over 100 KB are replaced by their size and SHA-256 at ingest; snapshots keep
  the original size and hash (migration 006). In production those dumps would go to object storage.
- **Perplexity's scraper failed** after ~30 minutes on a single prompt. The parser classified the record
  as `error`; the 50-minute run deadline would have recorded slower runs as `timed_out`. A slow platform
  also delays parsing for the whole window, because parsing runs when all platforms finish; parsing per
  platform as soon as its snapshot lands is the next change.

The three real records are labelled fixtures in `backend/tests/fixtures/brightdata/`. Parser 1.3.0
re-parsed the whole history and the marts switched over automatically; the insights are unchanged
(same four changes, same two data problems).

## Decisions worth defending

- Exactly-once per window = Temporal child workflow id `collect/<platform>/<window>` with
  `REJECT_DUPLICATE` **and** unique keys in Postgres. The second guard survives Temporal retention.
- Overlap allowed, deadline per run, no late back-fill: an answer collected an hour late is not that
  window's answer.
- Metrics recompute whole days from the parsed layer rather than adding deltas: idempotent by
  construction, and a day's numbers are always explainable from `parsed`.
- Visibility is NULL without real answers; data-problem days are excluded from change detection and
  reported separately.
- The report's recommendations are only actions the client can take, each with a success metric;
  collection problems are ours and sit under data quality.
- The chat model is Claude Opus 5.5 at medium effort, with server-side refusal fallbacks. The loop is a
  plain Messages API tool loop in the API because it must hold the user's session token to call the
  MCP server as that user.

## Deployment fixes found on the way (all in git)

- `render.yaml`: Render ignores `sync: false` inside env groups (the secrets would have been dropped),
  and `starter` is no longer a plan id. Secrets are now declared per service.
- `CORS_ORIGINS` crashed the API at startup unless written as a JSON list; it now accepts a plain URL,
  a comma-separated list or JSON.
- Behind Render's proxy the AuthKit callback saw `localhost:10000` and redirected there after sign-in;
  redirects are now built from the public URL.

## What I cut

- Hand-labelled set: 23 synthetic cases plus 3 real records, not a large labelled corpus. Truncation
  detection is heuristic: on the synthetic Google AI Mode days with ~32 % cut-off answers the parser
  flags 24.8 %, so recall is roughly 75 %.
- Position for prose answers is first-mention order, which is approximate.
- Brand mention sentiment (named favourably or as a warning) is not modelled.
- No row-level security in Postgres; org isolation is enforced in the repository layer (every query
  filters on the token's org) and tested. RLS would be the next layer of defence.
- No partitioning or archival of `raw` (needed well before 100×, see ARCHITECTURE.md).
- Report is a hosted HTML page (print to PDF from the browser).
- Chat history is kept in the browser for the session, not persisted.

## What I noticed in the synthetic data

- 94 % of answers are real. Failures by type: scraper errors 350, blocked/login/HTML pages 216,
  empty 151, cut off 136, answer to a different prompt 47.
- Two data problems would read as market moves to a naive pipeline: Grok returned login walls and
  blocks for 2 days (22 % real answers), and Google AI Mode started cutting answers off on 16 Nov.
- Copilot cites through Bing redirect links and Google AI Mode through `google.com/url?q=` links;
  without unwrapping, both would show bing.com / google.com as their top cited domain.

## Timeline (PST, from commits and run history)

| Stage | First working |
|---|---|
| Parsing + SQL ETL producing daily metrics | 13:47 |
| Collection workflows + schedule, insights, report | 13:55 |
| API + MCP + chat (tested) | 14:00 |
| Web app | 14:08 |
| Deployed on Render; first scheduled 15-minute window collected | 15:30 |
| Sign-up through WorkOS on the live app | 15:40 |
| Bright Data verified live; parser 1.3.0 | 16:25 |
| Real collection on the schedule | 16:45 |
