-- Layers: config (what we track) -> raw (untouched answers) -> parsed (one shape for every
-- platform, versioned) -> mart (daily metrics, insights, reports). ops holds pipeline state,
-- app holds the dashboards product.

create schema if not exists config;
create schema if not exists raw;
create schema if not exists parsed;
create schema if not exists mart;
create schema if not exists ops;
create schema if not exists app;

-- ---------------------------------------------------------------- config
create table config.platforms (
  platform_id text primary key,
  name        text not null,
  active      boolean not null default true,
  settings    jsonb not null default '{}'::jsonb   -- connector settings, e.g. Bright Data dataset id
);

create table config.categories (category text primary key);

create table config.prompts (
  prompt_id  text primary key,
  category   text not null references config.categories,
  text       text not null,
  active     boolean not null default true,
  created_at timestamptz not null default now()
);
create unique index prompts_text_uq on config.prompts (text);

create table config.brands (
  brand_id       text primary key,
  name           text not null,
  aliases        text[] not null default '{}',
  strict_aliases text[] not null default '{}',
  domains        text[] not null default '{}'
);

create table config.brand_categories (
  brand_id text not null references config.brands on delete cascade,
  category text not null references config.categories,
  primary key (brand_id, category)
);

create table config.settings (
  key   text primary key,
  value jsonb not null
);

-- ---------------------------------------------------------------- raw
-- One row per (15-minute window, platform). The unique key is what makes each window
-- collected once: a retried or doubly-triggered workflow finds the existing row.
create table raw.collection_runs (
  run_id            bigint generated always as identity primary key,
  window_start      timestamptz not null,
  platform_id       text not null references config.platforms,
  source            text not null,                      -- brightdata | synthetic
  status            text not null default 'pending'
                    check (status in ('pending', 'triggered', 'collected', 'failed', 'timed_out')),
  snapshot_id       text,
  prompts_requested int not null default 0,
  answers_received  int not null default 0,
  answers_inserted  int not null default 0,
  attempts          int not null default 0,
  error             text,
  workflow_id       text,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  constraint collection_runs_window_uq unique (window_start, platform_id),
  constraint window_aligned check (extract(epoch from window_start)::bigint % 900 = 0)
);

-- The snapshot body exactly as Bright Data returned it (byte-for-byte).
create table raw.snapshots (
  snapshot_id text primary key,
  run_id      bigint references raw.collection_runs,
  body        text not null,
  received_at timestamptz not null default now()
);

-- One row per answer. Payload is the scraper record, untouched; parsing never writes here.
create table raw.answers (
  raw_answer_id  bigint generated always as identity primary key,
  run_id         bigint references raw.collection_runs,
  window_start   timestamptz not null,
  platform_id    text not null references config.platforms,
  prompt_id      text not null references config.prompts,
  source         text not null,                         -- brightdata | synthetic | history
  collected_at   timestamptz not null,
  payload        jsonb not null,
  payload_sha256 text not null,
  inserted_at    timestamptz not null default now(),
  constraint raw_answers_window_uq unique (window_start, platform_id, prompt_id)
);
create index raw_answers_run_idx on raw.answers (run_id);

-- ---------------------------------------------------------------- parsed
create table parsed.answers (
  raw_answer_id  bigint not null references raw.answers on delete cascade,
  parser_version text not null,
  platform_id    text not null,
  prompt_id      text not null,
  category       text not null,
  window_start   timestamptz not null,
  day            date not null,
  status         text not null
                 check (status in ('ok', 'error', 'empty', 'not_answer', 'truncated', 'wrong_prompt')),
  status_detail  text,
  answer_chars   int not null default 0,
  parsed_at      timestamptz not null default now(),
  primary key (raw_answer_id, parser_version)
);
create index parsed_answers_day_idx on parsed.answers (parser_version, day, platform_id);

create table parsed.mentions (
  raw_answer_id  bigint not null,
  parser_version text not null,
  brand_id       text not null references config.brands,
  position       int not null check (position >= 1),
  matched_alias  text not null,
  char_offset    int not null,
  primary key (raw_answer_id, parser_version, brand_id),
  foreign key (raw_answer_id, parser_version) references parsed.answers on delete cascade
);

create table parsed.citations (
  raw_answer_id  bigint not null,
  parser_version text not null,
  ordinal        int not null,
  url            text not null,
  host           text not null,
  domain         text not null,
  primary key (raw_answer_id, parser_version, ordinal),
  foreign key (raw_answer_id, parser_version) references parsed.answers on delete cascade
);
create index parsed_citations_domain_idx on parsed.citations (parser_version, domain);

-- ---------------------------------------------------------------- mart
create table mart.daily_visibility (
  day                date not null,
  platform_id        text not null,
  brand_id           text not null,
  real_answers       int not null,   -- real answers to prompts in the brand's categories
  mentioning_answers int not null,   -- ... of which name the brand
  visibility         numeric,        -- mentioning / real; NULL (not 0) when real_answers = 0
  avg_position       numeric,        -- mean rank among brands named, over mentioning answers
  total_answers      int not null,   -- every answer, real or not (data-quality context)
  parser_version     text not null,
  computed_at        timestamptz not null default now(),
  primary key (day, platform_id, brand_id)
);

create table mart.daily_citations (
  day             date not null,
  platform_id     text not null,
  category        text not null,
  domain          text not null,
  citing_answers  int not null,      -- real answers citing the domain at least once
  citations       int not null,      -- citations of the domain
  real_answers    int not null,
  total_citations int not null,      -- all citations in real answers that day/platform/category
  citation_share  numeric,           -- citations / total_citations
  answer_rate     numeric,           -- citing_answers / real_answers
  parser_version  text not null,
  primary key (day, platform_id, category, domain)
);

create table mart.daily_platform_health (
  day          date not null,
  platform_id  text not null,
  total        int not null,
  ok           int not null,
  error        int not null,
  empty        int not null,
  not_answer   int not null,
  truncated    int not null,
  wrong_prompt int not null,
  windows      int not null,
  parser_version text not null,
  primary key (day, platform_id)
);

create table mart.insights (
  insight_id   bigint generated always as identity primary key,
  batch_id     uuid not null,
  generated_at timestamptz not null default now(),
  kind         text not null,      -- visibility_change | data_issue
  brand_id     text,
  platform_id  text not null,
  category     text,
  change_day   date,
  before_value numeric,
  after_value  numeric,
  delta        numeric,
  before_n     int,
  after_n      int,
  p_value      double precision,
  confidence   text not null check (confidence in ('high', 'medium', 'low')),
  summary      text not null,
  evidence     jsonb not null default '{}'::jsonb
);
create index insights_batch_idx on mart.insights (batch_id);

create table mart.reports (
  report_id    uuid primary key,
  focus_brand  text not null references config.brands,
  generated_at timestamptz not null default now(),
  data_through date not null,
  insights_batch uuid not null,
  html         text not null
);

-- ---------------------------------------------------------------- ops
-- Days whose metrics must be recomputed. Written in the same transaction as parsed rows,
-- drained in the same transaction as the metric refresh: incremental and race-free.
create table ops.dirty_days (
  day      date primary key,
  queued_at timestamptz not null default now()
);

create table ops.pipeline_runs (
  id          bigint generated always as identity primary key,
  step        text not null,
  started_at  timestamptz not null default now(),
  finished_at timestamptz,
  status      text not null default 'running' check (status in ('running', 'succeeded', 'failed')),
  rows_in     int,
  rows_out    int,
  detail      jsonb not null default '{}'::jsonb,
  error       text
);
create index pipeline_runs_step_idx on ops.pipeline_runs (step, started_at desc);

insert into config.settings (key, value) values ('active_parser_version', '"1.2.0"'::jsonb);
