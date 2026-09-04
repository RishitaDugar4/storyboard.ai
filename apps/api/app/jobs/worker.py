"""arq worker entrypoint.

    arq app.jobs.worker.WorkerSettings --queue ai

Handlers are the same objects the inline queue runs; arq only supplies
concurrency, retries and a broker. Everything about a job's state still lives
in Postgres, so a lost Redis message costs a re-enqueue rather than a result.
"""
from __future__ import annotations

import logging
import os
import uuid

from arq import cron, func
from arq.connections import RedisSettings

from ..db.session import dispose_engine
from .events import close_bus
from .handlers import HANDLERS
from .maintenance import cron_reap_stuck_jobs

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("hbz.worker")


def _wrap(handler):
    async def run(ctx, job_id: str) -> None:
        await handler(uuid.UUID(job_id))
    return run


#: Named explicitly via arq.func: setting __name__ on a closure is not enough,
#: because every wrapper shares the qualname "_wrap.<locals>.run" and arq keys
#: its registry by name -- so they silently collapse into one function.
FUNCTIONS = [func(_wrap(fn), name=kind.replace(".", "_"))
             for kind, fn in HANDLERS.items()]


async def on_shutdown(ctx) -> None:
    await close_bus()
    await dispose_engine()


#: Runs on every worker, every minute. Cheap (one indexed query) and the only
#: thing that recovers a job whose worker was killed.
CRON_JOBS = [cron(cron_reap_stuck_jobs, minute=set(range(0, 60)), run_at_startup=True)]


class WorkerSettings:
    functions = FUNCTIONS
    cron_jobs = CRON_JOBS
    redis_settings = RedisSettings.from_dsn(
        os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    #: IO-bound provider calls; the render worker runs separately at 1.
    max_jobs = int(os.getenv("WORKER_CONCURRENCY", "8"))
    job_timeout = int(os.getenv("WORKER_JOB_TIMEOUT", "600"))
    keep_result = 3600
    shutdown = on_shutdown
