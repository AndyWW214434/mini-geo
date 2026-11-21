-- Dashboards product. Every row carries the WorkOS organization id; the API takes that id
-- from the verified session token, never from the request body.

create table app.dashboards (
  dashboard_id uuid primary key default gen_random_uuid(),
  org_id       text not null,
  name         text not null,
  description  text,
  created_by   text not null,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);
create index dashboards_org_idx on app.dashboards (org_id, updated_at desc);

create table app.charts (
  chart_id     uuid primary key default gen_random_uuid(),
  dashboard_id uuid not null references app.dashboards on delete cascade,
  org_id       text not null,
  title        text not null,
  spec         jsonb not null,   -- validated ChartSpec: a query over mart tables, not SQL
  position     int not null default 0,
  created_by   text not null,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);
create index charts_dashboard_idx on app.charts (org_id, dashboard_id, position);

-- The AI chat's free-form query tool runs as this role: SELECT on metrics and config only,
-- nothing in app/raw/ops. It cannot read another organization's dashboards even with
-- hand-written SQL.
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'geo_readonly') then
    create role geo_readonly nologin;
  end if;
end $$;
grant usage on schema mart, config to geo_readonly;
grant select on mart.daily_visibility, mart.daily_citations, mart.daily_platform_health to geo_readonly;
grant select on config.brands, config.brand_categories, config.prompts, config.platforms,
                config.categories to geo_readonly;
grant geo_readonly to current_user;
