"""Test that a collation match gives the title of the series record (#480).

MariaDB compares titles with utf8mb4_general_ci: no case, no accents,
and no trailing spaces. SQLite compares them exactly. Thus, these tests
give TVSeries a query that compares titles as MariaDB does."""

import pytest

from app import db
from tests.factories import make_tv_series


class _CollatingQuery:
    """A TVSeries query whose filter_by(title=...) compares as MariaDB does."""

    def __init__(self, model):
        self.model = model
        self.real = db.session.query(model)

    def filter_by(self, **criteria):
        if set(criteria) != {"title"}:
            return self.real.filter_by(**criteria)
        wanted = criteria["title"].rstrip().lower()
        ids = [
            series.id
            for series in self.real.all()
            if series.title.rstrip().lower() == wanted
        ]
        return self.real.filter(self.model.id.in_(ids or [0]))

    def __getattr__(self, name):
        return getattr(self.real, name)


class _CollatingProperty:
    def __get__(self, instance, owner):
        return _CollatingQuery(owner)


@pytest.fixture
def mariadb_collation():
    """Give TVSeries the collating query. Restore the real descriptor after.

    monkeypatch reads the old value through the descriptor. That needs an
    app context, and it would restore a stale query object."""

    from app.models import TVSeries

    own = TVSeries.__dict__.get("query")
    TVSeries.query = _CollatingProperty()
    try:
        yield
    finally:
        if own is None:
            del TVSeries.query
        else:
            TVSeries.query = own


def test_a_case_or_space_match_returns_the_record_title(app, mariadb_collation):
    from app.importing import _canonical_series_title

    with app.app_context():
        make_tv_series("Monk")
        make_tv_series("The Colgate Comedy Hour ")
        db.session.commit()
        try:
            assert _canonical_series_title("MONK") == "Monk"
            assert _canonical_series_title("Monk") == "Monk"
            assert (
                _canonical_series_title("The Colgate Comedy Hour")
                == "The Colgate Comedy Hour "
            )
            assert _canonical_series_title("Unknown Show") == "Unknown Show"
        finally:
            db.session.rollback()


def test_the_parse_uses_the_record_title_for_names_and_lock_key(app, mariadb_collation):
    from app.importing import evaluate_filename, lock_key_for_details

    with app.app_context():
        series = make_tv_series("Monk")
        db.session.commit()
        try:
            details = evaluate_filename("MONK - S01E01 - [DVD].mkv", log=False)
            assert details["title"] == "Monk"
            assert details["dirname"].startswith("TV Shows/Monk/")
            assert details["basename"].startswith("Monk - S01E01")
            assert lock_key_for_details(details) == lock_key_for_details(
                dict(details, title=series.title)
            )
        finally:
            db.session.rollback()


def test_a_reversed_span_is_rejected(app):
    """A span that ends before it starts is a bad name (#482)."""

    from app.importing import FilenameRejection, evaluate_filename

    with app.app_context():
        details = evaluate_filename("Monk - S01E05-E03 - [DVD].mkv", log=False)
        assert isinstance(details, FilenameRejection)
        assert details.detail == "reversed episode span"
        assert evaluate_filename("Monk - S01E03-E05 - [DVD].mkv", log=False)
