"""Test the phases of the weekly S3 sync (#358).

Before the split, 1 try around the whole task logged a failure and
returned. Thus, a failed prune left the job FINISHED, and the reports
and the import of the uncataloged files did not run.
"""

from datetime import datetime, timedelta, timezone

import pytest


def _sync_env(app, monkeypatch, objects, fail_delete=None, fail_listing=False):
    """Patch the S3 side of the sync. Return the record of the calls."""

    from app import aws_storage

    calls = {"deletes": [], "delete_options": [], "mails": [], "uncataloged": []}

    def listing(bucket, prefix=""):
        if fail_listing:
            raise ConnectionError("S3 is away")
        return objects.get(prefix, [])

    def delete(key, **kwargs):
        if key == fail_delete:
            raise ConnectionError("S3 delete failed")
        calls["deletes"].append(key)
        calls["delete_options"].append(kwargs)

    class FakeClient:
        def put_object(self, **kwargs):
            pass

    monkeypatch.setattr(aws_storage, "get_matching_s3_objects", listing)
    monkeypatch.setattr(aws_storage, "aws_delete", delete)
    monkeypatch.setattr(aws_storage, "aws_restore", lambda key, **kw: None)
    monkeypatch.setattr(
        "app.email.send_email", lambda subject, **kw: calls["mails"].append(subject)
    )
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kw: FakeClient())
    monkeypatch.setattr(
        aws_storage,
        "_import_uncataloged",
        lambda roots: calls["uncataloged"].append(1),
        raising=False,
    )
    return calls


OLD = datetime.now(timezone.utc) - timedelta(days=30)


def _strays(*names):
    return {
        "untouched/": [
            {"Key": f"untouched/{name}", "Size": 1, "LastModified": OLD}
            for name in names
        ]
    }


def test_a_failed_prune_fails_the_job_and_still_reports(app, monkeypatch):
    """The prune fails on its second delete. The task raises at the end.

    The first delete is in the report mail, and the import of the
    uncataloged files still runs."""

    from app import aws_storage

    calls = _sync_env(
        app,
        monkeypatch,
        _strays("A Stray (2001) - [DVD].mkv", "B Stray (2001) - [DVD].mkv"),
        fail_delete="untouched/B Stray (2001) - [DVD].mkv",
    )
    real_prune = aws_storage._prune_unclaimed

    def ordered_prune(listing, job, unreferenced_files):
        listing = listing._replace(keys=sorted(listing.keys))
        return real_prune(listing, job, unreferenced_files)

    monkeypatch.setattr(aws_storage, "_prune_unclaimed", ordered_prune)

    with pytest.raises(RuntimeError, match="prune"):
        aws_storage.sync_aws_s3_storage_task()

    assert calls["deletes"] == ["untouched/A Stray (2001) - [DVD].mkv"]

    # The prune has its own claim set, exact in letter case. The claim
    # check of aws_delete folds case and would keep some orphans (#686).

    assert calls["delete_options"] == [{}]
    assert "Fitzflix - Deleted unreferenced AWS files" in calls["mails"]
    assert calls["uncataloged"] == [1]


def test_a_failed_listing_skips_the_prune_and_fails_the_job(app, monkeypatch):
    from app import aws_storage

    calls = _sync_env(app, monkeypatch, {}, fail_listing=True)

    with pytest.raises(RuntimeError, match="listing"):
        aws_storage.sync_aws_s3_storage_task()

    assert calls["deletes"] == []
    assert calls["uncataloged"] == [1]


def test_a_failed_file_pass_skips_the_prune(app, monkeypatch):
    """The prune deletes objects. It runs only after a clean file pass."""

    from app import aws_storage

    calls = _sync_env(app, monkeypatch, _strays("C Stray (2001) - [DVD].mkv"))

    def broken(listing, job):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(aws_storage, "_reconcile_files", broken)

    with pytest.raises(RuntimeError, match="file records"):
        aws_storage.sync_aws_s3_storage_task()

    assert calls["deletes"] == []
    assert calls["uncataloged"] == [1]


def test_a_clean_sync_returns_true(app, monkeypatch):
    from app import aws_storage

    calls = _sync_env(app, monkeypatch, _strays("D Stray (2001) - [DVD].mkv"))

    assert aws_storage.sync_aws_s3_storage_task() is True
    assert calls["deletes"] == ["untouched/D Stray (2001) - [DVD].mkv"]
    assert calls["uncataloged"] == [1]


def test_the_uncataloged_import_walks_each_root_once(app, monkeypatch, tmp_path):
    """The 2 identical walks of the movie and TV roots are 1 loop now."""

    from app import aws_storage, importing

    movies = tmp_path / "Movies" / "Film (2001)"
    shows = tmp_path / "TV" / "Show" / "Season 01"
    movies.mkdir(parents=True)
    shows.mkdir(parents=True)
    (movies / "Film (2001) - [DVD].mkv").write_bytes(b"x")
    (movies / "poster.jpg").write_bytes(b"x")
    (shows / "Show - S01E01 - [DVD].mkv").write_bytes(b"x")
    (shows / ".hidden.mkv").write_bytes(b"x")

    queued = []
    monkeypatch.setattr(
        importing, "enqueue_localization", lambda path: queued.append(path)
    )
    monkeypatch.setitem(app.config, "LIBRARY_DIR", str(tmp_path))
    with app.app_context():
        aws_storage._import_uncataloged(
            (str(tmp_path / "Movies"), str(tmp_path / "TV"))
        )

    assert sorted(queued) == [
        str(movies / "Film (2001) - [DVD].mkv"),
        str(shows / "Show - S01E01 - [DVD].mkv"),
    ]


def _row_with_library_file(app, tmp_path, title, size, data, **columns):
    """Make a film row with a library file below tmp_path. Return (id, path)."""

    from app import db
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        file = make_movie_file(
            make_movie(title, 2021), "Bluray-1080p", filesize_bytes=size, **columns
        )
        db.session.commit()
        file_id, relative = file.id, file.file_path
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    return file_id, str(path)


def test_the_walk_queues_a_library_file_that_is_not_the_file_of_its_record(
    app, tmp_path, monkeypatch
):
    """Radarr upgraded a file at the same path, and its import was lost (#750).

    The record has the path. Before, the walk asked only that. The file
    with the size of its record is not queued."""

    from app import aws_storage, importing, tracks

    _, changed = _row_with_library_file(app, tmp_path, "Upgraded Here", 9, b"new file")
    _, same = _row_with_library_file(app, tmp_path, "Same Here", 4, b"same")
    queued, scans = [], []
    monkeypatch.setattr(
        importing, "enqueue_localization", lambda path: queued.append(path) or True
    )
    monkeypatch.setattr(
        tracks, "_rescan_after_a_failed_edit", lambda *a, **k: scans.append(a)
    )
    monkeypatch.setitem(app.config, "LIBRARY_DIR", str(tmp_path))
    with app.app_context():
        aws_storage._import_uncataloged((str(tmp_path / "Movies"),))

    assert queued == [changed]
    assert scans == []


def test_the_walk_scans_a_changed_file_that_an_edit_of_fitzflix_wrote(
    app, tmp_path, monkeypatch
):
    """An edit that died after its write left the stale flag (#750).

    That file is the file of its record. A scan stores its rows and its
    size. An import would make it a new original."""

    from app import aws_storage, importing, tracks

    file_id, _ = _row_with_library_file(
        app, tmp_path, "Edit Died", 9, b"edited", aws_stale=True
    )
    queued, scans = [], []
    monkeypatch.setattr(
        importing, "enqueue_localization", lambda path: queued.append(path) or True
    )
    monkeypatch.setattr(
        tracks, "_rescan_after_a_failed_edit", lambda *a, **k: scans.append(a[0])
    )
    monkeypatch.setitem(app.config, "LIBRARY_DIR", str(tmp_path))
    with app.app_context():
        aws_storage._import_uncataloged((str(tmp_path / "Movies"),))

    assert queued == []
    assert scans == [file_id]


def test_the_uncataloged_phase_never_enters_a_hidden_directory(
    app, tmp_path, monkeypatch
):
    """The download folders of Sonarr and Radarr are in the library folders (#710).

    A download below .sabnzbd waits for Radarr or for a manual import.
    The phase must not send it to the import. A metadata directory of
    the NAS is not a library folder either."""

    from app import aws_storage, importing

    film = tmp_path / "Movies" / "Film (2002)"
    download = tmp_path / "Movies" / ".sabnzbd" / "Some.Film.1957.1080p.BluRay-GRP"
    screens = download / "Screens"
    metadata = film / "@eaDir" / "Film (2002) - [DVD].mkv"
    for folder in (film, screens, metadata):
        folder.mkdir(parents=True)
    (film / "Film (2002) - [DVD].mkv").write_bytes(b"x")
    (download / "Some.Film.1957.1080p.BluRay-GRP.mkv").write_bytes(b"x")
    (screens / "01.png").write_bytes(b"x")
    (metadata / "SYNOVIDEO_VIDEO_SCREENSHOT.jpg").write_bytes(b"x")

    queued = []
    monkeypatch.setattr(
        importing, "enqueue_localization", lambda path: queued.append(path)
    )
    monkeypatch.setitem(app.config, "LIBRARY_DIR", str(tmp_path))
    with app.app_context():
        aws_storage._import_uncataloged((str(tmp_path / "Movies"),))

    assert queued == [str(film / "Film (2002) - [DVD].mkv")]


def _absent_archived_film(app, title, year):
    """Make a rank-1 film whose archive is in S3 and whose library file is absent."""

    from app import db
    from tests.factories import make_movie, make_movie_file

    key = f"untouched/{title} ({year}) - [DVD].mkv"
    with app.app_context():
        make_movie_file(
            make_movie(title, year),
            "DVD",
            aws_key=key,
            aws_date_uploaded=OLD,
            aws_filesize_bytes=1,
        )
        db.session.commit()
    return key


def test_a_volume_that_drops_in_the_file_pass_stops_the_restores(app, monkeypatch):
    """The volume dies after the idle check. The first absent file stops the pass (#431)."""

    from app import aws_storage

    key = _absent_archived_film(app, "Dropped Mid Pass", 2001)
    calls = _sync_env(
        app,
        monkeypatch,
        {
            "untouched/": [
                {"Key": key, "Size": 1, "LastModified": OLD},
                {"Key": "untouched/E Stray (2001) - [DVD].mkv", "Size": 1},
            ]
        },
    )
    restores = []
    monkeypatch.setattr(
        aws_storage, "aws_restore", lambda key, **kw: restores.append(key)
    )
    checks = []

    def dead_after_the_idle_check(paths):
        checks.append(paths)
        return [] if len(checks) == 1 else ["/Volumes/Movies"]

    monkeypatch.setattr("app.videos._dead_volumes", dead_after_the_idle_check)
    errors = []
    monkeypatch.setattr(app.logger, "error", lambda *a, **k: errors.append(a))

    # A drop is a short NFS fault. The sync books itself again in 30
    # minutes, with no ERROR record (follow-up #431).
    with app.app_context():
        assert aws_storage.sync_aws_s3_storage_task() is True

    assert restores == []
    assert calls["deletes"] == []
    assert calls["uncataloged"] == []
    assert len(checks) == 2
    assert errors == []
    from tests.test_scheduling import scheduled_jobs

    (retry,) = [
        job
        for job in scheduled_jobs(app.maintenance_queue)
        if job.func_name == "app.videos.sync_aws_s3_storage_task"
    ]
    assert retry.kwargs == {"volume_drops": 1}


def test_the_sixth_volume_drop_fails_the_sync(app, monkeypatch):
    """The retries have a limit. The last drop is an ERROR and fails the job (follow-up #431)."""

    from app import aws_storage

    _absent_archived_film(app, "Dropped Six Times", 2003)
    _sync_env(app, monkeypatch, {})
    checks = []

    def dead_after_the_idle_check(paths):
        checks.append(paths)
        return [] if len(checks) == 1 else ["/Volumes/Movies"]

    monkeypatch.setattr("app.videos._dead_volumes", dead_after_the_idle_check)
    with app.app_context(), pytest.raises(aws_storage.LibraryVolumeDropped):
        aws_storage.sync_aws_s3_storage_task(
            volume_drops=aws_storage.MAX_SYNC_VOLUME_DROPS - 1
        )


def test_a_remount_in_the_file_pass_fails_the_pass(app, monkeypatch):
    """A new device id of a library root abandons the result (#431)."""

    from app import aws_storage

    _absent_archived_film(app, "Remounted Pass", 2002)
    seen = []

    def devices():
        seen.append(1)
        return {"MOVIE_LIBRARY": 1 if len(seen) == 1 else 2}

    monkeypatch.setattr(aws_storage, "_library_devices", devices)
    monkeypatch.setattr(aws_storage, "aws_restore", lambda key, **kw: None)
    monkeypatch.setattr(
        aws_storage, "get_matching_s3_objects", lambda bucket, prefix="": []
    )
    monkeypatch.setitem(app.config, "AWS_BUCKET", "test-bucket")

    with app.app_context():
        listing = aws_storage._list_archives()
        with pytest.raises(aws_storage.LibraryVolumeDropped, match="mounted again"):
            aws_storage._reconcile_files(listing, None)
