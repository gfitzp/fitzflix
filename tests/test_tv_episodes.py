"""TVEpisode rows (#78): slot identity, uniqueness, and series cascade."""

import pytest
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import TVEpisode

from tests.factories import make_tv_episode, make_tv_series


def test_episode_round_trips_through_its_series(app):
    with app.app_context():
        series = make_tv_series("Doctor Who (1963)")
        make_tv_episode(
            series,
            1,
            1,
            title="An Unearthly Child",
            overview="Two teachers follow a strange pupil home.",
            runtime=25,
        )
        db.session.commit()

        stored = series.episodes.filter_by(season=1, episode=1).one()
        assert stored.title == "An Unearthly Child"
        assert stored.series is series


def test_slot_is_unique_per_series(app):
    with app.app_context():
        series = make_tv_series("Columbo")
        other = make_tv_series("The Rockford Files")
        make_tv_episode(series, 1, 1, title="Murder by the Book")

        # The same slot on a different series is fine
        make_tv_episode(other, 1, 1, title="The Kirkoff Case")
        db.session.commit()

        with pytest.raises(IntegrityError):
            make_tv_episode(series, 1, 1, title="Duplicate")
        db.session.rollback()


def test_episodes_cascade_with_their_series(app):
    with app.app_context():
        series = make_tv_series("K-9 and Company")
        make_tv_episode(series, 1, 1, title="A Girl's Best Friend")
        db.session.commit()

        db.session.delete(series)
        db.session.commit()
        assert TVEpisode.query.count() == 0
