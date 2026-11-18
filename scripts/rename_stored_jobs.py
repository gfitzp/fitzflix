"""Give the jobs that Redis holds a new function name.

Run this script after a change that moves or renames a task. A job in
Redis keeps the name that it had when it was queued. A worker with the
new code cannot find the function of an old name, and the job fails.

Each OLD and NEW is a dotted name. A function name that is OLD, or that
starts with OLD and a dot, gets NEW in its place. Thus, 1 pair can
rename 1 task or all the tasks of a module.

The script changes the name in the data of each job and in its meta. It
also renames the last-run records of the scheduler. Thus, the System
page keeps the "Last ran" times. Without --apply, the script changes
nothing and only reports.

Run it while the programs are stopped. RUNBOOK.md has the full sequence.

Usage: venv/bin/python scripts/rename_stored_jobs.py OLD NEW [OLD NEW ...] [--apply]
"""

import collections
import logging
import os
import sys
import zlib

# The hash of the last-run times of the scheduler. scheduler.py writes
# 1 field for each function and schedule. A test pins this name.

LATEST_KEY = "fitzflix:cron:latest"


def new_name(name, pairs):
    """Return the new name of a function, or None if no pair applies."""

    if not isinstance(name, str):
        return None
    for old, new in pairs:
        if name == old:
            return new
        if name.startswith(old + "."):
            return new + name[len(old) :]
    return None


def rename_stored_jobs(connection, pairs, apply=False):
    """Rename the stored jobs and the scheduler records. Return the counts."""

    from rq.job import Job

    counts = collections.Counter()
    for raw in connection.scan_iter("rq:job:*", count=1000):
        key = raw.decode()
        if connection.type(key) != b"hash":
            continue
        try:
            job = Job.fetch(key[len("rq:job:") :], connection=connection)
            name = job.func_name
        except Exception:
            counts["jobs that the script cannot read"] += 1
            continue
        renamed = new_name(name, pairs)
        if renamed:
            counts[f"jobs with the status {job.get_status().value}"] += 1
            if apply:
                # rq keeps the name inside the compressed data of the job.

                job.func_name = renamed
                connection.hset(key, "data", zlib.compress(job.data))
        renamed_meta = new_name((job.meta or {}).get("func"), pairs)
        if renamed_meta:
            counts["jobs with the name in their meta"] += 1
            if apply:
                job.meta["func"] = renamed_meta
                job.save_meta()

    for field, value in connection.hgetall(LATEST_KEY).items():
        name, _, schedule = field.decode().partition("|")
        renamed = new_name(name, pairs)
        if renamed:
            counts["last-run records of the scheduler"] += 1
            if apply:
                connection.hset(LATEST_KEY, f"{renamed}|{schedule}", value)
                connection.hdel(LATEST_KEY, field)
    return counts


def main(arguments):
    """Read the pairs from the command line, then report or apply."""

    apply = "--apply" in arguments
    names = [argument for argument in arguments if argument != "--apply"]
    if not names or len(names) % 2:
        print(__doc__)
        return 2
    pairs = list(zip(names[0::2], names[1::2]))

    # The folder of this file is the first entry of the module path. The
    # app package is one folder up.

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    # The start of the app writes log lines. They are not part of the answer.

    logging.disable(logging.CRITICAL)

    from app import create_app

    app = create_app()
    with app.app_context():
        counts = rename_stored_jobs(app.redis, pairs, apply=apply)
    print("Renamed:" if apply else "To rename (nothing changed, add --apply):")
    for label, count in sorted(counts.items()):
        print(f"  {count:>5}  {label}")
    if not counts:
        print("  nothing has one of the old names")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
