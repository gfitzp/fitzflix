"""Test abandon_import, the 1 cleanup of a failed import stage.

Before it, 4 except blocks each removed their work files, rejected the
source and released the lock in their own way. The localization and
the conversion released the staging reservation. The move and the
cataloging did not. Thus, the reservation of a failed move held its
space until the grace period ran out.
"""

import os


def _reserved(app, basename):
    from app.importing import STAGING_RESERVED_KEY

    return app.redis.hexists(STAGING_RESERVED_KEY, basename)


def test_a_failed_move_releases_the_staging_reservation(app, incoming_dir, monkeypatch):
    """A move that fails rejects the source and frees its staging space."""

    from app import importing, videos
    from tests.test_localization import rejected_files, space_move_fixture

    basename = "Failed Move Space (2021) - [DVD].mkv"
    source, hidden, details = space_move_fixture(app, incoming_dir, basename)

    def broken(*args, **kwargs):
        raise RuntimeError("simulated inspection failure")

    monkeypatch.setattr(videos, "inspect_localized_file", broken)
    monkeypatch.setattr(importing, "inspect_localized_file", broken)
    with app.app_context():
        importing.set_staging_reservation(basename, 1000, [basename])
        assert _reserved(app, basename)
        videos.move_localized_file(source, details, None, hidden)

        assert not _reserved(app, basename)
        assert not os.path.exists(hidden)
        assert not os.path.exists(source)
        assert basename in rejected_files(app)
    os.remove(os.path.join(app.config["REJECTS_DIR"], "exception", basename))


def test_a_failed_reject_still_releases_the_lock(app, incoming_dir, monkeypatch):
    """A failure of 1 step must not stop the next step.

    A lock that stays held blocks the title for the whole lock TTL."""

    from app import importing

    source = os.path.join(incoming_dir, "Reject Fails (2021) - [DVD].mkv")
    stray = os.path.join(app.config["STAGING_DIR"], "Reject Fails stray.mkv")
    with open(stray, "wb") as f:
        f.write(b"work file")

    def refuse(path, reason):
        raise OSError("rejects volume away")

    monkeypatch.setattr(importing, "move_to_rejects", refuse)
    with app.app_context():
        lock = app.lock_manager.lock("abandon-import-test", 60000)
        assert lock
        importing.set_staging_reservation(os.path.basename(source), 10, ["x"])
        importing.abandon_import(source, lock, strays=(stray, None))

        assert not os.path.exists(stray)
        assert not _reserved(app, os.path.basename(source))
        assert app.redis.get("abandon-import-test") is None


def test_keep_source_leaves_the_source_for_the_next_sweep(app, incoming_dir):
    """A failed handoff keeps the source in the import directory (#340)."""

    from app import importing

    source = os.path.join(incoming_dir, "Kept Source (2021) - [DVD].mkv")
    with open(source, "wb") as f:
        f.write(b"source")
    try:
        with app.app_context():
            importing.abandon_import(source, None, keep_source=True)
        assert os.path.exists(source)
    finally:
        os.remove(source)
