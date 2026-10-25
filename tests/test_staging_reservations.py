"""Test the staging reservations of the imports that run at the same time (#294)."""

import json
import os
import subprocess
import time

import pytest

from app import importing, retry_job_id
from app.importing import (
    STAGING_MARGIN,
    STAGING_RESERVED_KEY,
    release_staging_reservation,
    reserve_staging,
    set_staging_reservation,
    staging_outstanding,
)

from tests.conftest import _TMP
from tests.test_scheduling import scheduled_jobs

GB = 1024**3


@pytest.fixture(scope="module")
def sample_mkv(app):
    """Build a 1-second Matroska file with an English audio track."""

    path = os.path.join(_TMP, "sample-staging.mkv")
    if not os.path.exists(path):
        subprocess.run(
            [
                app.config["FFMPEG_BIN"],
                "-f",
                "lavfi",
                "-i",
                "testsrc=duration=1:size=320x240:rate=10",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=1",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                "-metadata:s:a:0",
                "language=eng",
                "-shortest",
                "-y",
                path,
            ],
            check=True,
            capture_output=True,
        )
    return path


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
                retry_job_id(
                    "localization_task", f"'{basename}'", 0, 0, "staging1", "t0"
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
