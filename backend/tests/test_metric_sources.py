"""Synthetic and real answers are never blended in one metric series."""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB, reason="TEST_DATABASE_URL not set")

CHECKSUM = """select md5(coalesce(string_agg(t::text, ',' order by day, platform_id, brand_id), '')) c from (
                select day, platform_id, brand_id, real_answers, mentioning_answers, visibility
                from mart.daily_visibility) t"""


def test_switching_sources_recomputes_without_blending(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DB)
    from geo.config import settings
    settings.cache_clear()
    from geo.catalog import load_brands, load_prompts
    from geo.db import connection
    from geo.etl.pipeline import RawAnswer, insert_raw, parse_pending, refresh_metrics
    from geo.synthetic.generator import Generator

    window = datetime(2026, 11, 19, 7, 45, tzinfo=UTC)
    gen = Generator(load_brands(), load_prompts(), datetime(2026, 10, 22).date())
    with connection() as conn:
        conn.execute("select ops.set_metrics_sources(%s)", (["synthetic", "history"],))
    refresh_metrics()
    with connection() as conn:
        before = conn.execute(CHECKSUM).fetchone()["c"]
        rows = [RawAnswer(window, "chatgpt", p.prompt_id, "brightdata", window, gen.record("chatgpt", p, window, 9))
                for p in load_prompts()]
        insert_raw(conn, rows)
    parse_pending()
    refresh_metrics()
    with connection() as conn:
        assert conn.execute(CHECKSUM).fetchone()["c"] == before, "real answers leaked into synthetic metrics"
        conn.execute("select ops.set_metrics_sources(%s)", (["brightdata"],))
    refresh_metrics()
    with connection() as conn:
        days = conn.execute("select distinct day, platform_id from mart.daily_visibility").fetchall()
        assert [(d["day"].isoformat(), d["platform_id"]) for d in days] == [("2026-11-19", "chatgpt")]
        total = conn.execute("select sum(total) n from mart.daily_platform_health").fetchone()["n"]
        assert total == 20
        conn.execute("select ops.set_metrics_sources(%s)", (["synthetic", "history"],))
    refresh_metrics()
    with connection() as conn:
        assert conn.execute(CHECKSUM).fetchone()["c"] == before
        conn.execute("delete from raw.answers where source = 'brightdata' and window_start = %s", (window,))
