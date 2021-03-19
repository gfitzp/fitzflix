from rq.registry import StartedJobRegistry

from flask import current_app, jsonify
from flask_login import current_user, login_required

from app import db
from app.api import bp


@bp.route("/queue-details")
@login_required
def queue_details():
    """Return the number of tasks in queue, and details on tasks currently running.

    This endpoint is checked every 5 seconds so the website can update the current number
    of tasks in queue, and the details of the tasks that are currently running.
    """

    localizations = StartedJobRegistry(
        "fitzflix-localize", connection=current_app.redis
    )
    localization_tasks_running = localizations.get_job_ids()
    transcodes = StartedJobRegistry("fitzflix-transcode", connection=current_app.redis)
    transcodes_running = transcodes.get_job_ids()
    tasks = StartedJobRegistry("fitzflix-tasks", connection=current_app.redis)
    tasks_running = tasks.get_job_ids()
    details = {}
    if current_user.is_authenticated:

        # Count the number of tasks in queue and number of tasks running.
        # We're only interested in the task and transcode queues.

        details["count"] = (
            len(current_app.localize_queue.job_ids)
            + len(localization_tasks_running)
            + len(current_app.transcode_queue.job_ids)
            + len(transcodes_running)
        )

        details["running"] = []

        # Get the details for the running jobs in the tasks queue

        for job_id in localization_tasks_running:
            job = current_app.localize_queue.fetch_job(job_id)
            if job:
                details["running"].append(
                    {
                        "job": job_id,
                        "description": job.meta.get("description", job.description),
                        "progress": (
                            job.meta.get("progress", -1) if job is not None else 100
                        ),
                    }
                )

        # Get the details for the running jobs in the transcoding queue

        for job_id in transcodes_running:
            job = current_app.transcode_queue.fetch_job(job_id)
            if job:
                details["running"].append(
                    {
                        "job": job_id,
                        "description": job.meta.get("description", job.description),
                        "progress": (
                            job.meta.get("progress", -1) if job is not None else 100
                        ),
                    }
                )

        # Get the details for any tasks currently being processed

        for job_id in tasks_running:
            job = current_app.task_queue.fetch_job(job_id)
            if job:
                details["running"].append(
                    {
                        "job": job_id,
                        "description": job.meta.get("description", job.description),
                        "progress": (
                            job.meta.get("progress", -1) if job is not None else 100
                        ),
                    }
                )

        details["localization_queue"] = []

        running_position = 1
        for job_id in localization_tasks_running:
            job = current_app.localize_queue.fetch_job(job_id)
            if job:
                details["localization_queue"].append(
                    {
                        "id": job.id,
                        "position": running_position,
                        "status": job.get_status(),
                        "enqueued_at": job.enqueued_at,
                        "started_at": job.started_at,
                        "ended_at": job.ended_at,
                    }
                )
                running_position = running_position + 1

        for job_id in current_app.localize_queue.job_ids:
            job = current_app.localize_queue.fetch_job(job_id)
            if job:
                details["localization_queue"].append(
                    {
                        "id": job.id,
                        "position": int(job.get_position()) + running_position,
                        "status": job.get_status(),
                        "enqueued_at": job.enqueued_at,
                        "started_at": job.started_at,
                        "ended_at": job.ended_at,
                    }
                )

        return jsonify(details)

    else:

        # The user could not be authenticated, return a 401 http error code

        return jsonify(details), 401
