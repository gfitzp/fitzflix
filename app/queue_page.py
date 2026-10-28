"""The read of the background jobs for the queue page and the navbar badge.

The queue page lists the jobs of the 5 pipeline queues. Each page shows
the running banners of those jobs and the navbar badge counts them.
User.get_queue_details and User.get_queue_count call this module."""

from datetime import datetime, timezone

from flask import current_app
from rq.exceptions import InvalidJobOperation, NoSuchJobError
from rq.registry import ScheduledJobRegistry, StartedJobRegistry


def scheduled_entries(registry):
    """Return (job id, aware UTC time) for each job of a scheduled registry.

    One zrange reads the ids and the times together (#511). The
    scheduler can remove a due job between 2 reads. Then rq's
    get_scheduled_time raised NoSuchJobError, and a page answered 500."""

    return [
        (
            registry.parse_job_id(raw_id),
            datetime.fromtimestamp(score, tz=timezone.utc),
        )
        for raw_id, score in registry.connection.zrange(
            registry.key, 0, -1, withscores=True
        )
    ]


def listed_queues():
    """Return the queues that the queue page lists, each with a job filter.

    The filter is None for a queue whose jobs all list. The SQL queue
    also runs the cataloging of each import. Its other work (the SQL
    refreshes) is not file work. Thus, only its pipeline stages list.
    Before, a file that was in cataloging had no job row. Its trail
    showed as a "no job in queue" row at the end of the table."""

    from app.pipeline import STAGES

    return (
        (current_app.handoff_queue, None),
        (current_app.import_queue, None),
        (current_app.transcode_queue, None),
        (current_app.file_queue, None),
        (current_app.sql_queue, STAGES),
    )


def listed_jobs():
    """Return the running jobs and the queued jobs that the queue page lists.

    Each list holds the rq jobs in the order of the queues and then
    of each queue."""

    running = []
    queued = []
    for queue, only in listed_queues():
        for target, job_ids in (
            (running, StartedJobRegistry(queue=queue).get_job_ids()),
            (queued, queue.job_ids),
        ):
            # 1 pipeline per list, not 1 round trip per job (#526). A
            # missing hash gives None. Unlike fetch_job, this is no write
            # from a page view.
            for job in queue.job_class.fetch_many(job_ids, connection=queue.connection):
                if job is not None and (only is None or job.func_name in only):
                    target.append(job)
    return running, queued


def queue_details():
    """Return the running and queued background jobs for the queue page.

    This method merges the pipeline queues into one ordered list with
    queue positions. The running banners at the top of each page and
    the running rows of the queue table have the same order.
    """

    # The running banners and the running rows keep their order by
    # the time that each FILE first started to run. This is the
    # original banner-order request from Glenn. The first file to
    # start is at the top. A newer file comes below it. The work of a
    # file steps between queues as it progresses. Each step is a new
    # job with a new started_at. Before, the table sorted by that
    # time. Thus, a file moved down the table at each step, and the
    # table and the banners did not agree. The first_run anchor of
    # the pipeline trail survives the steps. A job without a trail
    # uses its own start.

    from app.pipeline import first_enqueued, first_run, front_times

    def file_started(job):
        """Return the time that the FILE of the job first started to run.

        The anchor of the trail is UTC with an offset (#405). An anchor
        of an older trail has no offset and is local wall clock. The
        value is aware UTC, the same as the own started_at of rq.
        flask-moment reads each time as UTC."""

        anchor = first_run(current_app.redis, job)
        if anchor:
            try:
                stamp = datetime.fromisoformat(anchor)
            except ValueError:
                return job.started_at
            if stamp.tzinfo is None:
                stamp = stamp.astimezone()
            return stamp.astimezone(timezone.utc)
        return job.started_at

    def enqueue_anchor(job):
        """Return the time that the FILE of the job first entered the pipeline.

        The Enqueued column stays the same as the work steps between
        queues. Each step is a new job with a new enqueued_at. A job
        outside the pipeline keeps its own enqueue time."""

        return first_enqueued(current_app.redis, job) or job.enqueued_at

    def banner_worthy(job):
        """Return True if a running job gets an alert at the top of the page.

        Frame pool work never gets one (Glenn, 2026-08-27). A
        replacement banner per round disrupts the game. It also shows
        that the pool changed. Even the 'Extracting a frame from X'
        banner of the nightly batch names films that will become
        answers. All these jobs still list on the queue page.

        The SQL worker never gets one either (Glenn, 2026-10-14). Its
        cataloging jobs list as rows only. Thus, the banners stay at
        1 for each of the 5 pipeline workers."""

        if job.origin == current_app.sql_queue.name:
            return False
        return not (job.func_name or "").startswith("app.frames.")

    def row(job, started_at):
        """Return the queue-page fields of 1 job."""

        # fetch_many already read the status. A second read would fail
        # for a hash that a Forget or a flush deleted meanwhile (#565).

        return {
            "id": job.id,
            "status": job.get_status(refresh=False),
            "enqueued_at": enqueue_anchor(job),
            "started_at": started_at,
            "ended_at": job.ended_at,
            "description": job.meta.get("description", job.description),
            # A check or finish job of a multi-job run (the Atmos twin)
            # shows the chips of the first job of the run. A booked
            # check names its wait ("checks again"), not "retries".
            "trail_job": job.meta.get("trail_job"),
            "scheduled_word": job.meta.get("scheduled_word"),
        }

    def time_key(value):
        """Return a sort key that puts a missing time last."""

        return (value is None, value or datetime.min.replace(tzinfo=timezone.utc))

    running_jobs, queued_jobs = listed_jobs()

    running = []
    for job in running_jobs:
        try:
            started_at = file_started(job)
            item = row(job, started_at)
        except (InvalidJobOperation, NoSuchJobError):
            # The job went away between the reads (#565). Skip its row.
            continue
        item["first_run"] = (
            started_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            if started_at
            else "9999"
        )
        item["progress"] = job.meta.get("progress", -1)
        running.append((job, item))

    # The sort is stable. Thus, 2 files with the same start keep the
    # same order in the banners and in the table.

    running.sort(key=lambda pair: time_key(pair[1]["started_at"]))

    details = {}
    details["count"] = len(running_jobs) + len(queued_jobs)
    details["running"] = [item for job, item in running if banner_worthy(job)]

    # A job that an admin moved to the front of its queue (#236) lists
    # before the other waiting jobs. The newest move is at the top,
    # because each move pushes the job onto the head of its queue.

    # The job at the head of its queue gets no front button. A move
    # does not change which job of that queue a worker takes next.

    fronted = front_times(current_app.redis, [job.id for job in queued_jobs])
    heads = set()
    for queue, only in listed_queues():
        head = current_app.redis.lindex(queue.key, 0)
        if head is not None:
            heads.add(head.decode() if isinstance(head, bytes) else head)
    queued = []
    for job in queued_jobs:
        try:
            item = row(job, None)
        except (InvalidJobOperation, NoSuchJobError):
            continue
        item["fronted"] = job.id in fronted
        item["at_head"] = job.id in heads
        queued.append(item)
    queued.sort(
        key=lambda item: (
            (0, -fronted[item["id"]])
            if item["fronted"]
            else (1, time_key(item["enqueued_at"]))
        )
    )

    details["all"] = [
        {
            key: value
            for key, value in item.items()
            if key not in ("first_run", "progress")
        }
        for job, item in running
    ] + queued
    for i, task in enumerate(details["all"]):
        task["position"] = i + 1

    # A deferred retry (a file that is still copying in, or a locked
    # title) is in the ScheduledJobRegistry of each queue. It is not
    # in the queue itself. Before, these retries were not visible
    # here. They showed only as amber chips on the in-flight list of
    # the File Activity page. The trail chips moved onto the queue
    # rows (Glenn, 2026-08). Now the queue page is the one place that
    # shows all the work in flight. Thus, the retries list too. They
    # come after the live queue, with no position, because they are
    # not in line yet.

    scheduled = []
    for queue, only in listed_queues():
        registry = ScheduledJobRegistry(queue=queue)
        entries = scheduled_entries(registry)

        # 1 pipeline, as listed_jobs does (#565). fetch_job removes a
        # missing id from the queue list. That is a write from a page view.

        jobs = queue.job_class.fetch_many(
            [job_id for job_id, _ in entries], connection=queue.connection
        )
        for job, (job_id, scheduled_for) in zip(jobs, entries):
            if job is not None and (only is None or job.func_name in only):
                try:
                    item = row(job, None)
                except (InvalidJobOperation, NoSuchJobError):
                    continue
                item["status"] = "scheduled"
                item["ended_at"] = None
                item["scheduled_for"] = scheduled_for
                item["position"] = None
                scheduled.append(item)

    scheduled.sort(key=lambda d: (d["scheduled_for"] is None, d["scheduled_for"] or ""))
    details["all"].extend(scheduled)

    return details


def queue_count():
    """Return the total of queued and running jobs for the navbar badge.

    The badge counts the same jobs that the queue page lists. Each page
    renders it (#526). Thus, a queue with no job filter only counts the
    job hashes that exist, in 1 pipeline. Only the SQL queue reads its
    jobs, for the stage filter."""

    total = 0
    for queue, only in listed_queues():
        job_ids = StartedJobRegistry(queue=queue).get_job_ids() + queue.job_ids
        if only is None:
            pipeline = queue.connection.pipeline()
            for job_id in job_ids:
                pipeline.exists(queue.job_class.key_for(job_id))
            total += sum(1 for exists in pipeline.execute() if exists)
        else:
            total += sum(
                1
                for job in queue.job_class.fetch_many(
                    job_ids, connection=queue.connection
                )
                if job is not None and job.func_name in only
            )
    return total
