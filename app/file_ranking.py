"""The quality ranking of the library files (#361).

This module holds the one rule that selects the best copy of a film or
of an episode. It also holds the one rule that tells if that copy is
worth an upgrade. The reads of the quality tiers are here too. The ranked_files reporting view of MariaDB has the same rule. Its
definition is RANKED_FILES_VIEW below, and a test compiles the Python
rank and compares it with that definition.

app.models re-exports the public names of this module. Thus, the old
imports of the rank functions from app.models stay correct."""

from collections import namedtuple

from flask import g, has_app_context

from app import db
from app.models import File, Movie, RefQuality, TVSeries

# The order of the copies in a movie group, best first. Never select a
# fullscreen copy while a widescreen copy exists. Then select the best
# quality. At the same quality, a real rip ranks above a 1080p SDR
# conversion (#292). A tie goes to the lowest file id. Thus, the winner
# is always the same.

MOVIE_BEST_ORDER = (
    File.fullscreen.asc(),
    RefQuality.preference.desc(),
    File.converted_from_quality.isnot(None).asc(),
    File.id.asc(),
)

# The order of the copies of an episode, best first. This is the movie
# order with the span of the file after the quality. At the same
# quality, a file that holds more episodes ranks first.

TV_BEST_ORDER = (
    File.fullscreen.asc(),
    RefQuality.preference.desc(),
    File.last_episode.desc(),
    File.converted_from_quality.isnot(None).asc(),
    File.id.asc(),
)

# The ranked_files reporting view (#273). The window clauses are the
# order of MOVIE_BEST_ORDER and TV_BEST_ORDER. Migration d4e2a9c71b38
# created this definition. A change to a rank needs a new migration
# that runs this string.

RANKED_FILES_VIEW = """
    CREATE OR REPLACE VIEW ranked_files AS
    SELECT file.id AS id,
           file.untouched_basename AS untouched_basename,
           ROW_NUMBER() OVER (
               PARTITION BY movie.id, file.feature_type_id,
                            file.plex_title, file.edition
               ORDER BY file.fullscreen ASC, q.preference DESC,
                        file.converted_from_quality IS NOT NULL ASC,
                        file.id ASC
           ) AS `rank`
      FROM file
      JOIN movie ON movie.id = file.movie_id
      JOIN ref_quality q ON q.id = file.quality_id
    UNION ALL
    SELECT file.id AS id,
           file.untouched_basename AS untouched_basename,
           ROW_NUMBER() OVER (
               PARTITION BY tv_series.id, file.season, file.episode
               ORDER BY file.fullscreen ASC, q.preference DESC,
                        file.last_episode DESC,
                        file.converted_from_quality IS NOT NULL ASC,
                        file.id ASC
           ) AS `rank`
      FROM file
      JOIN tv_series ON tv_series.id = file.series_id
      JOIN ref_quality q ON q.id = file.quality_id
"""


def movie_file_rank():
    """Rank each movie file in its title/feature/edition group by quality.

    The order is MOVIE_BEST_ORDER. The ranked_files reporting view uses
    the same rule (#273)."""

    return (
        db.func.row_number()
        .over(
            partition_by=(
                Movie.id,
                File.feature_type_id,
                File.plex_title,
                File.edition,
            ),
            order_by=MOVIE_BEST_ORDER,
        )
        .label("rank")
    )


def tv_file_rank():
    """Rank each episode file in its series/season/episode group.

    The edition is not part of the group. That is intentional. For a TV
    file, it only carries the optional episode title. Thus, 2 copies of
    the same episode compete, with or without a title in their
    filenames. The order is TV_BEST_ORDER. The ranked_files reporting
    view uses the same rule (#273).
    """

    return (
        db.func.row_number()
        .over(
            partition_by=(TVSeries.id, File.season, File.episode),
            order_by=TV_BEST_ORDER,
        )
        .label("rank")
    )


def ranked_movie_files():
    """Return a subquery of (id, rank) with the movie_file_rank of each movie file.

    rank 1 is the best copy of its title, feature, and edition group."""

    return (
        db.session.query(File.id, movie_file_rank())
        .join(Movie, (Movie.id == File.movie_id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .subquery()
    )


def ranked_tv_files():
    """Return a subquery of (id, rank) with the tv_file_rank of each episode file.

    rank 1 is the best copy of its series, season, and episode."""

    return (
        db.session.query(File.id, tv_file_rank())
        .join(TVSeries, (TVSeries.id == File.series_id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .subquery()
    )


def best_copy_key(file, quality):
    """Return the Python sort key of MOVIE_BEST_ORDER for a (File, RefQuality) pair.

    The smallest key is the best copy. A pair with no file or no quality
    sorts after each real copy. The filmography uses this key to keep 1
    copy of a film that has several rank-1 editions."""

    if file is None or quality is None:
        return (1, False, 0, False, 0)
    return (
        0,
        bool(file.fullscreen),
        -(quality.preference or 0),
        file.converted_from_quality is not None,
        file.id,
    )


def copy_upgradable(
    fullscreen, preference, threshold, criterion_pref=None, physical=False
):
    """Return True if a best copy is worth an upgrade.

    This is the one rule of the amber badge. A copy on physical media
    that only has a physical release (DVD, SD or 720p Blu-ray TV) is
    final. Thus, it is never upgradable. A fullscreen copy is always
    upgradable. Otherwise the copy is upgradable below the target. The
    target is the app-wide threshold. With the quality of a Criterion
    release, the target is that quality, capped at the threshold. The
    caller applies the exclusion of the shopping list."""

    if physical:
        return False
    target = min(criterion_pref or threshold, threshold)
    return bool(fullscreen) or (preference or 0) < target


# 1 quality tier, as plain values. A plain tuple stays valid after the
# session closes.

QualityTier = namedtuple("QualityTier", "id preference physical_media")


def quality_anchors():
    """Return a dict of quality title -> QualityTier for each tier.

    The quality tiers are reference data. The dict is kept in flask.g.
    Thus, a request reads the table 1 time."""

    if has_app_context() and "quality_anchors" in g:
        return g.quality_anchors
    anchors = {
        title: QualityTier(id, preference, bool(physical))
        for id, title, preference, physical in db.session.query(
            RefQuality.id,
            RefQuality.quality_title,
            RefQuality.preference,
            RefQuality.physical_media,
        )
    }
    if has_app_context():
        g.quality_anchors = anchors
    return anchors


def quality_preference(title):
    """Return the preference of the quality tier with this title, or None."""

    quality = quality_anchors().get(title)
    return quality.preference if quality is not None else None


def quality_id(title):
    """Return the id of the quality tier with this title, or None."""

    quality = quality_anchors().get(title)
    return quality.id if quality is not None else None


def lowest_physical_preference(prefix):
    """Return the lowest preference of a physical tier whose title starts with prefix.

    The result is None if no such tier exists."""

    preferences = [
        quality.preference
        for title, quality in quality_anchors().items()
        if title.startswith(prefix) and quality.physical_media
    ]
    return min(preferences) if preferences else None


def upgrade_threshold():
    """Return the quality preference below which a copy is upgradable.

    The threshold is the Bluray-1080p tier."""

    return quality_preference("Bluray-1080p") or 0
