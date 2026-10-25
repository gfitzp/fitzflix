"""The helpers of the localization tests: test_localization and its 2 splits.

The import of the task modules waits for the call. Thus, this module is
safe at collection time."""

import os
import time


def settle(path, age_seconds=3600):
    """Backdate the mtime of a file. Then the completeness gate trusts it."""

    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))


def run_full_chain(app, source):
    """Run localization -> move -> finalize. Return the enqueued finalize args."""

    import app.videos as videos

    assert videos.localization_task(source) is True
    move_jobs = [
        job
        for job in app.handoff_queue.jobs
        if job.func_name == "app.videos.move_localized_file"
    ]
    assert len(move_jobs) == 1
    assert videos.move_localized_file(*move_jobs[0].args) is True

    finalize_jobs = [
        job
        for job in app.sql_queue.jobs
        if job.func_name == "app.videos.finalize_localization"
    ]
    assert len(finalize_jobs) == 1
    videos.finalize_localization(*finalize_jobs[0].args)
    return finalize_jobs[0].args


def rejected_files(app):
    """Return all real files under the rejects directory."""

    return [
        name
        for _, _, files in os.walk(app.config["REJECTS_DIR"])
        for name in files
        if name != ".DS_Store"
    ]


def separate_library_volume(app, monkeypatch):
    """Make the library directories report a device of their own.

    The test tree keeps staging and the library on 1 disk. Thus, the
    library copy is a plain rename. The real library is on the NAS, so
    move_localized_file copies across volumes. This fake makes the test
    take that path."""

    import stat as stat_module

    real_stat = os.stat
    library = app.config["LIBRARY_DIR"]

    def fake_stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if str(path).startswith(library) and stat_module.S_ISDIR(result.st_mode):
            values = list(result)
            values[2] = result.st_dev + 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(os, "stat", fake_stat)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
