"""The quality ranking of the library files (#361).

This module holds the one rule that selects the best copy of a film or
of an episode. It also holds the one rule that tells if that copy is
worth an upgrade. The reads of the quality tiers are here too. The ranked_files reporting view of MariaDB has the same rule. Its
definition is RANKED_FILES_VIEW below, and a test compiles the Python
rank and compares it with that definition.

app.models re-exports the public names of this module. Thus, the old
imports of the rank functions from app.models stay correct."""

from collections import namedtuple

from flask import current_app, g, has_app_context
from sqlalchemy.orm import joinedload

from app import db
from app.models import MOVIES, TV_SHOWS, File, Movie, RefQuality, TVSeries

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


def criterion_settled(file, quality, criterion_pref, threshold, disc_owned):
    """Return True if a library copy of a Criterion film needs nothing more (#77a).

    The disc must be owned AND the copy must satisfy the format of the
    release, capped at the threshold. Decided by Glenn: an owned disc
    with a Bluray-1080p file is settled, even if Criterion released the
    film again in 2160p. That upgrade is the job of the shopping list.
    The threshold also covers a release whose quality was never
    recorded."""

    return bool(disc_owned) and not copy_upgradable(
        file.fullscreen, quality.preference, threshold, criterion_pref
    )


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


# The import rules: the library files that outrank an incoming file, and
# the files that an import replaces. They were methods of File.


def find_better_files(details):
    # A message to the future Glenn (written by Glenn, 2020-08-02):
    #
    # This place is a message, and part of a system of messages. Give
    # it your attention.
    #
    # Glenn wrote much of the logic of this method without sleep. Thus,
    # it is not certain that it works 100% correctly. But be careful
    # with improvements. It is easy to break this method or to cause
    # unintended effects. This is especially true for special features,
    # different versions (for example, the Director's Cut), and
    # multi-episode TV show files.
    #
    # The method is also difficult because the logic is the reverse of
    # what you expect. It looks for the files that are better than the
    # file that Fitzflix imports. Thus, the import continues only if
    # the method finds NO matches.
    #
    # The intended rules are:
    #
    # Movies:
    #
    # - If the existing file is a full screen version, always replace
    #   it with a non-full screen version, even if the quality is lower.
    #
    # - If the existing file is a non-full screen version, and the new
    #   file is full screen, always reject the new full screen file.
    #
    # - If the existing and the new files are both non-full screen
    #   versions, compare the quality. Replace the existing file only
    #   if the new file has the same or better quality.
    #
    # TV Shows:
    #
    # - If the existing file is a full screen version, always replace
    #   it with a non-full screen version. Do this even if the quality
    #   is lower, or if a multi-episode file loses episodes.
    #
    # - If the existing file is a non-full screen version, and the new
    #   file is full screen, always reject the new full screen file.
    #
    # - If the existing and the new files are both non-full screen
    #   versions, compare the quality and the episode count. Replace
    #   the existing file only if the new file has the same or better
    #   quality. The new file must also contain the same number of
    #   episodes or more.
    #
    # Never import full screen versions of movies or TV shows. Then you
    # should be safe. You do not want full screen versions anyway.
    #
    # In short: this place is not a place of honor.

    """Return the library files that outrank the parsed details of an import.

    details is the dict of evaluate_filename. The import goes on only if
    this list is empty."""

    better_files = []

    source_quality = RefQuality.query.filter_by(
        quality_title=details["quality_title"]
    ).first()

    current_app.logger.debug(f"Import vars: {details}")

    if details["media_library"] == MOVIES:
        # If the new file is a full screen version, these better files
        # prevent the import of this file:
        # - the exact same movie, also full screen, in a better quality
        # - the exact same movie, NOT full screen, in the same or better
        #   quality

        if details.get("fullscreen") == True:
            better_files = (
                File.query.join(Movie, (Movie.id == File.movie_id))
                .join(RefQuality, (RefQuality.id == File.quality_id))
                .filter(
                    File.media_library == details["media_library"],
                    File.dirname == details["dirname"],
                    Movie.title == details["title"],
                    Movie.year == details["year"],
                    db.or_(
                        db.and_(
                            File.plex_title == details["plex_title"],
                            File.edition == details.get("edition"),
                            File.fullscreen == True,
                            RefQuality.preference > source_quality.preference,
                        ).self_group(),
                        # The parser gives a full screen file the plex
                        # title and the edition of its widescreen copy.
                        # Only the flag differs. Any widescreen copy
                        # blocks it.
                        db.and_(
                            File.plex_title == details["plex_title"],
                            File.edition == details.get("edition"),
                            File.fullscreen == False,
                        ).self_group(),
                    ),
                )
                .all()
            )

        # Otherwise, if the file is not full screen, these better files
        # prevent the import of this file:
        # - the exact same movie, not full screen, in a better quality

        else:
            better_files = (
                File.query.join(Movie, (Movie.id == File.movie_id))
                .join(RefQuality, (RefQuality.id == File.quality_id))
                .filter(
                    File.media_library == details["media_library"],
                    File.dirname == details["dirname"],
                    Movie.title == details["title"],
                    Movie.year == details["year"],
                    File.plex_title == details["plex_title"],
                    File.edition == details.get("edition"),
                    File.fullscreen == False,
                    RefQuality.preference > source_quality.preference,
                )
                .all()
            )

    elif details["media_library"] == TV_SHOWS:
        # The identity of a TV episode is series + season + episode
        # span. It is NEVER the edition. For a TV file, the edition
        # holds the episode-title segment of the filename. Two releases
        # of the same episode can give it different titles (the Seeds
        # of Doom case from Glenn: a Blu-ray special did not replace its
        # DVD predecessor because the discs gave the extra different
        # names).
        #
        # If the new file is a full screen version, these existing
        # files prevent the import of this file:
        # - same TV episode range, also full screen, in the same quality
        # - wider TV episode range, also full screen, in the same or
        #   better quality
        # - same TV episode range, NOT full screen, in the same quality
        # - wider TV episode range, NOT full screen, in the same or
        #   better quality

        if details.get("fullscreen") == True:
            better_files = (
                File.query.join(TVSeries, (TVSeries.id == File.series_id))
                .join(RefQuality, (RefQuality.id == File.quality_id))
                .filter(
                    File.media_library == details["media_library"],
                    File.dirname == details["dirname"],
                    TVSeries.title == details["title"],
                    File.season == details["season"],
                    File.episode == details["episode"],
                    db.or_(
                        db.and_(
                            File.last_episode == details.get("last_episode"),
                            File.fullscreen == True,
                            RefQuality.preference == source_quality.preference,
                        ).self_group(),
                        db.and_(
                            File.last_episode > details.get("last_episode"),
                            File.fullscreen == True,
                            RefQuality.preference >= source_quality.preference,
                        ).self_group(),
                        db.and_(
                            File.last_episode == details.get("last_episode"),
                            File.fullscreen == False,
                            RefQuality.preference == source_quality.preference,
                        ).self_group(),
                        db.and_(
                            File.last_episode > details.get("last_episode"),
                            File.fullscreen == False,
                            RefQuality.preference >= source_quality.preference,
                        ).self_group(),
                    ).self_group(),
                )
                .all()
            )

        # Otherwise, if the file is not full screen, these better files
        # prevent the import of this file:
        # - same TV show episode range, not full screen, in a better
        #   quality
        # - wider TV show episode range, not full screen, in the same
        #   quality

        else:
            better_files = (
                File.query.join(TVSeries, (TVSeries.id == File.series_id))
                .join(RefQuality, (RefQuality.id == File.quality_id))
                .filter(
                    File.media_library == details["media_library"],
                    File.dirname == details["dirname"],
                    TVSeries.title == details["title"],
                    File.season == details["season"],
                    File.episode == details["episode"],
                    db.or_(
                        db.and_(
                            File.last_episode == details.get("last_episode"),
                            RefQuality.preference > source_quality.preference,
                        ).self_group(),
                        db.and_(
                            File.last_episode > details.get("last_episode"),
                            RefQuality.preference == source_quality.preference,
                        ).self_group(),
                    ).self_group(),
                    File.fullscreen == False,
                )
                .all()
            )

    # A conversion to 1080p SDR (#292) has 2 more rules. An admin who
    # chose "Import" for a held conversion replaces the existing file,
    # whatever its quality. A file with HDR or BT.2020 colors never
    # blocks a conversion. The conversion replaces it.

    if details.get("converted_from_quality"):
        if details.get("replace_existing"):
            return []
        better_files = [f for f in better_files if not f.needs_sdr_conversion]

    return better_files


def same_identity_files(file):
    """Return the other files of this title, edition, or episode span, in any quality.

    A TV file includes the files with a narrower episode span. These
    are the files that find_worse_files can remove, before its
    quality rule."""

    query = (
        File.query.join(RefQuality, (RefQuality.id == File.quality_id))
        .options(joinedload(File.quality, innerjoin=True))
        .filter(
            db.or_(File.fullscreen == file.fullscreen, File.fullscreen == True),
            File.id != file.id,
        )
    )
    if file.media_library == MOVIES:
        query = query.filter(
            File.movie_id == file.movie_id,
            File.feature_type_id == file.feature_type_id,
            File.plex_title == file.plex_title,
            File.edition == file.edition,
        )
    elif file.media_library == TV_SHOWS:
        query = query.filter(
            File.series_id == file.series_id,
            File.season == file.season,
            File.episode == file.episode,
            File.last_episode <= file.last_episode,
        )
    else:
        return []
    return query.all()


def find_worse_files(file, replace_existing=False):
    """Return the same-title files that this file outranks.

    These are the candidates for removal after an upgrade. file is the
    File row of the import. replace_existing is the "Import" choice of
    an admin for a held conversion. A
    conversion to 1080p SDR (#292) also outranks the files with HDR
    or BT.2020 colors, in any quality. After an admin chose "Import"
    for a held conversion, it outranks every file of its title.
    """

    worse_files = _worse_by_quality(file)
    if file.converted_from_quality:
        replace_all = replace_existing

        # A real rip of the same quality outranks a conversion. Only
        # the "Import" choice of an admin lets the conversion replace it.

        if not replace_all:
            worse_files = [
                f
                for f in worse_files
                if f.converted_from_quality
                or f.needs_sdr_conversion
                or f.quality.preference < file.quality.preference
                or (
                    file.media_library == TV_SHOWS
                    and f.last_episode < file.last_episode
                )
            ]
        worse_files += [
            f
            for f in same_identity_files(file)
            if f not in worse_files and (replace_all or f.needs_sdr_conversion)
        ]
    return worse_files


def _worse_by_quality(file):
    """Return the same-title files of the same or a lower quality."""

    worse_files = []

    if file.media_library == MOVIES:
        worse_files = (
            File.query.join(RefQuality, (RefQuality.id == File.quality_id))
            .options(joinedload(File.quality, innerjoin=True))
            .filter(
                File.movie_id == file.movie_id,
                File.feature_type_id == file.feature_type_id,
                File.plex_title == file.plex_title,
                File.edition == file.edition,
                RefQuality.preference <= file.quality.preference,
                db.or_(File.fullscreen == file.fullscreen, File.fullscreen == True),
                File.id != file.id,
            )
            .all()
        )

    elif file.media_library == TV_SHOWS:
        # The edition is absent on purpose. For a TV file, it holds the
        # episode-title segment of the filename. Two releases of the
        # same episode can give it different titles. The episode span
        # is the identity. Thus, an upgrade with a new title still
        # removes its predecessor. A wider span replaces a narrower
        # file only at the same or a better quality, the rule of
        # find_better_files and tv_file_rank. A DVD "S13E01-E06" must
        # not delete a Bluray "S13E01".

        worse_files = (
            File.query.join(RefQuality, (RefQuality.id == File.quality_id))
            .options(joinedload(File.quality, innerjoin=True))
            .filter(
                File.series_id == file.series_id,
                File.season == file.season,
                File.episode == file.episode,
                File.last_episode <= file.last_episode,
                RefQuality.preference <= file.quality.preference,
                db.or_(File.fullscreen == file.fullscreen, File.fullscreen == True),
                File.id != file.id,
            )
            .all()
        )

    return worse_files


def real_rips_blocking(file_details):
    """Return the real rips that make a conversion wait for a decision.

    These are the library files of the same title, edition, or episode
    span that are not conversions and have no HDR or BT.2020 colors.
    Their quality is the quality of the conversion or better."""

    target = RefQuality.query.filter_by(
        quality_title=file_details["quality_title"]
    ).first()
    query = File.query.join(RefQuality, (RefQuality.id == File.quality_id)).filter(
        File.dirname == file_details["dirname"],
        File.fullscreen == False,
        File.converted_from_quality.is_(None),
        RefQuality.preference >= target.preference,
    )
    if file_details["media_library"] == MOVIES:
        query = query.join(Movie, (Movie.id == File.movie_id)).filter(
            Movie.title == file_details["title"],
            Movie.year == file_details["year"],
            File.plex_title == file_details["plex_title"],
            File.edition == file_details.get("edition"),
        )
    else:
        query = query.join(TVSeries, (TVSeries.id == File.series_id)).filter(
            TVSeries.title == file_details["title"],
            File.season == file_details["season"],
            File.episode == file_details["episode"],
            File.last_episode >= file_details["last_episode"],
        )
    return [f for f in query.all() if not f.needs_sdr_conversion]
