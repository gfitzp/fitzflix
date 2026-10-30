"""Test the staging reservations of the imports that run at the same time (#294)."""

import json
import os
import time

import pytest

from app import importing
from app.importing import (
    STAGING_MARGIN,
    STAGING_RESERVED_KEY,
    release_staging_reservation,
    reserve_staging,
    set_staging_reservation,
    staging_outstanding,
)

from tests.test_scheduling import scheduled_jobs

GB = 1024**3


@pytest.fixture
def staging(app):
    """Return the staging directory, empty before and after the test."""

    path = app.config["STAGING_DIR"]
    os.makedirs(path, exist_ok=True)
    for name in os.listdir(path):
        os.remove(os.path.join(path, name))
    yield path
    for name in os.listdir(path):
        os.remove(os.path.join(path, name))


@pytest.fixture
def volume(monkeypatch):
    """Fake the size of the staging volume. Set volume["free"] in a test."""

    sizes = {"total": 1000 * GB, "free": 500 * GB}
    monkeypatch.setattr(
        importing, "_staging_usage", lambda path: (sizes["total"], sizes["free"])
    )
    return sizes


def _plant(directory, name, size):
    """Make a sparse file of the given size."""

    path = os.path.join(directory, name)
    with open(path, "wb") as f:
        f.truncate(size)
    return path


def test_a_reservation_takes_the_space(app, staging, volume):
    with app.app_context():
        assert (
            reserve_staging(
                "A (2020) - [Bluray-2160p].mkv",
                200 * GB,
                ["A (2020) - [Bluray-2160p].mkv"],
                staging,
            )
            == "ok"
        )
        # 500 free - 200 for A - 20 margin leaves 280.
        assert (
            reserve_staging(
                "B (2020) - [Bluray-2160p].mkv",
                300 * GB,
                ["B (2020) - [Bluray-2160p].mkv"],
                staging,
            )
            == "wait"
        )
        assert (
            reserve_staging(
                "B (2020) - [Bluray-2160p].mkv",
                270 * GB,
                ["B (2020) - [Bluray-2160p].mkv"],
                staging,
            )
            == "ok"
        )


def test_the_bytes_already_written_count_against_the_reservation(app, staging, volume):
    with app.app_context():
        set_staging_reservation(
            "A (2020) - [Bluray-2160p].mkv",
            100 * GB,
            ["A (2020) - [Bluray-2160p].mkv", "A (2020) - [Bluray-1080p].mkv"],
        )
        _plant(staging, "A (2020) - [Bluray-2160p].mkv", 30 * GB)
        _plant(staging, ".A (2020) - [Bluray-1080p].mkv", 20 * GB)
        _plant(staging, ".A (2020) - [Bluray-1080p].mkv.sdr-video.mkv", 5 * GB)
        _plant(staging, "Other (2020) - [DVD].mkv", 7 * GB)
        assert staging_outstanding(staging) == 45 * GB
        assert (
            staging_outstanding(staging, exclude="A (2020) - [Bluray-2160p].mkv") == 0
        )


def test_a_reservation_with_no_files_expires_after_the_grace_period(app, staging):
    with app.app_context():
        set_staging_reservation(
            "Fresh (2020) - [DVD].mkv", 10 * GB, ["Fresh (2020) - [DVD].mkv"]
        )
        app.redis.hset(
            STAGING_RESERVED_KEY,
            "Gone (2020) - [DVD].mkv",
            json.dumps(
                {
                    "bytes": 50 * GB,
                    "stems": ["Gone (2020) - [DVD]"],
                    "at": time.time() - 3600,
                }
            ),
        )
        # The fresh reservation has no files yet, but it is in its grace period.
        assert staging_outstanding(staging) == 10 * GB
        assert app.redis.hexists(STAGING_RESERVED_KEY, "Fresh (2020) - [DVD].mkv")
        assert not app.redis.hexists(STAGING_RESERVED_KEY, "Gone (2020) - [DVD].mkv")

        release_staging_reservation("Fresh (2020) - [DVD].mkv")
        assert staging_outstanding(staging) == 0


def test_a_file_too_big_for_the_volume_is_not_staged(app, staging, volume):
    with app.app_context():
        volume["total"] = 100 * GB
        assert (
            reserve_staging(
                "Huge (2020) - [Bluray-2160p].mkv",
                100 * GB - STAGING_MARGIN + 1,
                ["x"],
                staging,
            )
            == "too big"
        )


def test_localization_waits_for_staging_space(
    app, sample_mkv, incoming_dir, staging, volume
):
    """Another import holds the space. The import waits and keeps its source."""

    from app import tasks
    from tests.test_localization import rejected_files

    basename = "Staging Wait (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    os.makedirs(incoming_dir, exist_ok=True)
    with open(sample_mkv, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())
    stamp = time.time() - 3600
    os.utime(source, (stamp, stamp))

    volume["free"] = 30 * GB
    try:
        with app.app_context():
            set_staging_reservation(
                "Other (2021) - [Bluray-2160p].mkv",
                15 * GB,
                ["Other (2021) - [Bluray-2160p].mkv"],
            )
            assert importing.localization_task(source) is True

            waits = [job.id for job in scheduled_jobs(app.import_queue)]
            assert (
                tasks.requeue_job_id(
                    "app.videos.localization_task",
                    source,
                    {
                        "transient_retries": 0,
                        "completeness_retries": 0,
                        "space_waits": 1,
                        "tmdb_retries": 0,
                    },
                )
                in waits
            )
            assert os.listdir(staging) == []
            assert len(app.handoff_queue) == 0
            assert basename not in rejected_files(app)
            assert os.path.exists(source)
            assert not app.redis.hexists(STAGING_RESERVED_KEY, basename)

            # The other import finished. Its reservation goes away.
            release_staging_reservation("Other (2021) - [Bluray-2160p].mkv")
            assert importing.localization_task(source, space_waits=1) is True
            (move_job,) = [
                job
                for job in app.handoff_queue.jobs
                if job.func_name == "app.videos.move_localized_file"
            ]
            hidden = move_job.args[3]
            assert hidden.startswith(staging)

            # The reservation now covers only the localized file.
            entry = json.loads(app.redis.hget(STAGING_RESERVED_KEY, basename))
            assert entry["bytes"] == os.path.getsize(hidden)
            move_job.delete()
            os.remove(hidden)
    finally:
        if os.path.exists(source):
            os.remove(source)


def test_the_extras_of_2_films_get_their_own_staged_output():
    """The library name of an extra is only the extra, for example "Trailer.mkv"."""

    first = importing.staged_output_path(
        "/staging", "First Film (2001) - Trailer - Trailer [DVD].mkv", "Trailer.mkv"
    )
    second = importing.staged_output_path(
        "/staging", "Second Film (2002) - Trailer - Trailer [DVD].mp4", "Trailer.mkv"
    )
    assert first != second
    assert (
        first == "/staging/.First Film (2001) - Trailer - Trailer [DVD].localized.mkv"
    )
    assert second.endswith(".localized.mkv")


def test_no_reservation_without_the_lock(app, staging, volume, monkeypatch):
    """A held lock makes the import wait. It does not reserve unlocked (#478)."""

    monkeypatch.setattr(importing.time, "sleep", lambda seconds: None)
    with app.app_context():
        held = app.lock_manager.lock(importing.STAGING_RESERVE_LOCK, 30000)
        assert held
        try:
            name = "Locked (2020) - [Bluray-2160p].mkv"
            assert reserve_staging(name, 10 * GB, [name], staging) == "lock"
            assert not app.redis.hexists(STAGING_RESERVED_KEY, name)
        finally:
            app.lock_manager.unlock(held)


def test_a_busy_staging_lock_is_not_a_space_wait(
    app, sample_mkv, incoming_dir, staging, volume, monkeypatch, caplog
):
    """The import tries again soon. The space-wait counter stays (#478)."""

    import logging

    from app import tasks

    monkeypatch.setattr(importing, "reserve_staging", lambda *args: "lock")
    basename = "Lock Wait (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    os.makedirs(incoming_dir, exist_ok=True)
    with open(sample_mkv, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())
    stamp = time.time() - 3600
    os.utime(source, (stamp, stamp))
    try:
        with app.app_context(), caplog.at_level(logging.INFO):
            assert importing.localization_task(source) is True
            waits = [job.id for job in scheduled_jobs(app.import_queue)]
            assert (
                tasks.requeue_job_id(
                    "app.videos.localization_task",
                    source,
                    {
                        "transient_retries": 0,
                        "completeness_retries": 0,
                        "space_waits": 0,
                        "tmdb_retries": 0,
                    },
                )
                in waits
            )
            assert "The staging lock is busy" in caplog.text
            assert "on staging. The other imports" not in caplog.text
            assert os.path.exists(source)
    finally:
        if os.path.exists(source):
            os.remove(source)
