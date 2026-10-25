"""Pin the paths of the 2 phases of a TMDB refresh (app/tmdb_refresh.py).

refresh_tmdb_info fetches. apply_tmdb_refresh applies the payload on
the sql queue. Both phases find the record of the refresh the same way."""

import json
import zlib

from app import db
from tests.factories import make_movie, make_movie_file, make_tv_series


def _payload(details):
    return zlib.compress(json.dumps(details).encode("utf-8"))


def test_both_phases_decline_a_record_that_is_gone(app):
    from app.tmdb_refresh import apply_tmdb_refresh, refresh_tmdb_info

    for library in ("Movies", "TV Shows"):
        assert refresh_tmdb_info(library, 999999) is False
        assert apply_tmdb_refresh(library, 999999, tmdb_payload=_payload({})) is False
    assert refresh_tmdb_info("Music", 1) is False


def test_a_series_refresh_works_on_the_record_that_owns_the_tmdb_id(app, monkeypatch):
    """A series that shares its TMDB id with an older record refreshes that record."""

    from app.tmdb_refresh import apply_tmdb_refresh, refresh_tmdb_info

    searched = []

    class Answer:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    def fake_get(url, params=None, **kwargs):
        searched.append(params.get("query"))
        if "/search/tv" in url:
            return Answer({"results": [{"id": 555}]})
        return Answer({"id": 555, "name": "Canonical Show"})

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "key")
    monkeypatch.setattr("app.tmdb_apply.tmdb_get", fake_get)
    with app.app_context():
        canonical = make_tv_series("Canonical Show", tmdb_id=555)
        duplicate = make_tv_series("Duplicate Show", tmdb_id=555)
        db.session.commit()
        canonical_id, duplicate_id = canonical.id, duplicate.id

    assert refresh_tmdb_info("TV Shows", duplicate_id) is True
    assert searched[0] == "Canonical Show"
    jobs = [
        job
        for job in app.sql_queue.jobs
        if job.func_name == "app.videos.apply_tmdb_refresh"
    ]
    assert [job.description for job in jobs] == [
        "Updating 'Canonical Show' with TMDB data"
    ]

    payload = _payload({"id": 555, "name": "Canonical Show (TMDB)"})
    assert apply_tmdb_refresh("TV Shows", duplicate_id, tmdb_payload=payload) is True
    with app.app_context():
        from app.models import TVSeries

        assert db.session.get(TVSeries, canonical_id).tmdb_name == (
            "Canonical Show (TMDB)"
        )
        assert db.session.get(TVSeries, duplicate_id).tmdb_name is None


def test_a_merge_moves_the_files_and_every_review(app):
    """The apply merges a duplicate into the record that owns the TMDB id."""

    from app.models import File, Movie, User, UserMovieReview
    from app.tmdb_refresh import apply_tmdb_refresh

    with app.app_context():
        user = User.query.first()
        source = make_movie("Merge Source", 2001)
        file = make_movie_file(source, "DVD")
        file.untouched_basename = file.basename
        target = make_movie("Merge Target", 2001, tmdb_id=4243)
        for text in ("first", "second"):
            db.session.add(
                UserMovieReview(user_id=user.id, movie_id=source.id, review=text)
            )
        db.session.commit()
        source_id, target_id, file_id = source.id, target.id, file.id

    payload = _payload(
        {"id": 4243, "title": "Merge Target", "release_date": "2001-05-01"}
    )
    assert (
        apply_tmdb_refresh("Movies", source_id, tmdb_id=4243, tmdb_payload=payload)
        is True
    )
    with app.app_context():
        assert db.session.get(Movie, source_id) is None
        assert db.session.get(File, file_id).movie_id == target_id
        assert UserMovieReview.query.filter_by(movie_id=target_id).count() == 2


def test_a_rename_onto_a_claimed_path_is_refused(app, monkeypatch):
    """A sibling row on the new path stops the rename and emails the admin."""

    import app.tmdb_refresh as tmdb_refresh
    from app.tmdb_refresh import apply_tmdb_refresh

    sent = []
    monkeypatch.setattr(
        tmdb_refresh, "notify_admin_text", lambda subject, *a: sent.append(subject)
    )
    with app.app_context():
        movie = make_movie("Old Name", 1999)
        file = make_movie_file(movie, "DVD")
        file.untouched_basename = file.basename
        other = make_movie("Claimant", 1998)
        sibling = make_movie_file(other, "DVD", plex_title="New Name (1999)")
        sibling.file_path = "Movies/New Name (1999)/New Name (1999) - [DVD].mkv"
        db.session.commit()
        movie_id, file_id = movie.id, file.id
        old_path = file.file_path

    payload = _payload({"id": 777, "title": "New Name", "release_date": "1999-02-02"})
    assert apply_tmdb_refresh("Movies", movie_id, tmdb_payload=payload) is True
    with app.app_context():
        from app.models import File

        assert db.session.get(File, file_id).file_path == old_path
    assert sent == ["Rename collision needs triage"]
