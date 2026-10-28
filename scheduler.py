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
import os
import signal
import socket
import sys
import time

from datetime import datetime

from rq import Queue
from rq.cron import CronJob, CronScheduler, cron_scheduler_registry
from rq.scheduler import RQScheduler
from rq.utils import now

from app import cron_table, get_app, next_cron_time
from app.maintenance import keep_cron_registered
from app.queues import QUEUE_NAMES
from app.tasks import FAILURE_TTL


class LocalTimeCronJob(CronJob):
    """Read the cron fields on the local clock of the server.

    This is the use_local_timezone behavior of rq-scheduler. rq.cron does
    not have it. rq.cron evaluates the cron fields in UTC. That would
    shift each job by the UTC offset. For example, the nightly backup
    would run at 8:30 PM, not at 12:30 AM. This class always computes
    the next time from now, never from the last run. Thus, a restart
    never replays a missed occurrence. The old scheduler did the same.
    next_cron_time applies the daylight saving rules of the local zone."""

    def get_next_enqueue_time(self):
        return next_cron_time(self.cron, now())


# This key holds the last-enqueued times. They live outside the
# CronScheduler hash, because the TTL of that hash ends with the process.
# Thus, the "Last ran" column of the System page survives a restart. This
# is the run-history behavior of the register_cron flow.

LATEST_KEY = "fitzflix:cron:latest"

TICK_SECONDS = 15

# Only 1 process can run the cron table. 2 processes enqueue each job 2
# times. The duplicate check of rq cannot fire inside its pipeline
# (#323). Thus, this process holds its own lock. It refreshes the lock on
# each tick, so a killed process frees it in INSTANCE_TTL seconds.

INSTANCE_KEY = "fitzflix:scheduler:instance"
INSTANCE_TTL = 45

app = get_app()

stop_requested = False


def request_stop(signum, frame):
    global stop_requested
    stop_requested = True


signal.signal(signal.SIGTERM, request_stop)
signal.signal(signal.SIGINT, request_stop)

instance_token = f"{socket.gethostname()}:{os.getpid()}"
refresh_instance = app.redis.register_script(
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"
)
release_instance = app.redis.register_script(
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)

# Wait for the lock. A replacement of a killed process waits until the
# lock of that process expires. A second process waits as long as the
# first one runs.

waiting = False
while not app.redis.set(INSTANCE_KEY, instance_token, nx=True, ex=INSTANCE_TTL):
    if stop_requested:
        sys.exit(0)
    if not waiting:
        holder = app.redis.get(INSTANCE_KEY)
        app.logger.warning(
            f"The recurring-jobs process {holder!r} is running. This process "
            f"waits for its lock"
        )
        waiting = True
    time.sleep(1)

# A crashed predecessor can leave a stale registry entry, and the System
# page would list it. Thus, sweep the registry first. The duplicate check
# of rq does not protect against 2 processes. The instance lock does.

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
        # The System page reads the name of the table from the meta. The
        # func_name of rq is the module that defines the function. For a
        # function that app.videos exports from another module, that is
        # not the name of the table.
        meta={
            "cron_string": entry["cron"],
            "description": entry["description"],
            "func": entry["func"],
        },
        result_ttl=86400,
        # A failure and its result stream expire (#478)
        failure_ttl=FAILURE_TTL,
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

# This process holds the instance lock. Thus, a mover lock that exists
# now belongs to a dead process. While it lived (up to 75 seconds), the
# fallback period of rq kept the new mover idle for 2 minutes (#321).

app.redis.delete(*[mover.get_locking_key(name) for name in QUEUE_NAMES])
mover.register_birth()

app.logger.info(
    f"Recurring-jobs process started: {len(cron.get_jobs())} cron jobs, "
    f"scheduled-job mover on {len(QUEUE_NAMES)} queues"
)

try:
    while not stop_requested:
        if not refresh_instance(
            keys=[INSTANCE_KEY], args=[instance_token, INSTANCE_TTL]
        ):
            # A stall longer than INSTANCE_TTL (a sleep and wake of the
            # Mac) lets the key expire with no other holder (#531). Then
            # this process takes the lock again. Only a key that another
            # process holds stops it.
            if app.redis.set(INSTANCE_KEY, instance_token, nx=True, ex=INSTANCE_TTL):
                app.logger.warning(
                    "The instance lock of the recurring-jobs process expired "
                    "during a stall. The process took it again"
                )
            else:
                app.logger.error(
                    "The recurring-jobs process lost its instance lock. It stops"
                )
                break

        # The same stall can expire the hash and the registry entry of
        # the cron scheduler (#562). Register again before the next save.

        if keep_cron_registered(cron):
            app.logger.warning(
                "The cron scheduler lost its registration during a stall. "
                "The process registered it again"
            )
        if mover.should_reacquire_locks:
            mover.acquire_locks()
        mover.enqueue_scheduled_jobs()
        mover.heartbeat()

        if cron.enqueue_jobs():
            cron.save_jobs_data()
            record_latest_times()
        cron.heartbeat()

        # Sleep in short steps. A stop request then ends the loop in 1
        # second. Supervisor waits 10 seconds before SIGKILL, and a kill
        # skips the cleanup below (#322).

        wake = time.monotonic() + TICK_SECONDS
        while not stop_requested and time.monotonic() < wake:
            time.sleep(1)
finally:
    cron.register_death()

    # stop() releases the mover locks. register_death() alone kept them
    # for up to 75 seconds (#321).

    mover.stop()
    release_instance(keys=[INSTANCE_KEY], args=[instance_token])

sys.exit(0)
