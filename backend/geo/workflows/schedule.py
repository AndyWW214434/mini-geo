"""Temporal client and schedules (created or updated idempotently at worker start)."""

from __future__ import annotations

from datetime import timedelta

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleState,
    ScheduleUpdate,
)

from geo.config import settings
from geo.workflows.collection import CollectWindow, CollectWindowInput, DailyReport

COLLECT_SCHEDULE_ID = "geo-collect-every-15m"
REPORT_SCHEDULE_ID = "geo-daily-report"


async def connect() -> Client:
    s = settings()
    if s.temporal_api_key:  # Temporal Cloud
        return await Client.connect(s.temporal_address, namespace=s.temporal_namespace,
                                    api_key=s.temporal_api_key, tls=True)
    return await Client.connect(s.temporal_address, namespace=s.temporal_namespace)


def _schedules() -> dict[str, Schedule]:
    s = settings()
    return {
        COLLECT_SCHEDULE_ID: Schedule(
            action=ScheduleActionStartWorkflow(
                CollectWindow.run,
                CollectWindowInput(deadline_minutes=s.collection_deadline_minutes,
                                   poll_seconds=s.brightdata_poll_seconds),
                id="collect-window", task_queue=s.temporal_task_queue,
                execution_timeout=timedelta(minutes=s.collection_deadline_minutes + 30)),
            spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(minutes=s.collection_interval_minutes))]),
            # Windows are independent and keyed in the DB, so overlap is safe and a slow window
            # never delays the next one. Missed windows older than ~one interval are not
            # back-collected: an answer fetched an hour late is not that window's answer.
            policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.ALLOW_ALL,
                                  catchup_window=timedelta(minutes=s.collection_interval_minutes - 1)),
            state=ScheduleState(note="Collect every prompt on every platform for each 15-minute window"),
        ),
        REPORT_SCHEDULE_ID: Schedule(
            action=ScheduleActionStartWorkflow(DailyReport.run, id="daily-report", task_queue=s.temporal_task_queue),
            spec=ScheduleSpec(cron_expressions=["30 0 * * *"]),  # 00:30 UTC, after the day closes
            policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP, catchup_window=timedelta(hours=6)),
        ),
    }


async def ensure_schedule(paused: bool = False) -> dict[str, str]:
    client = await connect()
    out = {}
    for schedule_id, schedule in _schedules().items():
        schedule.state.paused = paused
        try:
            await client.create_schedule(schedule_id, schedule)
            out[schedule_id] = "created"
        except ScheduleAlreadyRunningError:
            handle = client.get_schedule_handle(schedule_id)

            def updater(_input, schedule=schedule):
                return ScheduleUpdate(schedule=schedule)

            await handle.update(updater)
            out[schedule_id] = "updated"
    return out
