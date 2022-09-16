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
    downloads = StartedJobRegistry("fitzflix-download", connection=current_app.redis)
    download_tasks_running = downloads.get_job_ids()

    details = {}

    if current_user.is_authenticated:

        # Count the number of localizations and transcodes in queue and running.
        # We're only interested in the task and transcode queues.

        details["count"] = (
            len(current_app.localize_queue.job_ids)
            + len(localization_tasks_running)
            + len(current_app.transcode_queue.job_ids)
            + len(transcodes_running)
            + len(download_tasks_running)
            # + len(current_app.download_queue.job_ids)
        )

        # Create list of localizations and transcodes currently running

        details["running"] = []

        for job_id in localization_tasks_running:
            job = current_app.localize_queue.fetch_job(job_id)
            if job:
                details["running"].append(
                    {
                        "id": job.id,
                        "status": job.get_status(),
                        "enqueued_at": job.enqueued_at,
                        "started_at": job.started_at,
                        "ended_at": job.ended_at,
                        "description": job.meta.get("description", job.description),
                        "progress": (
                            job.meta.get("progress", -1) if job is not None else 100
                        ),
                    }
                )

        for job_id in transcodes_running:
            job = current_app.transcode_queue.fetch_job(job_id)
            if job:
                details["running"].append(
                    {
                        "id": job.id,
                        "status": job.get_status(),
                        "enqueued_at": job.enqueued_at,
                        "started_at": job.started_at,
                        "ended_at": job.ended_at,
                        "description": job.meta.get("description", job.description),
                        "progress": (
                            job.meta.get("progress", -1) if job is not None else 100
                        ),
                    }
                )

        for job_id in download_tasks_running:
            job = current_app.download_queue.fetch_job(job_id)
            if job:
                details["running"].append(
                    {
                        "id": job.id,
                        "status": job.get_status(),
                        "enqueued_at": job.enqueued_at,
                        "started_at": job.started_at,
                        "ended_at": job.ended_at,
                        "description": job.meta.get("description", job.description),
                        "progress": (
                            job.meta.get("progress", -1) if job is not None else 100
                        ),
                    }
                )

        details["running"] = sorted(details["running"], key=lambda d: d["started_at"])

        # Create list of all localizations and transcodes in queue

        details["all"] = []
        for job_id in localization_tasks_running:
            job = current_app.localize_queue.fetch_job(job_id)
            if job:
                details["all"].append(
                    {
                        "id": job.id,
                        "status": job.get_status(),
                        "enqueued_at": job.enqueued_at,
                        "started_at": job.started_at,
                        "ended_at": job.ended_at,
                        "description": job.meta.get("description", job.description),
                    }
                )

        for job_id in transcodes_running:
            job = current_app.transcode_queue.fetch_job(job_id)
            if job:
                details["all"].append(
                    {
                        "id": job.id,
                        "status": job.get_status(),
                        "enqueued_at": job.enqueued_at,
                        "started_at": job.started_at,
                        "ended_at": job.ended_at,
                        "description": job.meta.get("description", job.description),
                    }
                )

        for job_id in download_tasks_running:
            job = current_app.download_queue.fetch_job(job_id)
            if job:
                details["all"].append(
                    {
                        "id": job.id,
                        "status": job.get_status(),
                        "enqueued_at": job.enqueued_at,
                        "started_at": job.started_at,
                        "ended_at": job.ended_at,
                        "description": job.meta.get("description", job.description),
                    }
                )

        for job_id in current_app.localize_queue.job_ids:
            job = current_app.localize_queue.fetch_job(job_id)
            if job:
                details["all"].append(
                    {
                        "id": job.id,
                        "status": job.get_status(),
                        "enqueued_at": job.enqueued_at,
                        "started_at": job.started_at,
                        "ended_at": job.ended_at,
                        "description": job.meta.get("description", job.description),
                    }
                )

        for job_id in current_app.transcode_queue.job_ids:
            job = current_app.transcode_queue.fetch_job(job_id)
            if job:
                details["all"].append(
                    {
                        "id": job.id,
                        "status": job.get_status(),
                        "enqueued_at": job.enqueued_at,
                        "started_at": job.started_at,
                        "ended_at": job.ended_at,
                        "description": job.meta.get("description", job.description),
                    }
                )

#         for job_id in current_app.download_queue.job_ids:
#             job = current_app.download_queue.fetch_job(job_id)
#             if job:
#                 details["all"].append(
#                     {
#                         "id": job.id,
#                         "status": job.get_status(),
#                         "enqueued_at": job.enqueued_at,
#                         "started_at": job.started_at,
#                         "ended_at": job.ended_at,
#                         "description": job.meta.get("description", job.description),
#                     }
#                 )

        details["all"] = sorted(
            details["all"],
            key=lambda d: (
                d["started_at"] is None,
                d["started_at"],
                d["enqueued_at"] is None,
                d["enqueued_at"],
            ),
        )

        for i, task in enumerate(details["all"]):
            details["all"][i]["position"] = i + 1

        return jsonify(details)

    else:

        # The user could not be authenticated, return a 401 http error code

        return jsonify(details), 401
