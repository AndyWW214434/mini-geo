"""Temporal worker. Run N replicas; Temporal spreads workflow and activity tasks across them."""

from __future__ import annotations

import asyncio
import logging
import signal
from concurrent.futures import ThreadPoolExecutor

import structlog
from temporalio.worker import Worker

from geo.cli import bootstrap
from geo.config import settings
from geo.workflows.activities import ALL as ACTIVITIES
from geo.workflows.collection import WORKFLOWS
from geo.workflows.schedule import connect, ensure_schedule

log = structlog.get_logger()


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    s = settings()
    await asyncio.to_thread(bootstrap)
    client = await connect()
    log.info("schedules", result=await ensure_schedule())
    worker = Worker(
        client,
        task_queue=s.temporal_task_queue,
        workflows=WORKFLOWS,
        activities=ACTIVITIES,
        activity_executor=ThreadPoolExecutor(max_workers=32),
        max_concurrent_activities=32,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    log.info("worker_started", task_queue=s.temporal_task_queue, source=s.effective_source)
    async with worker:
        await stop.wait()
    log.info("worker_stopped")


if __name__ == "__main__":
    asyncio.run(main())
