-- Which collected answers count for metrics. Synthetic and real (Bright Data) answers must never
-- be blended in one series: a switch of source would read as a market change. The setting
-- `metrics_sources` picks the sources; parsed.metric_answers is the only definition of
-- "answers that count", and every metric, insight and report query reads through it.

alter table parsed.answers add column source text;
update parsed.answers p set source = r.source from raw.answers r where r.raw_answer_id = p.raw_answer_id;
alter table parsed.answers alter column source set not null;
create index parsed_answers_source_idx on parsed.answers (parser_version, source, day);

insert into config.settings (key, value) values ('metrics_sources', '["synthetic", "history"]'::jsonb)
on conflict (key) do nothing;

create or replace function config.metrics_sources() returns text[]
language sql stable as $$
  select coalesce(array(select jsonb_array_elements_text(value) from config.settings
                        where key = 'metrics_sources'), '{}')
$$;

create or replace view parsed.metric_answers as
select * from parsed.answers
where parser_version = config.active_parser_version()
  and source = any(config.metrics_sources());

-- Switch sources atomically: update the setting and recompute every day that has any answers,
-- so days with no answers from the new sources are cleared rather than left stale.
create or replace function ops.set_metrics_sources(p_sources text[]) returns int
language plpgsql as $$
declare n int;
begin
  update config.settings set value = to_jsonb(p_sources) where key = 'metrics_sources';
  insert into ops.dirty_days (day)
  select distinct day from parsed.answers where parser_version = config.active_parser_version()
  on conflict do nothing;
  get diagnostics n = row_count;
  return n;
end;
$$;

create or replace function mart.refresh_days(p_days date[]) returns jsonb
language plpgsql as $$
declare
  v_version text := config.active_parser_version();
  v_vis int; v_cit int; v_health int;
begin
  if p_days is null or cardinality(p_days) = 0 then
    return jsonb_build_object('days', 0);
  end if;

  delete from mart.daily_visibility     where day = any(p_days);
  delete from mart.daily_citations      where day = any(p_days);
  delete from mart.daily_platform_health where day = any(p_days);

  -- Visibility: for brand B on platform P on day D,
  --   real answers to prompts in B's categories that name B / real answers to prompts in B's categories.
  -- The grid includes every (day, platform, brand) we collected anything for, so a day with
  -- answers but no real ones yields visibility NULL rather than a missing row or a 0.
  with ans as (
    select raw_answer_id, day, platform_id, category, status
    from parsed.metric_answers
    where day = any(p_days)
  ),
  den as (
    select a.day, a.platform_id, bc.brand_id,
           count(*) filter (where a.status = 'ok') as real_answers,
           count(*)                                 as total_answers
    from ans a
    join config.brand_categories bc on bc.category = a.category
    group by 1, 2, 3
  ),
  num as (
    select a.day, a.platform_id, m.brand_id,
           count(*)          as mentioning,
           avg(m.position)   as avg_position
    from ans a
    join parsed.mentions m
      on m.raw_answer_id = a.raw_answer_id and m.parser_version = v_version
    join config.brand_categories bc
      on bc.brand_id = m.brand_id and bc.category = a.category
    where a.status = 'ok'
    group by 1, 2, 3
  )
  insert into mart.daily_visibility
    (day, platform_id, brand_id, real_answers, mentioning_answers, visibility, avg_position,
     total_answers, parser_version)
  select d.day, d.platform_id, d.brand_id, d.real_answers, coalesce(n.mentioning, 0),
         case when d.real_answers > 0 then round(coalesce(n.mentioning, 0)::numeric / d.real_answers, 6) end,
         round(n.avg_position, 4),
         d.total_answers, v_version
  from den d
  left join num n using (day, platform_id, brand_id);
  get diagnostics v_vis = row_count;

  -- Citation share per domain, per day / platform / prompt category, over real answers.
  with ans as (
    select raw_answer_id, day, platform_id, category
    from parsed.metric_answers
    where day = any(p_days) and status = 'ok'
  ),
  cites as (
    select a.day, a.platform_id, a.category, c.domain, a.raw_answer_id
    from ans a
    join parsed.citations c on c.raw_answer_id = a.raw_answer_id and c.parser_version = v_version
  ),
  totals as (
    select a.day, a.platform_id, a.category,
           count(distinct a.raw_answer_id) as real_answers,
           count(c.raw_answer_id)          as total_citations
    from ans a
    left join cites c using (day, platform_id, category, raw_answer_id)
    group by 1, 2, 3
  )
  insert into mart.daily_citations
    (day, platform_id, category, domain, citing_answers, citations, real_answers, total_citations,
     citation_share, answer_rate, parser_version)
  select c.day, c.platform_id, c.category, c.domain,
         count(distinct c.raw_answer_id), count(*),
         t.real_answers, t.total_citations,
         round(count(*)::numeric / nullif(t.total_citations, 0), 6),
         round(count(distinct c.raw_answer_id)::numeric / nullif(t.real_answers, 0), 6),
         v_version
  from cites c
  join totals t using (day, platform_id, category)
  group by c.day, c.platform_id, c.category, c.domain, t.real_answers, t.total_citations;
  get diagnostics v_cit = row_count;

  insert into mart.daily_platform_health
    (day, platform_id, total, ok, error, empty, not_answer, truncated, wrong_prompt, windows, parser_version)
  select day, platform_id, count(*),
         count(*) filter (where status = 'ok'),
         count(*) filter (where status = 'error'),
         count(*) filter (where status = 'empty'),
         count(*) filter (where status = 'not_answer'),
         count(*) filter (where status = 'truncated'),
         count(*) filter (where status = 'wrong_prompt'),
         count(distinct window_start),
         v_version
  from parsed.metric_answers
  where day = any(p_days)
  group by day, platform_id;
  get diagnostics v_health = row_count;

  return jsonb_build_object('days', cardinality(p_days), 'visibility_rows', v_vis,
                            'citation_rows', v_cit, 'health_rows', v_health);
end;
$$;

create or replace function mart.refresh_category_days(p_days date[]) returns int
language plpgsql as $$
declare
  v_version text := config.active_parser_version();
  n int;
begin
  delete from mart.daily_category_visibility where day = any(p_days);
  with ans as (
    select raw_answer_id, day, platform_id, category, status
    from parsed.metric_answers
    where day = any(p_days)
  ),
  den as (
    select a.day, a.platform_id, a.category, bc.brand_id,
           count(*) filter (where a.status = 'ok') as real_answers, count(*) as total_answers
    from ans a join config.brand_categories bc on bc.category = a.category
    group by 1, 2, 3, 4
  ),
  num as (
    select a.day, a.platform_id, a.category, m.brand_id, count(*) as mentioning, avg(m.position) as avg_position
    from ans a
    join parsed.mentions m on m.raw_answer_id = a.raw_answer_id and m.parser_version = v_version
    join config.brand_categories bc on bc.brand_id = m.brand_id and bc.category = a.category
    where a.status = 'ok'
    group by 1, 2, 3, 4
  )
  insert into mart.daily_category_visibility
  select d.day, d.platform_id, d.category, d.brand_id, d.real_answers, coalesce(n.mentioning, 0),
         case when d.real_answers > 0 then round(coalesce(n.mentioning, 0)::numeric / d.real_answers, 6) end,
         round(n.avg_position, 4), d.total_answers, v_version
  from den d left join num n using (day, platform_id, category, brand_id);
  get diagnostics n = row_count;
  return n;
end;
$$;
