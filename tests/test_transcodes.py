"""Test the derived files.

The tests cover the record of transcode outputs, the adoption sweep,
the linked delete (the rows cascade, and the physical purge enqueues),
and the structural exclusion from the ranking surfaces."""

import os

from tests.factories import make_movie, make_movie_file


def test_record_transcode_is_idempotent_by_path(app):
    """Make sure a second transcode updates the existing row and does not add a row."""

    from app import db
    from app.models import DerivedFile
    from app.transcodes import record_transcode

    with app.app_context():
        movie = make_movie("Derived Record", 1990)
        file = make_movie_file(movie, "Bluray-1080p")
        db.session.commit()

        output_dir = os.path.join(app.config["TRANSCODES_DIR"], file.dirname)
        os.makedirs(output_dir, exist_ok=True)
        output = os.path.join(output_dir, f"{file.plex_title}.mp4")
        with open(output, "wb") as handle:
            handle.write(b"transcoded bytes")

        record_transcode(file, output)
        db.session.commit()
        record_transcode(file, output)
        db.session.commit()

        rows = DerivedFile.query.all()
        assert len(rows) == 1
        assert rows[0].source_file_id == file.id
        assert rows[0].kind == "handbrake"
        assert rows[0].file_path == os.path.relpath(
            output, app.config["TRANSCODES_DIR"]
        )
        assert rows[0].filesize_bytes == len(b"transcoded bytes")


def test_finalize_transcoding_tracks_the_output(app):
    """Make sure the transcode finalizer tracks the output.

    The finalizer renames the hidden output into place AND records it
    as a derived file."""

    from app import db
    from app.models import DerivedFile, File
    from app.importing import finalize_transcoding

    with app.app_context():
        movie = make_movie("Derived Finalize", 1991)
        file = make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        file_id = file.id

        ext = app.config["HANDBRAKE_EXTENSION"]
        output_dir = os.path.join(app.config["TRANSCODES_DIR"], file.dirname)
        os.makedirs(output_dir, exist_ok=True)
        hidden = os.path.join(output_dir, f".{file.plex_title}.{ext}")
        with open(hidden, "wb") as handle:
            handle.write(b"handbrake output")

        lock = app.lock_manager.lock(file.file_identifier(), 60000)
        finalize_transcoding(file_id, lock)

        assert os.path.exists(os.path.join(output_dir, f"{file.plex_title}.{ext}"))
        row = DerivedFile.query.one()
        assert row.source_file_id == file_id
        # finalize commits in its own context. Refresh the view of this
        # session.
        db.session.expire_all()
        assert db.session.get(File, file_id).date_transcoded is not None


def test_linked_delete_cascades_rows_and_purges_files(app, admin_client):
    """Make sure a linked delete cascades the rows and purges the files.

    A delete of the original from the file page removes the derived rows
    (relationship cascade). It enqueues the physical purge on the
    file-operation queue. The removal task deletes the copy and removes
    the empty directory."""

    import re

    from app import db
    from app.models import DerivedFile
    from app.transcodes import record_transcode, remove_derived_paths

    with app.app_context():
        movie = make_movie("Derived Deleted", 1992)
        file = make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        file_id = file.id

        output_dir = os.path.join(app.config["TRANSCODES_DIR"], file.dirname)
        os.makedirs(output_dir, exist_ok=True)
        output = os.path.join(output_dir, f"{file.plex_title}.mp4")
        with open(output, "wb") as handle:
            handle.write(b"doomed copy")
        record_transcode(file, output)
        db.session.commit()

    page = admin_client.get(f"/file/{file_id}").get_data(as_text=True)
    assert "Copy:" in page
    token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page).group(1)
    response = admin_client.post(
        f"/file/{file_id}",
        data={"csrf_token": token, "delete_submit": "Delete File"},
    )
    assert response.status_code == 302

    with app.app_context():
        assert DerivedFile.query.count() == 0
        job_ids = app.file_queue.get_job_ids()
        purge_jobs = [
            app.file_queue.fetch_job(job_id)
            for job_id in job_ids
            if "remove_derived_paths"
            in (app.file_queue.fetch_job(job_id).func_name or "")
        ]
        assert len(purge_jobs) == 1
        assert purge_jobs[0].args[0] == [output]

    # The task removes the copy and removes the empty folder.

    remove_derived_paths([output])
    assert not os.path.exists(output)
    assert not os.path.exists(output_dir)


def test_adoption_sweep_matches_by_dirname_and_stem(app):
    """Make sure the adoption sweep matches by dirname and stem.

    The sweep adopts a copy under the dirname of its original with the
    plex_title as the stem. It counts the copies that are already
    tracked. It does not touch a stray file with no match."""

    import shutil

    from app import db
    from app.models import DerivedFile
    from app.transcodes import adopt_transcodes_task, record_transcode

    # The tmp transcode tree persists across tests. Start from a clean
    # tree.
    shutil.rmtree(app.config["TRANSCODES_DIR"], ignore_errors=True)
    os.makedirs(app.config["TRANSCODES_DIR"], exist_ok=True)

    with app.app_context():
        movie = make_movie("Derived Adopted", 1993)
        file = make_movie_file(movie, "Bluray-1080p")
        tracked_movie = make_movie("Derived Tracked", 1994)
        tracked = make_movie_file(tracked_movie, "Bluray-1080p")
        db.session.commit()

        root = app.config["TRANSCODES_DIR"]
        for source in (file, tracked):
            os.makedirs(os.path.join(root, source.dirname), exist_ok=True)
            with open(
                os.path.join(root, source.dirname, f"{source.plex_title}.mp4"),
                "wb",
            ) as handle:
                handle.write(b"x")
        record_transcode(
            tracked,
            os.path.join(root, tracked.dirname, f"{tracked.plex_title}.mp4"),
        )
        db.session.commit()

        os.makedirs(os.path.join(root, "Movies/Nobody Home (1999)"), exist_ok=True)
        with open(
            os.path.join(root, "Movies/Nobody Home (1999)/Nobody Home.mp4"), "wb"
        ) as handle:
            handle.write(b"x")

        summary = adopt_transcodes_task()
        assert summary == {"adopted": 1, "already": 1, "unmatched": 1}
        adopted = DerivedFile.query.filter_by(source_file_id=file.id).one()
        assert adopted.basename == f"{file.plex_title}.mp4"

        # A second run adopts nothing new.

        assert adopt_transcodes_task()["adopted"] == 0


def test_derived_rows_never_reach_ranking_surfaces(app, admin_client):
    """Make sure a derived row never reaches a ranking surface.

    This is the structural guarantee. A derived copy changes nothing on
    the library page or in the shopping tier. It is not a File row."""

    from app import db
    from app.transcodes import record_transcode

    with app.app_context():
        movie = make_movie("Derived Invisible", 1995)
        file = make_movie_file(movie, "DVD")
        db.session.commit()

        output_dir = os.path.join(app.config["TRANSCODES_DIR"], file.dirname)
        os.makedirs(output_dir, exist_ok=True)
        output = os.path.join(output_dir, f"{file.plex_title}.mp4")
        with open(output, "wb") as handle:
            handle.write(b"x")
        record_transcode(file, output)
        db.session.commit()

    page = admin_client.get("/library/movie").get_data(as_text=True)
    assert "Derived Invisible (1995)" in page
    assert page.count("Derived Invisible (1995)") == 1
    # The DVD tier of the original shows. There is never an
    # Unknown-quality phantom.
    assert "DVD" in page
