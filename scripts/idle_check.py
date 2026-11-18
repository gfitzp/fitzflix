"""Tell if the background queues are idle.

The script exits with 0 when no queue has a job that waits or runs. It
exits with 1 when a queue is busy, and it prints each busy queue. A job
that is booked for a later time does not count. The deploy script runs
this check before it stops the programs, because a stop ends a job
that runs.

Usage: venv/bin/python scripts/idle_check.py
"""

import logging
import os
import sys

# The folder of this file is the first entry of the module path. The
# app package is one folder up.

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The start of the app writes log lines. They are not part of the answer.

logging.disable(logging.CRITICAL)

from rq.registry import StartedJobRegistry  # noqa: E402

from app import create_app  # noqa: E402

app = create_app()
busy = []
with app.app_context():
    for key, queue in app.queues.items():
        queued = len(queue)
        started = len(StartedJobRegistry(queue=queue))
        if queued or started:
            busy.append(f"{key}: queued {queued}, started {started}")

print("idle" if not busy else "busy: " + "; ".join(busy))
sys.exit(1 if busy else 0)
