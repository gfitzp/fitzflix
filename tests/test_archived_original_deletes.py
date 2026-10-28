"""Test that a series delete and a catalog exclude remove their archived originals (#464).

The foreign keys are ON DELETE SET NULL. The rows stayed with no owner,
kept claiming their S3 keys, and the objects were never pruned."""

from app import db
from tests.conftest import csrf_token_from
from tests.factories import make_movie, make_tv_file, make_tv_series


def _original(**owner):
    from app.models import ArchivedOriginal

    name = f"Orphan Original {sorted(owner.items())} - [Bluray-2160p].mkv"
    original = ArchivedOriginal(
        media_library="TV Shows" if "series_id" in owner else "Movies",
        quality_title="Bluray-2160p",
        untouched_basename=name,
        aws_key=f"untouched/{name}",
        reason="archive only",
        **owner,
    )
    db.session.add(original)
    db.session.flush()
    return original.aws_key


def _deletes(app):
    return [
        job.args[0]
        for queue in (app.request_queue, app.handoff_queue, app.file_queue)
        for job in queue.jobs
        if job.func_name == "app.videos.aws_delete"
    ]


def test_a_series_delete_removes_its_archived_originals(app, admin_client):
    from app.models import ArchivedOriginal

    with app.app_context():
        series = make_tv_series("Original Delete Show", tmdb_id=464001)
        make_tv_file(series, 1, 1, "Bluray-1080p")
        db.session.flush()
        key = _original(series_id=series.id, season=1, episode=1)
        db.session.commit()
        series_id = series.id

    token = csrf_token_from(admin_client.get(f"/tv/{series_id}").get_data(as_text=True))
    response = admin_client.post(
        f"/tv/{series_id}/delete",
        data={"csrf_token": token, "delete_submit": "Delete"},
    )
    assert response.status_code == 302
    with app.app_context():
        assert ArchivedOriginal.query.filter_by(aws_key=key).count() == 0
    assert key in _deletes(app)


def test_a_catalog_exclude_removes_the_archived_originals(app):
    from app import cli as app_cli
    from app.models import ArchivedOriginal

    if "catalog" not in app.cli.commands:
        app_cli.register(app)
    with app.app_context():
        movie = make_movie("Original Exclude Film", 1999, tmdb_id=464002)
        key = _original(movie_id=movie.id)
        db.session.commit()
        movie_id = movie.id

    result = app.test_cli_runner().invoke(args=["catalog", "exclude", str(movie_id)])
    assert result.exit_code == 0, result.output
    with app.app_context():
        assert ArchivedOriginal.query.filter_by(aws_key=key).count() == 0
    assert key in _deletes(app)


def test_a_key_that_a_file_still_claims_is_not_deleted(app):
    """The claimed-key check runs after the commit, as for the file keys."""

    from app import cli as app_cli
    from tests.factories import make_movie_file

    if "catalog" not in app.cli.commands:
        app_cli.register(app)
    with app.app_context():
        movie = make_movie("Shared Key Film", 1998, tmdb_id=464003)
        key = _original(movie_id=movie.id)
        other = make_movie_file(make_movie("Claimant Film", 1997), "DVD")
        other.aws_key = key
        db.session.commit()
        movie_id = movie.id

    result = app.test_cli_runner().invoke(args=["catalog", "exclude", str(movie_id)])
    assert result.exit_code == 0, result.output
    assert key not in _deletes(app)
