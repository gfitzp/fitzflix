"""The recurring-jobs process: rq's native cron plus the scheduled-job
mover, replacing rq-scheduler.

One process does both halves of what rq-scheduler did: a CronScheduler
holds the cron_table registrations and enqueues them on schedule, and an
RQScheduler moves due entries out of every queue's ScheduledJobRegistry
(the enqueue_in defers and retries). Running the mover here — instead of
per-worker with_scheduler subprocesses — keeps the workers fork-free and
leaves the supervisor layout untouched.

Both loops are driven from this main thread because each class's own
start()/work() insists on installing signal handlers, which only the main
thread may do; the public stepping methods they loop over are called here
directly instead.
"""

import importlib
import signal
import sys
import time

from datetime import datetime, timezone

from croniter import croniter
from rq import Queue
from rq.cron import CronJob, CronScheduler, cron_scheduler_registry
from rq.scheduler import RQScheduler
from rq.utils import now

from app import cron_table, get_app


class LocalTimeCronJob(CronJob):
    """Cron fields read on the server's local clock — the rq-scheduler
    use_local_timezone semantics rq.cron lacks (it evaluates crons in
    UTC, which would shift every job by the UTC offset: the nightly
    backup at 8:30 PM instead of 12:30 AM). The next time is always
    computed from NOW, never from the last run, so a restart never
    replays a missed occurrence — also the old scheduler's behavior."""

    def get_next_enqueue_time(self):
        local_now = now().astimezone()
        next_local = croniter(self.cron, local_now).get_next(datetime)
        return next_local.astimezone(timezone.utc)


# Last-enqueued times, persisted outside the CronScheduler hash (whose
# TTL dies with the process) so the System page's "Last ran" column
# survives restarts — the register_cron flow's run-history semantics

LATEST_KEY = "fitzflix:cron:latest"

QUEUE_NAMES = [
    "fitzflix-import",
    "fitzflix-file-operation",
    "fitzflix-transcode",
    "fitzflix-sql",
    "fitzflix-user-request",
    "fitzflix-maintenance",
]

TICK_SECONDS = 15

app = get_app()

stop_requested = False


def request_stop(signum, frame):
    global stop_requested
    stop_requested = True


signal.signal(signal.SIGTERM, request_stop)
signal.signal(signal.SIGINT, request_stop)

# A crashed predecessor can leave a stale registry entry that would make
# this birth look like a duplicate; sweep first

cron_scheduler_registry.cleanup(app.redis)

cron = CronScheduler(connection=app.redis, name="fitzflix")
for entry in cron_table(app.config):
    module_name, func_name = entry["func"].rsplit(".", 1)
    func = getattr(importlib.import_module(module_name), func_name)
    cron_job = cron.register(
        func,
        queue_name=entry["queue"],
        cron=entry["cron"],
        job_timeout=entry["timeout"],
        meta={"cron_string": entry["cron"], "description": entry["description"]},
        result_ttl=86400,
    )
    # Re-class for local-time evaluation, and recompute the initial
    # next-run the constructor already derived in UTC
    cron_job.__class__ = LocalTimeCronJob
    cron_job.next_enqueue_time = cron_job.get_next_enqueue_time()
# Restore each job's last-enqueued time from the durable record, so a
# restart doesn't blank the System page's "Last ran" column

stored_latest = app.redis.hgetall(LATEST_KEY)
for cron_job in cron.get_jobs():
    raw = stored_latest.get(f"{cron_job.func_name}|{cron_job.cron}".encode())
    if raw:
        try:
            cron_job.latest_enqueue_time = datetime.fromisoformat(raw.decode())
        except ValueError:
            pass

cron.register_birth()

# Publish the table immediately so the System page's reader sees it
# before the first enqueue

cron.save_jobs_data()


def record_latest_times():
    mapping = {
        f"{cron_job.func_name}|{cron_job.cron}": cron_job.latest_enqueue_time.isoformat()
        for cron_job in cron.get_jobs()
        if cron_job.latest_enqueue_time
    }
    if mapping:
        app.redis.hset(LATEST_KEY, mapping=mapping)


mover = RQScheduler(
    [Queue(name, connection=app.redis) for name in QUEUE_NAMES],
    connection=app.redis,
    interval=TICK_SECONDS,
)
mover.register_birth()

app.logger.info(
    f"Recurring-jobs process started: {len(cron.get_jobs())} cron jobs, "
    f"scheduled-job mover on {len(QUEUE_NAMES)} queues"
)

try:
    while not stop_requested:
        if mover.should_reacquire_locks:
            mover.acquire_locks()
        mover.enqueue_scheduled_jobs()
        mover.heartbeat()

        if cron.enqueue_jobs():
            cron.save_jobs_data()
            record_latest_times()
        cron.heartbeat()

        time.sleep(TICK_SECONDS)
finally:
    cron.register_death()
    mover.register_death()

sys.exit(0)
