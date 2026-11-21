"""Collection workflows.

CollectWindow (started by the 15-minute Schedule)
  -> one CollectPlatformWindow child per active platform, with workflow id
     `collect/<platform>/<window>` and REJECT_DUPLICATE: a second trigger for the same window
     (manual trigger, schedule catch-up, retry) cannot start a second collection.
  -> after the children finish, parse + refresh metrics (idempotent).

Windows are independent, so overlapping runs are allowed: a slow Bright Data snapshot for
10:00 keeps polling while 10:15 starts. Each child has its own deadline; past it the run is
recorded as timed_out and its window stays a visible gap rather than being collected late.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, SearchAttributeKey, WorkflowIDReusePolicy
from temporalio.exceptions import ActivityError, ApplicationError, WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    from geo.workflows import activities as acts
    from geo.workflows.activities import PlatformWindow, RunFailure, SnapshotRef

SCHEDULED_START = SearchAttributeKey.for_datetime("TemporalScheduledStartTime")
WINDOW = timedelta(minutes=15)

DB_RETRY = RetryPolicy(initial_interval=timedelta(seconds=2), maximum_interval=timedelta(seconds=30),
                       maximum_attempts=6)
PROVIDER_RETRY = RetryPolicy(initial_interval=timedelta(seconds=5), backoff_coefficient=2.0,
                             maximum_interval=timedelta(minutes=2), maximum_attempts=8)


def window_of(ts: datetime) -> datetime:
    ts = ts.astimezone(UTC)
    return ts.replace(minute=ts.minute - ts.minute % 15, second=0, microsecond=0)


@dataclass
class CollectWindowInput:
    window_start: str | None = None  # override for backfills; default = the scheduled time's window
    deadline_minutes: int = 50
    poll_seconds: int = 20


@workflow.defn
class CollectPlatformWindow:
    @workflow.run
    async def run(self, pw: PlatformWindow, deadline_minutes: int = 50, poll_seconds: int = 20) -> dict:
        run = await workflow.execute_activity(acts.begin_run, pw, start_to_close_timeout=timedelta(seconds=30),
                                              retry_policy=DB_RETRY)
        if run.status == "collected":
            return {"platform": pw.platform_id, "status": "already_collected"}

        deadline = datetime.fromisoformat(pw.window_start) + timedelta(minutes=deadline_minutes)
        try:
            snapshot_id = run.snapshot_id or await workflow.execute_activity(
                acts.trigger_snapshot, args=[run, pw], start_to_close_timeout=timedelta(minutes=2),
                retry_policy=PROVIDER_RETRY)
            ref = SnapshotRef(run.run_id, pw.platform_id, pw.window_start, snapshot_id, run.source)
            while True:
                state = await workflow.execute_activity(acts.snapshot_state, ref,
                                                        start_to_close_timeout=timedelta(seconds=60),
                                                        retry_policy=PROVIDER_RETRY)
                if state == "ready":
                    break
                if workflow.now() >= deadline:
                    await self._mark(run.run_id, "timed_out", f"snapshot {snapshot_id} not ready by {deadline}")
                    return {"platform": pw.platform_id, "status": "timed_out"}
                await workflow.sleep(timedelta(seconds=poll_seconds))
            result = await workflow.execute_activity(acts.ingest_snapshot, ref,
                                                     start_to_close_timeout=timedelta(minutes=5),
                                                     retry_policy=PROVIDER_RETRY)
            return {"platform": pw.platform_id, "status": "collected", **result}
        except ActivityError as err:
            cause = err.cause.message if isinstance(err.cause, ApplicationError) else str(err.cause or err)
            await self._mark(run.run_id, "failed", cause)
            return {"platform": pw.platform_id, "status": "failed", "error": cause}

    @staticmethod
    async def _mark(run_id: int, status: str, error: str) -> None:
        await workflow.execute_activity(acts.mark_run, RunFailure(run_id, status, error),
                                        start_to_close_timeout=timedelta(seconds=30), retry_policy=DB_RETRY)


@workflow.defn
class CollectWindow:
    @workflow.run
    async def run(self, inp: CollectWindowInput | None = None) -> dict:
        inp = inp or CollectWindowInput()
        if inp.window_start:
            window = window_of(datetime.fromisoformat(inp.window_start))
        else:
            scheduled = workflow.info().typed_search_attributes.get(SCHEDULED_START)
            window = window_of(scheduled or workflow.now())
        window_iso = window.isoformat()

        platforms = await workflow.execute_activity(acts.list_active_platforms,
                                                    start_to_close_timeout=timedelta(seconds=30),
                                                    retry_policy=DB_RETRY)
        children = []
        skipped = []
        for platform_id in platforms:
            try:
                children.append(await workflow.start_child_workflow(
                    CollectPlatformWindow.run,
                    args=[PlatformWindow(window_iso, platform_id), inp.deadline_minutes, inp.poll_seconds],
                    id=f"collect/{platform_id}/{window_iso}",
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    execution_timeout=timedelta(minutes=inp.deadline_minutes + 15),
                ))
            except WorkflowAlreadyStartedError:
                skipped.append(platform_id)  # this window is already being (or was) collected
        results = await asyncio.gather(*children) if children else []

        pipeline = await workflow.execute_activity(acts.run_pipeline, start_to_close_timeout=timedelta(minutes=10),
                                                   retry_policy=DB_RETRY)
        return {"window": window_iso, "results": results, "skipped_duplicates": skipped,
                "pipeline": pipeline}


@workflow.defn
class DailyReport:
    """Insights + report once a day, after the day's windows are in."""

    @workflow.run
    async def run(self) -> dict:
        await workflow.execute_activity(acts.run_pipeline, start_to_close_timeout=timedelta(minutes=10),
                                        retry_policy=DB_RETRY)
        return await workflow.execute_activity(acts.run_daily_report, start_to_close_timeout=timedelta(minutes=10),
                                               retry_policy=DB_RETRY)


WORKFLOWS = [CollectWindow, CollectPlatformWindow, DailyReport]
