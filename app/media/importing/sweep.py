"""The hourly sweep of the import directory, and the retry of the failed imports in the library."""

import os
import traceback
from rq.job import Job
from flask import current_app
from werkzeug.local import LocalProxy
from app import db, get_app, importable_basename, path_tag
from app.models import RefQuality
from app.media.importing import chain

app = LocalProxy(get_app)


def manual_import_task():
    """Scan the Import directory and import the files that are not in the queue."""

    with app.app_context():
        try:
            import_directory_files = os.listdir(current_app.config["IMPORT_DIR"])
            import_directory_files.sort()
            qualities = (
                db.session.query(RefQuality.quality_title)
                .order_by(RefQuality.preference.asc())
                .all()
            )
            qualities = [quality_title for (quality_title,) in qualities]

            # A filename can contain more than one quality string. Record the
            # files already handled. Then each file is enqueued one time per
            # scan.

            handled_basenames = set()

            for quality_title in qualities:
                for file in import_directory_files:
                    if (
                        importable_basename(os.path.basename(file))
                        and f"[{quality_title}]" in file
                        and os.path.basename(file) not in handled_basenames
                        and os.path.isfile(
                            os.path.join(current_app.config["IMPORT_DIR"], file)
                        )
                    ):
                        handled_basenames.add(os.path.basename(file))
                        lock = current_app.lock_manager.lock(
                            os.path.basename(file), 30000
                        )
                        if lock:
                            if chain.enqueue_localization(
                                os.path.join(current_app.config["IMPORT_DIR"], file),
                                quiet=True,
                            ):
                                current_app.logger.info(
                                    f"'{os.path.basename(file)}' Found in import directory"
                                )

                            current_app.lock_manager.unlock(lock)

            _retry_failed_in_place_imports()

        except Exception:
            current_app.logger.error(traceback.format_exc())

        else:
            return True


# The retries of the failed in-place localization of 1 path: at most 3
# in each window of 7 days. The window starts at the first retry, and
# the later retries do not move it (follow-up #437). When the counter
# key expires, a path that fails again gets 3 more retries (#693).

IN_PLACE_RETRIES_KEY = "fitzflix:in-place-import-retries:{}"
IN_PLACE_RETRY_WINDOW = 7 * 86400
MAX_IN_PLACE_RETRIES = 3


def _retry_failed_in_place_imports():
    """Queue again the failed localizations of the files outside the import directory (#437).

    A Sonarr or Radarr import reads the file at its library path. A kill
    or a timeout of its localization leaves the file in the library with
    no record, and the arr event does not come again. The scan of the
    import directory above never sees such a file.

    A path gets at most MAX_IN_PLACE_RETRIES retries in each
    IN_PLACE_RETRY_WINDOW. A failed job stays in the registry for
    FAILURE_TTL, and each failed retry adds a failed job. Thus, a path
    that fails each time gets 3 retries each week while it fails. This
    is on purpose: a file that met an outage of the database comes this
    way (#681), and a later week can import it (#693)."""

    from rq.registry import FailedJobRegistry

    queue = current_app.import_queue
    import_dir = os.path.abspath(current_app.config["IMPORT_DIR"])
    redis = current_app.redis
    for job in Job.fetch_many(
        FailedJobRegistry(queue=queue).get_job_ids(), connection=redis
    ):
        if (
            job is None
            or job.func_name != "app.media.importing.localization.localization_task"
        ):
            continue
        path = job.args[0] if job.args else job.kwargs.get("file_path")
        if not path or os.path.abspath(os.path.dirname(path)) == import_dir:
            continue
        if not os.path.isfile(path) or chain.already_cataloged(path):
            continue
        if chain.localization_pending(path):
            continue
        counter = IN_PLACE_RETRIES_KEY.format(path_tag(path))
        if int(redis.get(counter) or 0) >= MAX_IN_PLACE_RETRIES:
            continue
        if not chain.enqueue_localization(path, quiet=True):
            continue
        # Only a queued retry counts. The first retry starts the window,
        # and the later retries do not move it.
        tries = redis.incr(counter)
        if tries == 1:
            redis.expire(counter, IN_PLACE_RETRY_WINDOW)
        current_app.logger.warning(
            f"'{path}' Its in-place localization failed. It is queued again "
            f"(retry {tries} of {MAX_IN_PLACE_RETRIES})"
        )
