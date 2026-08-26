"""TMDB apply methods: payload fields must land on mapped columns.

Regression home for the tvdb_id persist bug where tmdb_tv_apply wrote the
TheTVDB cross-reference to an unmapped attribute and it never persisted.
"""

from app import db
from app.models import TVSeries

from tests.factories import make_tv_series


def test_tv_apply_persists_external_ids(app):
    with app.app_context():
        series = make_tv_series("Doctor Who")
        series.tmdb_tv_apply(
            {
                "id": 57243,
                "name": "Doctor Who",
                "external_ids": {"imdb_id": "tt0436992", "tvdb_id": 78804},
            }
        )
        db.session.commit()
        db.session.expire_all()

        stored = db.session.get(TVSeries, series.id)
        assert stored.tvdb_id == 78804
        assert stored.imdb_id == "tt0436992"
        assert stored.tmdb_id == 57243
