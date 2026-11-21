# Mini GEO

Tracks how AI assistants mention and recommend brands. Every 15 minutes it asks ChatGPT,
Perplexity, Gemini, Google AI Mode, Microsoft Copilot and Grok 20 payments questions through
Bright Data, stores the raw answers, parses brand mentions, positions and citations, computes
daily visibility in SQL, finds the changes that matter, and writes a report for Stripe. A
dashboards app (Next.js + FastAPI + WorkOS) lets users build charts by asking an AI chat that
reaches the data only through an MCP server.

- Design, failure modes, scale and cost: [ARCHITECTURE.md](ARCHITECTURE.md)
- Insights method: [docs/METHOD.md](docs/METHOD.md)
- Decisions, cuts and data notes: [NOTES.md](NOTES.md)
- The Stripe report: [reports/stripe-report.html](reports/stripe-report.html), also served live at `GET /reports/stripe`

## Layout

```
data/                 prompts.csv, brands.csv, history.jsonl.gz (synthetic, see NOTES.md)
backend/geo/
  platforms/          one adapter per AI platform (record shape, citations, quirks)
  collection/         Bright Data and synthetic answer sources
  workflows/          Temporal workflows, activities, schedules, worker
  parsing/            run status, brand mentions + positions, citation URLs (versioned)
  db/migrations/      schemas and the set-based SQL transforms
  etl/pipeline.py     load raw, parse, refresh metrics (incremental, idempotent)
  insights/           change detection with data-quality gating
  report/             report builder + template (every number from the marts)
  dashboards/         chart specs, metric registry, org-scoped repository
  mcp_server/         MCP server (tools for the chat), mounted at /mcp/
  chat/               Claude tool loop over an MCP session opened as the user
  api/                FastAPI app and WorkOS session verification
backend/tests/        parser (hand-labelled), workflow exactly-once, API/MCP auth and org isolation
web/                  Next.js app (AuthKit sign-up/login, dashboards, chat)
render.yaml           infrastructure as code (Render Blueprint)
.github/workflows/    CI: lint, bootstrap + double pipeline run, tests, web typecheck + build
```

## Run locally

Requirements: Python 3.12+, Node 22, Docker.

```bash
docker run -d --name geo-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=geo -p 54329:5432 postgres:16-alpine
```

```bash
cd backend && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

```bash
cd backend && .venv/bin/python -m geo.cli bootstrap
```

`bootstrap` migrates, seeds the catalog, and on an empty database loads the 30-day history,
runs the pipeline, detects insights and stores the report. It is idempotent and runs on every
worker start.

Collection needs a Temporal server (`temporal server start-dev`, or Temporal Cloud via env vars):

```bash
cd backend && .venv/bin/python -m geo.workflows.worker
```

API (with `WORKOS_CLIENT_ID`, `WORKOS_API_KEY`, `ANTHROPIC_API_KEY` in `backend/.env`; see `.env.example`):

```bash
cd backend && .venv/bin/uvicorn geo.api.main:app --reload
```

Web (see `web/.env.example`):

```bash
cd web && npm install && npm run dev
```

Tests (the integration tests need a migrated database):

```bash
cd backend && TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:54329/geo_test .venv/bin/pytest -q
```

Useful commands: `geo.cli pipeline` (parse + refresh), `geo.cli insights`, `geo.cli report --brand stripe --out report.html`,
`geo.cli schedule` (create/update the Temporal schedules).

## Deploy (Render + Supabase + Temporal Cloud + WorkOS)

1. **Supabase**: create a project; copy the *Session pooler* connection string (IPv4) as `DATABASE_URL`.
2. **Temporal Cloud**: create a namespace and an API key; note `TEMPORAL_ADDRESS`
   (`<namespace>.<account>.tmprl.cloud:7233`) and `TEMPORAL_NAMESPACE`.
3. **WorkOS**: enable AuthKit. Under Redirects set the redirect URI
   `https://<web>/callback`, sign-out URI `https://<web>/`, initiate-login URI `https://<web>/sign-in`.
   Under Roles keep `admin` and `member`. Copy the client id and API key.
4. **Render**: New → Blueprint → this repo. Fill the `sync: false` variables:
   `DATABASE_URL`, `WORKOS_*`, `TEMPORAL_*`, `BRIGHTDATA_API_TOKEN` (optional), `ANTHROPIC_API_KEY`,
   `PUBLIC_API_URL`, `CORS_ORIGINS`, `API_URL`, `NEXT_PUBLIC_WORKOS_REDIRECT_URI`.
5. The worker bootstraps the database and creates both Temporal schedules on start. Check
   `https://<api>/status` after 15 minutes: every platform should show a new collected window.

Secrets live only in Render's environment and local `.env` files (git-ignored).

## Operating

- **Is collection keeping up?** `GET /status`: last collected window, runs and failures in the last 24 h
  per platform, last error, and the latest run of each pipeline step. The Temporal UI shows every workflow.
- **Fix the parser:** change the code, bump `PARSER_VERSION` in `geo/parsing/parser.py`, deploy. The
  pipeline parses history under the new version and switches the marts when it has caught up.
- **Backfill or re-collect a window:** start `CollectWindow` with `{"window_start": "<iso>"}`; windows
  already collected are skipped.
