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

    calls = {"deletes": [], "mails": [], "uncataloged": []}

    def listing(bucket, prefix=""):
        if fail_listing:
            raise ConnectionError("S3 is away")
        return objects.get(prefix, [])

    def delete(key):
        if key == fail_delete:
            raise ConnectionError("S3 delete failed")
        calls["deletes"].append(key)

    class FakeClient:
        def put_object(self, **kwargs):
            pass

    monkeypatch.setattr(aws_storage, "get_matching_s3_objects", listing)
    monkeypatch.setattr(aws_storage, "aws_delete", delete)
    monkeypatch.setattr(aws_storage, "aws_restore", lambda key, **kw: None)
    monkeypatch.setattr(
        aws_storage, "send_email", lambda subject, **kw: calls["mails"].append(subject)
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
