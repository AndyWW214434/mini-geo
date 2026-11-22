"""Pipeline steps: seed config, load raw answers, parse, refresh metrics.

Each step is safe to run any number of times, concurrently:
  * raw loads insert ON CONFLICT DO NOTHING on (window, platform, prompt);
  * parsing selects only raw rows with no parsed row for this parser version, and inserts
    ON CONFLICT DO NOTHING, queueing the touched days in the same transaction;
  * metrics recompute whole days from the parsed layer (see 002_etl.sql).
"""

from __future__ import annotations

import gzip
import hashlib
import json
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import structlog

from geo.catalog import Brand, Prompt, load_brands, load_prompts
from geo.db import connection
from geo.parsing.parser import PARSER_VERSION, Parser
from geo.platforms import REGISTRY, adapter_for_record

log = structlog.get_logger()
WINDOW_SECONDS = 900


def floor_window(ts: datetime) -> datetime:
    epoch = int(ts.timestamp())
    return datetime.fromtimestamp(epoch - epoch % WINDOW_SECONDS, UTC)


OMIT_OVER_BYTES = 100_000


def slim_record(record: dict[str, Any], limit: int = OMIT_OVER_BYTES) -> dict[str, Any]:
    """Replace page dumps (raw HTML, raw response streams) with their size and SHA-256.

    Generic on purpose: no field names, any string field over `limit` bytes. The fields answers
    are parsed from (answer text, markdown, citations) are a few KB and kept verbatim.
    """
    out: dict[str, Any] = {}
    for key, value in record.items():
        if isinstance(value, str) and len(value) > limit:
            data = value.encode()
            out[key] = {"_omitted": True, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        else:
            out[key] = value
    return out


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@contextmanager
def tracked(step: str, **detail: Any) -> Iterator[dict[str, Any]]:
    """Record a pipeline step in ops.pipeline_runs (succeeded or failed, never lost)."""
    with connection() as conn:
        run_id = conn.execute("insert into ops.pipeline_runs (step, detail) values (%s, %s) returning id",
                              (step, json.dumps(detail, default=str))).fetchone()["id"]
    result: dict[str, Any] = {}
    try:
        yield result
    except Exception as exc:
        with connection() as conn:
            conn.execute("update ops.pipeline_runs set status='failed', finished_at=now(), error=%s "
                         "where id=%s", (repr(exc)[:2000], run_id))
        raise
    with connection() as conn:
        conn.execute("update ops.pipeline_runs set status='succeeded', finished_at=now(), rows_in=%s, "
                     "rows_out=%s, detail = detail || %s where id=%s",
                     (result.get("rows_in"), result.get("rows_out"), json.dumps(result, default=str), run_id))


# ------------------------------------------------------------------------------------- seed
def seed_catalog(brands: list[Brand] | None = None, prompts: list[Prompt] | None = None) -> None:
    brands, prompts = brands or load_brands(), prompts or load_prompts()
    cats = sorted({p.category for p in prompts} | {c for b in brands for c in b.categories})
    with connection() as conn:
        conn.cursor().executemany("insert into config.categories values (%s) on conflict do nothing",
                                  [(c,) for c in cats])
        conn.cursor().executemany(
            "insert into config.platforms (platform_id, name, settings) values (%s, %s, %s) "
            "on conflict (platform_id) do update set name = excluded.name, "
            "settings = excluded.settings || config.platforms.settings",
            [(a.platform_id, a.name, json.dumps({"dataset_id": a.default_dataset_id}))
             for a in REGISTRY.values()])
        conn.cursor().executemany(
            "insert into config.prompts (prompt_id, category, text) values (%s, %s, %s) "
            "on conflict (prompt_id) do update set category = excluded.category, text = excluded.text",
            [(p.prompt_id, p.category, p.text) for p in prompts])
        conn.cursor().executemany(
            "insert into config.brands (brand_id, name, aliases, strict_aliases, domains) "
            "values (%s, %s, %s, %s, %s) on conflict (brand_id) do update set name = excluded.name, "
            "aliases = excluded.aliases, strict_aliases = excluded.strict_aliases, domains = excluded.domains",
            [(b.brand_id, b.name, list(b.aliases), list(b.strict_aliases), list(b.domains)) for b in brands])
        conn.execute("delete from config.brand_categories")
        conn.cursor().executemany("insert into config.brand_categories values (%s, %s)",
                                  [(b.brand_id, c) for b in brands for c in b.categories])


def catalog_from_db(conn: psycopg.Connection) -> tuple[list[Brand], dict[str, Prompt]]:
    brands = [Brand(r["brand_id"], r["name"], (), tuple(r["aliases"]), tuple(r["strict_aliases"]),
                    tuple(r["domains"]))
              for r in conn.execute("select * from config.brands")]
    prompts = {r["prompt_id"]: Prompt(r["prompt_id"], r["category"], r["text"])
               for r in conn.execute("select * from config.prompts")}
    return brands, prompts


# ------------------------------------------------------------------------------------- raw
@dataclass(frozen=True)
class RawAnswer:
    window_start: datetime
    platform_id: str
    prompt_id: str
    source: str
    collected_at: datetime
    payload: dict[str, Any]
    run_id: int | None = None


def insert_raw(conn: psycopg.Connection, rows: list[RawAnswer]) -> int:
    if not rows:
        return 0
    cur = conn.execute(
        """insert into raw.answers (run_id, window_start, platform_id, prompt_id, source, collected_at,
                                    payload, payload_sha256)
           select * from unnest(%s::bigint[], %s::timestamptz[], %s::text[], %s::text[], %s::text[],
                                %s::timestamptz[], %s::jsonb[], %s::text[])
           on conflict (window_start, platform_id, prompt_id) do nothing""",
        ([r.run_id for r in rows], [r.window_start for r in rows], [r.platform_id for r in rows],
         [r.prompt_id for r in rows], [r.source for r in rows], [r.collected_at for r in rows],
         [json.dumps(r.payload) for r in rows], [payload_hash(r.payload) for r in rows]))
    return cur.rowcount


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def load_history(path: Path, batch: int = 2000) -> dict[str, int]:
    """Load Bright Data records from a .jsonl(.gz) file. Re-running loads nothing new."""
    stats = {"read": 0, "inserted": 0, "unknown_platform": 0, "unknown_prompt": 0, "no_timestamp": 0}
    with tracked("load_history", path=str(path)) as result, connection() as conn:
        _, prompts = catalog_from_db(conn)
        by_text = {p.text.strip().lower(): p.prompt_id for p in prompts.values()}
        buf: list[RawAnswer] = []
        for rec in _read_jsonl(path):
            stats["read"] += 1
            ad = adapter_for_record(rec)
            asked = ((rec.get("input") or {}).get("prompt") or rec.get("prompt") or "").strip().lower()
            ts = rec.get("timestamp")
            if not ad:
                stats["unknown_platform"] += 1
                continue
            if asked not in by_text:
                stats["unknown_prompt"] += 1
                continue
            if not ts:
                stats["no_timestamp"] += 1
                continue
            collected = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            buf.append(RawAnswer(floor_window(collected), ad.platform_id, by_text[asked],
                                 "synthetic" if rec.get("_synthetic") else "history", collected, slim_record(rec)))
            if len(buf) >= batch:
                stats["inserted"] += insert_raw(conn, buf)
                conn.commit()
                buf.clear()
        stats["inserted"] += insert_raw(conn, buf)
        result.update(stats, rows_in=stats["read"], rows_out=stats["inserted"])
    return stats


# ------------------------------------------------------------------------------------- parse
def parse_pending(version: str = PARSER_VERSION, batch: int = 1000, max_batches: int | None = None) -> int:
    """Parse raw answers that have no parsed row for `version`. Returns rows parsed."""
    total = 0
    with tracked("parse", parser_version=version) as result:
        with connection() as conn:
            brands, prompts = catalog_from_db(conn)
        parser = Parser(brands)
        assert parser.version == version, "parse with the code's parser version"
        batches = 0
        while max_batches is None or batches < max_batches:
            with connection() as conn:
                rows = conn.execute(
                    """select r.raw_answer_id, r.window_start, r.platform_id, r.prompt_id, r.source, r.payload
                       from raw.answers r
                       where not exists (select 1 from parsed.answers p
                                         where p.raw_answer_id = r.raw_answer_id and p.parser_version = %s)
                       order by r.raw_answer_id
                       limit %s
                       for update of r skip locked""", (version, batch)).fetchall()
                if not rows:
                    break
                _write_parsed(conn, parser, prompts, rows)
            total += len(rows)
            batches += 1
        result.update(rows_in=total, rows_out=total)
    return total


def _write_parsed(conn: psycopg.Connection, parser: Parser, prompts: dict[str, Prompt],
                  rows: list[dict[str, Any]]) -> None:
    a_cols: dict[str, list[Any]] = {k: [] for k in
                                    ("id", "platform", "prompt", "cat", "window", "day", "status", "detail", "chars", "source")}
    m_cols: dict[str, list[Any]] = {k: [] for k in ("id", "brand", "pos", "alias", "offset")}
    c_cols: dict[str, list[Any]] = {k: [] for k in ("id", "ord", "url", "host", "domain")}
    for r in rows:
        out = parser.parse(r["platform_id"], r["payload"])
        rid = r["raw_answer_id"]
        for k, v in zip(a_cols, (rid, r["platform_id"], r["prompt_id"], prompts[r["prompt_id"]].category,
                                 r["window_start"], r["window_start"].astimezone(UTC).date(), out.status,
                                 out.status_detail, out.answer_chars, r["source"]), strict=True):
            a_cols[k].append(v)
        for m in out.mentions:
            for k, v in zip(m_cols, (rid, m.brand_id, m.position, m.matched_alias, m.char_offset), strict=True):
                m_cols[k].append(v)
        for c in out.citations:
            for k, v in zip(c_cols, (rid, c.ordinal, c.url, c.host, c.domain), strict=True):
                c_cols[k].append(v)
    v = parser.version
    conn.execute(
        """insert into parsed.answers (raw_answer_id, parser_version, platform_id, prompt_id, category,
                                       window_start, day, status, status_detail, answer_chars, source)
           select id, %s, platform, prompt, cat, win, day, status, detail, chars, source
           from unnest(%s::bigint[], %s::text[], %s::text[], %s::text[], %s::timestamptz[], %s::date[],
                       %s::text[], %s::text[], %s::int[], %s::text[])
                as t(id, platform, prompt, cat, win, day, status, detail, chars, source)
           on conflict do nothing""", (v, *a_cols.values()))
    conn.execute(
        """insert into parsed.mentions (raw_answer_id, parser_version, brand_id, position, matched_alias,
                                        char_offset)
           select id, %s, brand, pos, alias, off
           from unnest(%s::bigint[], %s::text[], %s::int[], %s::text[], %s::int[]) as t(id, brand, pos, alias, off)
           on conflict do nothing""", (v, *m_cols.values()))
    conn.execute(
        """insert into parsed.citations (raw_answer_id, parser_version, ordinal, url, host, domain)
           select id, %s, ord, url, host, domain
           from unnest(%s::bigint[], %s::int[], %s::text[], %s::text[], %s::text[]) as t(id, ord, url, host, domain)
           on conflict do nothing""", (v, *c_cols.values()))
    conn.execute("insert into ops.dirty_days (day) select distinct unnest(%s::date[]) on conflict do nothing",
                 (a_cols["day"],))


# ------------------------------------------------------------------------------------- metrics
def activate_code_parser_version() -> bool:
    """Switch metrics to this code's parser version once its backlog is fully parsed."""
    with connection() as conn:
        active = conn.execute("select config.active_parser_version() as v").fetchone()["v"]
        if active == PARSER_VERSION:
            return False
        backlog = conn.execute(
            """select count(*) as n from raw.answers r where not exists (
                 select 1 from parsed.answers p where p.raw_answer_id = r.raw_answer_id
                 and p.parser_version = %s)""", (PARSER_VERSION,)).fetchone()["n"]
        if backlog:
            return False
        conn.execute("select ops.activate_parser_version(%s)", (PARSER_VERSION,))
        log.info("parser_version_activated", version=PARSER_VERSION, previous=active)
        return True


def refresh_metrics() -> dict[str, Any]:
    with tracked("refresh_metrics") as result, connection() as conn:
        out = conn.execute("select mart.refresh_dirty_days() as r").fetchone()["r"]
        result.update(out, rows_in=out.get("days"), rows_out=out.get("visibility_rows"))
    return out


def run_pipeline() -> dict[str, Any]:
    parsed = parse_pending()
    activated = activate_code_parser_version()
    metrics = refresh_metrics()
    return {"parsed": parsed, "activated_parser": activated, "metrics": metrics}
