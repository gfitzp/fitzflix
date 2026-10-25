"""Test the title comparison keys of each caller.

The strict key of the Criterion TMDB match removes the punctuation. The
loose key of the frame game, the Criterion schedule and the MySQL
collation folds keeps it. Diary keeps its own key (diary._normalize_title).
"""

from datetime import datetime, timedelta, timezone

CORPUS = [
    "The Searchers",
    "Muriel’s Wedding",
    "P. J. Hogan",
    "Amélie",
    "  Spider's   Eyes ",
    "Seven Samurai",
    "2001: A Space Odyssey",
    "Æon Flux",
    "Straße",
    "",
    None,
]


def test_the_strict_key_of_the_criterion_match(app):
    from app.title_keys import match_key

    assert [match_key(title) for title in CORPUS] == [
        "the searchers",
        "muriels wedding",
        "p j hogan",
        "amelie",
        "spiders eyes",
        "seven samurai",
        "2001 a space odyssey",
        "on flux",
        "strae",
        "",
        "",
    ]


def test_the_fuzzy_key_of_the_frame_game(app):
    from app.main.game import _normalize

    assert [_normalize(title) for title in CORPUS] == [
        "searchers",
        "muriel s wedding",
        "p j hogan",
        "amelie",
        "spider s eyes",
        "7 samurai",
        "2001 a space odyssey",
        "aeon flux",
        "strasse",
        "",
        "",
    ]


def test_the_enrichment_key_of_a_film_with_no_link(app):
    from app.criterion_now import _enrichment_key

    assert _enrichment_key("The Searchers", None) == "title:the searchers"
    assert _enrichment_key("Seven Samurai", "/films/seven-samurai") == (
        "id:seven-samurai"
    )


def test_the_loose_key_folds_the_accents_the_case_and_the_ends(app):
    from app.title_keys import title_key

    assert [title_key(title) for title in CORPUS] == [
        "the searchers",
        "muriel's wedding",
        "p. j. hogan",
        "amelie",
        "spider's   eyes",
        "seven samurai",
        "2001: a space odyssey",
        "aeon flux",
        "strasse",
        "",
        "",
    ]


def test_the_schedule_folds_the_accents_of_a_title(app):
    """The Criterion schedule compares titles with the loose key.

    Before, it compared only the case. Now "Amélie" in the heading also
    finds an "Amelie" entry."""

    from app.criterion_now import _current_slot, _enrichment_key

    now = datetime(2026, 10, 25, 20, tzinfo=timezone.utc)
    entry = (now - timedelta(minutes=5), now + timedelta(hours=1), "Amelie", None)
    assert _current_slot([entry], "Amélie ", now) == (entry, None)
    assert _enrichment_key("Amélie", None) == _enrichment_key("amelie", "")


def test_the_current_slot_ignores_the_case_of_the_title(app):
    from app.criterion_now import _current_slot

    now = datetime(2026, 10, 25, 20, tzinfo=timezone.utc)
    entry = (now - timedelta(minutes=5), now + timedelta(hours=1), "Jaws", None)
    assert _current_slot([entry], "JAWS", now) == (entry, None)
    assert _current_slot([entry], "Jaws 2", now) == (None, None)


def test_the_certification_sync_folds_the_case_of_a_stored_value(app, monkeypatch):
    """A value that differs only in case updates the stored row."""

    from app import db
    from app.models import RefTMDBCertification
    from tests.fakes import Response

    import app.tmdb_refresh as tmdb_refresh

    listing = {
        "certifications": {"FR": [{"certification": "TP", "meaning": "", "order": 1}]}
    }
    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(
        "app.tmdb_apply.tmdb_get", lambda url, **kwargs: Response(listing)
    )
    with app.app_context():
        db.session.add(RefTMDBCertification(country="FR", certification="tp"))
        db.session.commit()
        assert tmdb_refresh.sync_certification_list() is True
        rows = [(c.country, c.order) for c in RefTMDBCertification.query.all()]
        assert rows == [("FR", 1)]
