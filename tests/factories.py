"""Row factories for tests.

The date columns are passed explicitly because the models' server-side
default (utc_timestamp()) only exists on MySQL/MariaDB, not SQLite.
"""

from datetime import datetime

from app import db
from app.models import File, Movie, RefFeatureType, RefQuality, TVSeries


def quality(quality_title):
    return RefQuality.query.filter_by(quality_title=quality_title).one()


def feature_type(name):
    return RefFeatureType.query.filter_by(feature_type=name).one()


def make_movie(title, year, **kwargs):
    movie = Movie(title=title, year=year, date_created=datetime.utcnow(), **kwargs)
    db.session.add(movie)
    db.session.flush()
    return movie


def make_tv_series(title, **kwargs):
    series = TVSeries(title=title, date_created=datetime.utcnow(), **kwargs)
    db.session.add(series)
    db.session.flush()
    return series


def make_file(basename, dirname, plex_title, media_library, quality_title, **kwargs):
    file = File(
        basename=basename,
        dirname=dirname,
        file_path=f"{dirname}/{basename}",
        plex_title=plex_title,
        media_library=media_library,
        quality_id=quality(quality_title).id,
        date_added=datetime.utcnow(),
        **kwargs,
    )
    db.session.add(file)
    db.session.flush()
    return file


def make_movie_file(movie, quality_title, feature_type_name=None, **kwargs):
    plex_title = kwargs.pop("plex_title", f"{movie.title} ({movie.year})")
    dirname = f"Movies/{movie.title} ({movie.year})"
    if feature_type_name:
        dirname = f"{dirname}/{feature_type_name}"
        basename = f"{plex_title}.mkv"
    else:
        basename = f"{plex_title} - [{quality_title}].mkv"
    return make_file(
        basename,
        dirname,
        plex_title,
        "Movies",
        quality_title,
        movie_id=movie.id,
        feature_type_id=(
            feature_type(feature_type_name).id if feature_type_name else None
        ),
        **kwargs,
    )


def make_tv_file(series, season, episode, quality_title, **kwargs):
    last_episode = kwargs.pop("last_episode", episode)
    episode_tag = f"S{season:02d}E{episode:02d}"
    if last_episode != episode:
        episode_tag = f"{episode_tag}-E{last_episode:02d}"
    plex_title = f"{series.title} - {episode_tag}"
    return make_file(
        f"{plex_title} - [{quality_title}].mkv",
        f"TV Shows/{series.title}/Season {season:02d}",
        plex_title,
        "TV Shows",
        quality_title,
        series_id=series.id,
        season=season,
        episode=episode,
        last_episode=last_episode,
        **kwargs,
    )
