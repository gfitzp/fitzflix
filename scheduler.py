"""Run the recurring-jobs process: the native cron of rq and the
scheduled-job mover. This process replaces rq-scheduler.

One process does the two halves of the work of rq-scheduler. A
CronScheduler holds the cron_table registrations and enqueues them on
schedule. An RQScheduler moves the due entries out of the
ScheduledJobRegistry of each queue. These entries are the enqueue_in
defers and the retries. The mover runs here, not in per-worker
with_scheduler subprocesses. Thus, the workers do not fork and the
supervisor layout does not change.

This main thread drives the two loops. The start() and work() methods of
each class install signal handlers. Only the main thread can do that.
Thus, this module calls the public step methods of each loop directly.
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
    """Read the cron fields on the local clock of the server.

    This is the use_local_timezone behavior of rq-scheduler. rq.cron does
    not have it. rq.cron evaluates the cron fields in UTC. That would
    shift each job by the UTC offset. For example, the nightly backup
    would run at 8:30 PM, not at 12:30 AM. This class always computes
    the next time from now, never from the last run. Thus, a restart
    never replays a missed occurrence. The old scheduler did the same."""

    def get_next_enqueue_time(self):
        local_now = now().astimezone()
        next_local = croniter(self.cron, local_now).get_next(datetime)
        return next_local.astimezone(timezone.utc)


# This key holds the last-enqueued times. They live outside the
# CronScheduler hash, because the TTL of that hash ends with the process.
# Thus, the "Last ran" column of the System page survives a restart. This
# is the run-history behavior of the register_cron flow.

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

# A crashed predecessor can leave a stale registry entry. That entry
# would make this birth look like a duplicate. Thus, sweep the registry
# first.

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
    # Change the class for local-time evaluation. Then compute the first
    # next-run time again, because the constructor derived it in UTC.
    cron_job.__class__ = LocalTimeCronJob
    cron_job.next_enqueue_time = cron_job.get_next_enqueue_time()
# Restore the last-enqueued time of each job from the durable record.
# Thus, a restart does not blank the "Last ran" column of the System page.

stored_latest = app.redis.hgetall(LATEST_KEY)
for cron_job in cron.get_jobs():
    raw = stored_latest.get(f"{cron_job.func_name}|{cron_job.cron}".encode())
    if raw:
        try:
            cron_job.latest_enqueue_time = datetime.fromisoformat(raw.decode())
        except ValueError:
            pass

cron.register_birth()

# Publish the table immediately. Thus, the reader of the System page
# sees the table before the first enqueue.

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
