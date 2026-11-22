"""Temporal activities: every side effect of collection lives here.

All activities are idempotent so Temporal can retry them freely:
  * begin_run upserts the (window, platform) row;
  * trigger_snapshot re-reads the stored snapshot id before triggering a new one;
  * ingest_snapshot inserts ON CONFLICT DO NOTHING.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime

import structlog
from temporalio import activity
from temporalio.exceptions import ApplicationError

from geo.collection.sources import RateLimited, SourceError, source_for
from geo.db import connection
from geo.etl import pipeline
from geo.platforms import adapter

log = structlog.get_logger()


@dataclass
class PlatformWindow:
    window_start: str  # ISO-8601, aligned to 15 minutes, UTC
    platform_id: str


@dataclass
class RunInfo:
    run_id: int
    status: str
    snapshot_id: str | None
    source: str
    dataset_id: str
    prompts: list[str]


@dataclass
class SnapshotRef:
    run_id: int
    platform_id: str
    window_start: str
    snapshot_id: str
    source: str


@dataclass
class RunFailure:
    run_id: int
    status: str  # failed | timed_out
    error: str


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


@activity.defn
def list_active_platforms() -> list[str]:
    with connection() as conn:
        return [r["platform_id"] for r in
                conn.execute("select platform_id from config.platforms where active order by platform_id")]


@activity.defn
def begin_run(pw: PlatformWindow) -> RunInfo:
    from geo.config import settings

    info = activity.info()
    with connection() as conn:
        row = conn.execute(
            """insert into raw.collection_runs (window_start, platform_id, source, workflow_id, attempts)
               values (%s, %s, %s, %s, 1)
               on conflict (window_start, platform_id) do update
                 set attempts = raw.collection_runs.attempts + 1, updated_at = now()
               returning run_id, status, snapshot_id, source""",
            (pw.window_start, pw.platform_id, settings().effective_source, info.workflow_id)).fetchone()
        dataset = conn.execute("select settings->>'dataset_id' as d from config.platforms where platform_id = %s",
                               (pw.platform_id,)).fetchone()["d"] or adapter(pw.platform_id).default_dataset_id
        prompts = [r["text"] for r in conn.execute(
            "select text from config.prompts where active order by prompt_id")]
    return RunInfo(row["run_id"], row["status"], row["snapshot_id"], row["source"], dataset, prompts)


@activity.defn
def trigger_snapshot(run: RunInfo, pw: PlatformWindow) -> str:
    with connection() as conn:  # a retry after a successful trigger must not trigger again
        existing = conn.execute("select snapshot_id from raw.collection_runs where run_id = %s",
                                (run.run_id,)).fetchone()["snapshot_id"]
    if existing:
        return existing
    src = source_for(run.source)
    inputs = [adapter(pw.platform_id).build_input(p) for p in run.prompts]
    try:
        snapshot_id = src.trigger(pw.platform_id, run.dataset_id, inputs, pw.window_start)
    except SourceError as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc
    except RateLimited as exc:
        raise ApplicationError(str(exc), type="RateLimited") from exc
    with connection() as conn:
        conn.execute("""update raw.collection_runs set snapshot_id = %s, status = 'triggered',
                        prompts_requested = %s, updated_at = now() where run_id = %s""",
                     (snapshot_id, len(inputs), run.run_id))
    log.info("snapshot_triggered", platform=pw.platform_id, window=pw.window_start, snapshot=snapshot_id)
    return snapshot_id


@activity.defn
def snapshot_state(ref: SnapshotRef) -> str:
    try:
        st = source_for(ref.source).status(ref.snapshot_id)
    except RateLimited as exc:
        raise ApplicationError(str(exc), type="RateLimited") from exc
    if st.state == "failed":
        raise ApplicationError(f"snapshot failed: {st.detail}", non_retryable=True)
    return st.state


@activity.defn
def ingest_snapshot(ref: SnapshotRef) -> dict[str, int]:
    """Download, keep the body byte-for-byte, store one raw row per prompt."""
    try:
        body = source_for(ref.source).download(ref.snapshot_id)
    except RateLimited as exc:
        raise ApplicationError(str(exc), type="RateLimited") from exc
    records = json.loads(body)
    if isinstance(records, dict):  # some endpoints wrap results
        records = records.get("data") or records.get("results") or [records]
    records = [pipeline.slim_record(r) for r in records if isinstance(r, dict)]
    window = datetime.fromisoformat(ref.window_start)
    raw_bytes = body.encode()
    with connection() as conn:
        conn.execute("""insert into raw.snapshots (snapshot_id, run_id, body, original_bytes, original_sha256)
                        values (%s, %s, %s, %s, %s) on conflict do nothing""",
                     (ref.snapshot_id, ref.run_id, json.dumps(records), len(raw_bytes),
                      hashlib.sha256(raw_bytes).hexdigest()))
        by_text = {_norm(r["text"]): r["prompt_id"] for r in conn.execute("select prompt_id, text from config.prompts")}
        rows, unmatched = [], 0
        for rec in records:
            asked = _norm((rec.get("input") or {}).get("prompt") or rec.get("prompt") or "")
            prompt_id = by_text.get(asked)
            if not prompt_id:
                unmatched += 1
                continue
            ts = rec.get("timestamp")
            collected = datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else datetime.now(window.tzinfo)
            rows.append(pipeline.RawAnswer(window, ref.platform_id, prompt_id, ref.source, collected, rec, ref.run_id))
        inserted = pipeline.insert_raw(conn, rows)
        requested = conn.execute("select prompts_requested from raw.collection_runs where run_id = %s",
                                 (ref.run_id,)).fetchone()["prompts_requested"]
        missing = max(0, requested - len({r.prompt_id for r in rows}))
        conn.execute(
            """update raw.collection_runs set status = 'collected', answers_received = %s,
               answers_inserted = answers_inserted + %s, updated_at = now(),
               error = case when %s > 0 or %s > 0 then format('%%s prompts missing, %%s records unmatched', %s, %s) end
               where run_id = %s""",
            (len(records), inserted, missing, unmatched, missing, unmatched, ref.run_id))
    log.info("snapshot_ingested", platform=ref.platform_id, window=ref.window_start, received=len(records),
             inserted=inserted, missing=missing)
    return {"received": len(records), "inserted": inserted, "missing": missing}


@activity.defn
def mark_run(failure: RunFailure) -> None:
    with connection() as conn:
        conn.execute("update raw.collection_runs set status = %s, error = %s, updated_at = now() "
                     "where run_id = %s and status <> 'collected'",
                     (failure.status, failure.error[:2000], failure.run_id))
    log.warning("collection_run_not_collected", run_id=failure.run_id, status=failure.status, error=failure.error)


@activity.defn
def run_pipeline() -> dict:
    return pipeline.run_pipeline()


@activity.defn
def run_daily_report() -> dict:
    from geo.insights.detect import run_insights
    from geo.report.build import build_and_store

    insights = run_insights()
    report_id, _ = build_and_store(None)
    return {"insights": insights, "report_id": str(report_id)}


ALL = [list_active_platforms, begin_run, trigger_snapshot, snapshot_state, ingest_snapshot, mark_run,
       run_pipeline, run_daily_report]
