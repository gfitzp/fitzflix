"""Test the guarded hand-offs to the cataloging and the in-place retry (#437).

The move stage queues the cataloging, and the cataloging books its own
retry while a volume is dead. A failed enqueue must release the title
lock and remove the copies. The source stays for the hourly sweep. The
sweep also queues again a failed in-place localization of a library
file."""

import os


def _lock_watch(app, monkeypatch):
    unlocked = []
    real_unlock = app.lock_manager.unlock
    monkeypatch.setattr(
        app.lock_manager,
        "unlock",
        lambda lock: (unlocked.append(lock), real_unlock(lock))[1],
    )
    return unlocked


def _incoming(sample_mkv, incoming_dir, basename):
    os.makedirs(incoming_dir, exist_ok=True)
    source = os.path.join(incoming_dir, basename)
    with open(sample_mkv, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())
    return source


def test_a_failed_finalize_enqueue_releases_the_lock_and_the_copies(
    app, sample_mkv, incoming_dir, monkeypatch
):
    import app.videos as videos

    basename = "Finalize Fault (2021) - [DVD].mkv"
    source = _incoming(sample_mkv, incoming_dir, basename)
    unlocked = _lock_watch(app, monkeypatch)

    def refuse(*args, **kwargs):
        raise ConnectionError("Redis is away")

    try:
        with app.app_context():
            assert videos.localization_task(source) is True
            (move,) = [
                job
                for job in app.handoff_queue.jobs
                if job.func_name == "app.videos.move_localized_file"
            ]
            monkeypatch.setattr(app.sql_queue, "enqueue", refuse)
            assert videos.move_localized_file(*move.args) is False

        title_locks = [lock for lock in unlocked if "Finalize Fault" in lock.resource]
        assert len(title_locks) == 1
        assert app.redis.get(title_locks[0].resource) is None
        assert os.path.exists(source)
        staging = app.config["STAGING_DIR"]
        assert not [n for n in os.listdir(staging) if "Finalize Fault" in n]
        folder = os.path.join(app.config["MOVIE_LIBRARY"], "Finalize Fault (2021)")
        assert not os.path.isdir(folder) or not [
            n for n in os.listdir(folder) if "Finalize Fault" in n
        ]
    finally:
        if os.path.exists(source):
            os.remove(source)


def test_a_failed_dead_volume_retry_releases_the_lock(app, tmp_path, monkeypatch):
    import app.importing as importing
    import app.videos as videos

    hidden = tmp_path / ".Dead Retry (2021) - [DVD].mkv"
    hidden.write_bytes(b"library copy")
    staged = tmp_path / "staged.mkv"
    staged.write_bytes(b"staged copy")
    source = tmp_path / "Dead Retry (2021) - [DVD].mkv"
    source.write_bytes(b"source")

    def refuse(*args, **kwargs):
        raise ConnectionError("Redis is away")

    monkeypatch.setattr(videos, "_dead_volumes", lambda paths: ["/Volumes/Movies"])
    monkeypatch.setattr(importing, "requeue", refuse)
    unlocked = _lock_watch(app, monkeypatch)
    with app.app_context():
        lock = app.lock_manager.lock('{"title": "Dead Retry"}', 60000)
        result = videos.finalize_localization(
            str(source),
            {"basename": source.name, "dirname": "Movies/Dead Retry (2021)"},
            lock,
            str(hidden),
            {},
            staged_output=str(staged),
        )
    assert result is False
    assert unlocked == [lock]
    assert app.redis.get(lock.resource) is None
    assert source.exists()
    assert not hidden.exists() and not staged.exists()


def test_the_sweep_queues_a_failed_in_place_import_again(app, tmp_path):
    """A killed localization of a library file is queued again, at most 3 times."""

    from rq.job import JobStatus
    from rq.registry import FailedJobRegistry

    import app.importing as importing

    library_file = tmp_path / "Killed In Place (2021) - [WEBDL-1080p].mkv"
    library_file.write_bytes(b"arr import")
    queue = app.import_queue
    registry = FailedJobRegistry(queue=queue)

    def fail_once():
        job = queue.enqueue("app.videos.localization_task", str(library_file))
        queue.remove(job)
        job.set_status(JobStatus.FAILED)
        registry.add(job, ttl=3600, exc_string="Work-horse terminated")
        return job

    def queued():
        return [
            job
            for job in queue.jobs
            if job.func_name == "app.videos.localization_task"
            and job.args == (str(library_file),)
        ]

    with app.app_context():
        for attempt in range(importing.MAX_IN_PLACE_RETRIES):
            fail_once()
            importing._retry_failed_in_place_imports()
            assert len(queued()) == 1, attempt
            for job in queued():
                job.delete()

        fail_once()
        importing._retry_failed_in_place_imports()
        assert queued() == []


def test_an_in_place_retry_counts_only_a_queued_job_in_a_fixed_window(
    app, tmp_path, monkeypatch
):
    """A sweep that queues nothing does not count, and the window does not move (follow-up #437)."""

    from rq.job import JobStatus
    from rq.registry import FailedJobRegistry

    import app.importing as importing
    from app import path_tag

    library_file = tmp_path / "Window In Place (2021) - [WEBDL-1080p].mkv"
    library_file.write_bytes(b"arr import")
    queue = app.import_queue
    job = queue.enqueue("app.videos.localization_task", str(library_file))
    queue.remove(job)
    job.set_status(JobStatus.FAILED)
    FailedJobRegistry(queue=queue).add(job, ttl=3600, exc_string="killed")
    counter = importing.IN_PLACE_RETRIES_KEY.format(path_tag(str(library_file)))

    with app.app_context():
        monkeypatch.setattr(importing, "enqueue_localization", lambda *a, **k: None)
        importing._retry_failed_in_place_imports()
        assert app.redis.get(counter) is None

        monkeypatch.setattr(importing, "enqueue_localization", lambda *a, **k: object())
        importing._retry_failed_in_place_imports()
        assert app.redis.get(counter) == b"1"
        app.redis.expire(counter, 100)
        importing._retry_failed_in_place_imports()
        assert app.redis.get(counter) == b"2"
        assert app.redis.ttl(counter) <= 100


def _refuse(*args, **kwargs):
    raise ConnectionError("Redis is away")


def _finalize_fixture(app, tmp_path, name, media_library="Movies"):
    """Return (source, hidden, staged, details, lock) for a cataloging run."""

    hidden = tmp_path / f".{name}"
    hidden.write_bytes(b"library copy")
    staged = tmp_path / f"staged-{name}"
    staged.write_bytes(b"staged copy")
    source = tmp_path / name
    source.write_bytes(b"source")
    folder = name.split(" - ")[0]
    details = {
        "basename": name,
        "dirname": f"{media_library}/{folder}",
        "file_path": f"{media_library}/{folder}/{name}",
        "media_library": media_library,
    }
    lock = app.lock_manager.lock(f'{{"title": "{folder}"}}', 60000)
    return source, hidden, staged, details, lock


def _assert_kept(app, lock, unlocked, source, hidden, staged):
    """The source stays for the sweep. The lock and the copies go (#521)."""

    assert lock in unlocked
    assert app.redis.get(lock.resource) is None
    assert source.exists()
    assert not hidden.exists() and not staged.exists()


def test_a_failed_sonarr_wait_keeps_the_source(app, tmp_path, monkeypatch):
    import app.importing as importing
    import app.videos as videos

    monkeypatch.setattr(
        importing, "resolve_import_series", lambda details: ("wait", None)
    )
    monkeypatch.setattr(importing, "requeue", _refuse)
    unlocked = _lock_watch(app, monkeypatch)
    with app.app_context():
        source, hidden, staged, details, lock = _finalize_fixture(
            app, tmp_path, "Sonarr Wait Fault - S01E01 - [HDTV-720p].mkv", "TV Shows"
        )
        result = videos.finalize_localization(
            str(source), details, lock, str(hidden), {}, staged_output=str(staged)
        )
    assert result is False
    _assert_kept(app, lock, unlocked, source, hidden, staged)


def test_a_failed_vanished_copy_retry_keeps_the_source(app, tmp_path, monkeypatch):
    import errno

    import app.importing as importing
    import app.videos as videos

    def lost(src, dst):
        raise OSError(errno.ENOENT, "the mount lost the copy")

    monkeypatch.setattr(importing, "_build_records", lambda *a, **k: None)
    monkeypatch.setattr(videos, "_rename_with_retries", lost)
    monkeypatch.setattr(importing, "requeue", _refuse)
    unlocked = _lock_watch(app, monkeypatch)
    with app.app_context():
        source, hidden, staged, details, lock = _finalize_fixture(
            app, tmp_path, "Vanished Fault (2021) - [DVD].mkv"
        )
        hidden.unlink()
        result = videos.finalize_localization(
            str(source), details, lock, str(hidden), {}, staged_output=str(staged)
        )
    assert result is False
    _assert_kept(app, lock, unlocked, source, hidden, staged)


def test_a_failed_hand_off_to_the_move_keeps_the_source(app, tmp_path, monkeypatch):
    import os as real_os
    from types import SimpleNamespace

    import app.importing as importing
    import app.videos as videos

    class OtherVolume:
        """Report the folder of the hidden copy on another volume."""

        def __getattr__(self, name):
            return getattr(real_os, name)

        def stat(self, path, *args, **kwargs):
            result = real_os.stat(path, *args, **kwargs)
            if path == str(tmp_path):
                return SimpleNamespace(st_dev=result.st_dev + 1)
            return result

    monkeypatch.setattr(importing, "_build_records", lambda *a, **k: None)
    monkeypatch.setattr(importing, "os", OtherVolume())
    monkeypatch.setattr(importing, "enqueue_task", _refuse)
    unlocked = _lock_watch(app, monkeypatch)
    with app.app_context():
        source, hidden, staged, details, lock = _finalize_fixture(
            app, tmp_path, "Hand Off Fault (2021) - [DVD].mkv"
        )
        result = videos.finalize_localization(
            str(source), details, lock, str(hidden), {}, staged_output=str(staged)
        )
    assert result is False
    _assert_kept(app, lock, unlocked, source, hidden, staged)


def test_a_failed_space_wait_inside_the_copy_keeps_the_source(
    app, incoming_dir, monkeypatch
):
    import errno

    import app.importing as importing
    import app.videos as videos
    from tests.test_localization import space_move_fixture

    def full(src, dst):
        raise OSError(errno.ENOSPC, "No space left on device")

    basename = "Space Wait Fault (2021) - [DVD].mkv"
    source, hidden, details = space_move_fixture(app, incoming_dir, basename)
    monkeypatch.setattr(videos, "inspect_localized_file", lambda *a, **k: {})
    monkeypatch.setattr(videos, "_rename_with_retries", full)
    monkeypatch.setattr(importing, "requeue", _refuse)
    unlocked = _lock_watch(app, monkeypatch)
    try:
        with app.app_context():
            lock = app.lock_manager.lock('{"title": "Space Wait Fault"}', 60000)
            assert videos.move_localized_file(source, details, lock, hidden) is False
        assert lock in unlocked
        assert os.path.exists(source)
        assert not os.path.exists(hidden)
    finally:
        for path in (source, hidden):
            if os.path.exists(path):
                os.remove(path)


def test_a_failed_dead_volume_retry_of_the_move_releases_the_lock(
    app, tmp_path, monkeypatch
):
    """The move books its volume retry inside the hand-off handler (follow-up #521).

    A Redis fault there failed the job. The title lock, the hidden output
    and the reservation stayed."""

    import app.importing as importing
    import app.videos as videos

    hidden = tmp_path / ".Move Retry (2021) - [DVD].mkv"
    hidden.write_bytes(b"localized output")
    source = tmp_path / "Move Retry (2021) - [DVD].mkv"
    source.write_bytes(b"source")

    def refuse(*args, **kwargs):
        raise ConnectionError("Redis is away")

    monkeypatch.setattr(videos, "_dead_volumes", lambda paths: ["/Volumes/Movies"])
    monkeypatch.setattr(importing, "requeue", refuse)
    unlocked = _lock_watch(app, monkeypatch)
    with app.app_context():
        lock = app.lock_manager.lock('{"title": "Move Retry"}', 60000)
        result = videos.move_localized_file(
            str(source),
            {"basename": source.name, "dirname": "Movies/Move Retry (2021)"},
            lock,
            str(hidden),
        )
    assert result is False
    assert unlocked == [lock]
    assert app.redis.get(lock.resource) is None
    assert source.exists()
    assert not hidden.exists()
