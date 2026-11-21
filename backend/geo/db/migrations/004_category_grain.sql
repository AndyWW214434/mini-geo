-- Visibility per prompt category as well as per brand. Changes usually live in one category
-- ("Stripe lost billing answers on ChatGPT") and get diluted in the brand-level number.

create table mart.daily_category_visibility (
  day                date not null,
  platform_id        text not null,
  category           text not null,
  brand_id           text not null,
  real_answers       int not null,
  mentioning_answers int not null,
  visibility         numeric,      -- NULL when real_answers = 0
  avg_position       numeric,
  total_answers      int not null,
  parser_version     text not null,
  primary key (day, platform_id, category, brand_id)
);
grant select on mart.daily_category_visibility to geo_readonly;

create or replace function mart.refresh_category_days(p_days date[]) returns int
language plpgsql as $$
declare
  v_version text := config.active_parser_version();
  n int;
begin
  delete from mart.daily_category_visibility where day = any(p_days);
  with ans as (
    select raw_answer_id, day, platform_id, category, status
    from parsed.answers
    where parser_version = v_version and day = any(p_days)
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

create or replace function mart.refresh_dirty_days() returns jsonb
language plpgsql as $$
declare
  v_days date[];
  v_cat int;
begin
  with claimed as (
    delete from ops.dirty_days
    where day in (select day from ops.dirty_days order by day for update skip locked)
    returning day
  )
  select coalesce(array_agg(day order by day), '{}') into v_days from claimed;
  if cardinality(v_days) = 0 then
    return jsonb_build_object('days', 0, 'refreshed', '[]'::jsonb);
  end if;
  v_cat := mart.refresh_category_days(v_days);
  return mart.refresh_days(v_days)
         || jsonb_build_object('category_rows', v_cat, 'refreshed', to_jsonb(v_days));
end;
$$;

insert into ops.dirty_days (day) select distinct day from parsed.answers on conflict do nothing;
