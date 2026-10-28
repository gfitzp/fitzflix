"""Test that a successful import releases its staging reservation (#461).

Before, only the abandon and the remux paths released it. A finished
import waited for the grace sweep of a later reservation."""

import os


def test_a_finished_import_leaves_no_staging_reservation(app, sample_mkv, incoming_dir):
    import app.videos as videos
    from app.importing import STAGING_RESERVED_KEY

    basename = "Released Stage (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(sample_mkv, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())

    with app.app_context():
        assert videos.localization_task(source) is True
        assert app.redis.hexists(STAGING_RESERVED_KEY, basename)
        (move_job,) = [
            job
            for job in app.handoff_queue.jobs
            if job.func_name == "app.videos.move_localized_file"
        ]
        assert videos.move_localized_file(*move_job.args) is True
        (finalize_job,) = [
            job
            for job in app.sql_queue.jobs
            if job.func_name == "app.videos.finalize_localization"
        ]
        videos.finalize_localization(*finalize_job.args)

        from app.models import File

        assert File.query.filter_by(untouched_basename=basename).count() == 1
        assert not app.redis.hexists(STAGING_RESERVED_KEY, basename)
