"""End-to-end collection against a local Temporal dev server and Postgres.

Proves the exactly-once property: the same window triggered twice yields one run per
platform and one raw answer per (platform, prompt).
Needs TEST_DATABASE_URL (a migrated, seeded database); skipped otherwise.
"""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB, reason="TEST_DATABASE_URL not set")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DB or "")
    monkeypatch.setenv("COLLECTOR_SOURCE", "synthetic")
    from geo.config import settings
    settings.cache_clear()


async def test_same_window_twice_is_collected_once():
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from geo.db import connection
    from geo.workflows.activities import ALL
    from geo.workflows.collection import WORKFLOWS, CollectWindow, CollectWindowInput

    window = "2026-11-21T10:15:00+00:00"
    with connection() as conn:
        conn.execute("delete from raw.answers where window_start = %s", (window,))
        conn.execute("delete from raw.snapshots where run_id in "
                     "(select run_id from raw.collection_runs where window_start = %s)", (window,))
        conn.execute("delete from raw.collection_runs where window_start = %s", (window,))

    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(env.client, task_queue="t", workflows=WORKFLOWS, activities=ALL,
                          activity_executor=ThreadPoolExecutor(8)):
            first = await env.client.execute_workflow(
                CollectWindow.run, CollectWindowInput(window_start=window, poll_seconds=1),
                id=f"w-{uuid.uuid4()}", task_queue="t")
            second = await env.client.execute_workflow(
                CollectWindow.run, CollectWindowInput(window_start="2026-11-21T10:22:00+00:00", poll_seconds=1),
                id=f"w-{uuid.uuid4()}", task_queue="t")

    assert {r["status"] for r in first["results"]} == {"collected"}
    assert len(second["skipped_duplicates"]) == 6 and second["results"] == []
    with connection() as conn:
        runs = conn.execute("select count(*) n from raw.collection_runs where window_start = %s", (window,)).fetchone()
        answers = conn.execute("select count(*) n from raw.answers where window_start = %s", (window,)).fetchone()
    assert runs["n"] == 6
    assert answers["n"] == 6 * 20
