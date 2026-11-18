"""Prove that a worker of each queue runs the code of this folder.

Run this script after a deploy. It puts 1 harmless job on each queue.
The job reads 1 setting and changes nothing. The script also queues the
SQS restore poll, which the schedule runs each hour. The poll is a real
task, so it proves the task registry too. The script waits up to 90
seconds, then prints how each job ended.

A correct result is 1 line for each queue with the status "finished",
and the line of the poll with "finished" and "True". A job that stays
"queued" has no worker that runs.

Usage: venv/bin/python scripts/drill_workers.py
"""

import logging
import os
import sys
import time

# The folder of this file is the first entry of the module path. The
# app package is one folder up.

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The start of the app writes log lines. They are not part of the answer.

logging.disable(logging.CRITICAL)

from app import create_app  # noqa: E402
from app.tasks import enqueue_task  # noqa: E402

WAIT_SECONDS = 90
ENDED = ("finished", "failed")

app = create_app()
with app.app_context():
    jobs = []
    for key, queue in app.queues.items():
        job = queue.enqueue(
            "app.settings.get_setting", "DVR_TV_CHANNELS", job_timeout=60
        )
        jobs.append((key, job))

    # enqueue_task returns None when a copy of the poll already waits.

    jobs.append(("sqs poll", enqueue_task("app.archive.aws_storage.sqs_retrieve_task")))

    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline:
        statuses = [job.get_status(refresh=True) for _, job in jobs if job]
        if all(status in ENDED for status in statuses):
            break
        time.sleep(1)

    for name, job in jobs:
        if job is None:
            print(f"{name:<12} not queued (a copy already waits)")
            continue
        # rq gives the status as an enum. Its value is the plain word.

        status = job.get_status().value
        value = job.return_value() if status == "finished" else None
        print(f"{name:<12} {status:<9} {str(value)[:40]}")
