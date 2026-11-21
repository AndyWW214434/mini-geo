-- Set-based transforms, run inside the database.
--
-- mart.refresh_dirty_days() is the only entry point. It drains ops.dirty_days and fully
-- recomputes every metric for exactly those days, in one transaction. Recomputing a day
-- from the parsed layer (rather than adding deltas) is what makes the step idempotent:
-- running it twice, or re-delivering an answer, cannot change the numbers.

create or replace function config.active_parser_version() returns text
language sql stable as $$
  select value #>> '{}' from config.settings where key = 'active_parser_version'
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
    from parsed.answers
    where parser_version = v_version and day = any(p_days)
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
    from parsed.answers
    where parser_version = v_version and day = any(p_days) and status = 'ok'
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
  from parsed.answers
  where parser_version = v_version and day = any(p_days)
  group by day, platform_id;
  get diagnostics v_health = row_count;

  return jsonb_build_object('days', cardinality(p_days), 'visibility_rows', v_vis,
                            'citation_rows', v_cit, 'health_rows', v_health);
end;
$$;

-- Drain the dirty-day queue and refresh. `for update skip locked` lets two pipeline workers
-- run at once without double work; each day is refreshed by whoever claimed it.
create or replace function mart.refresh_dirty_days() returns jsonb
language plpgsql as $$
declare
  v_days date[];
begin
  with claimed as (
    delete from ops.dirty_days
    where day in (select day from ops.dirty_days order by day for update skip locked)
    returning day
  )
  select coalesce(array_agg(day order by day), '{}') into v_days from claimed;
  return mart.refresh_days(v_days) || jsonb_build_object('refreshed', to_jsonb(v_days));
end;
$$;

-- Switching parser versions (after a reparse) marks every parsed day dirty.
create or replace function ops.activate_parser_version(p_version text) returns int
language plpgsql as $$
declare n int;
begin
  update config.settings set value = to_jsonb(p_version) where key = 'active_parser_version';
  insert into ops.dirty_days (day)
  select distinct day from parsed.answers where parser_version = p_version
  on conflict do nothing;
  get diagnostics n = row_count;
  return n;
end;
$$;

-- Last runs at a glance: is collection keeping up?
create or replace view ops.collection_status as
select platform_id,
       max(window_start) filter (where status = 'collected') as last_collected_window,
       count(*) filter (where window_start > now() - interval '24 hours')                       as runs_24h,
       count(*) filter (where window_start > now() - interval '24 hours' and status = 'collected') as collected_24h,
       count(*) filter (where window_start > now() - interval '24 hours' and status in ('failed', 'timed_out'))
                                                                                                as failed_24h,
       (array_agg(error order by window_start desc) filter (where error is not null))[1]       as last_error
from raw.collection_runs
group by platform_id;
