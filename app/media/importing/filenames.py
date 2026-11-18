"""The rules for filenames: parse a name, build a name, and reject a file."""

import os
import re
import shutil
import traceback
from datetime import datetime
from pathvalidate import sanitize_filename
from unidecode import unidecode
from flask import current_app
from app import db, makedirs_on_mounted_volume
from app.media.naming import hidden, library_name, visible_name
from app.models import (
    MOVIES,
    TV_SHOWS,
    File,
    Movie,
    RefFeatureType,
    RefQuality,
    TVSeries,
)
from app.metadata.tmdb_apply import tmdb_json

# Plex external-id tags. A movie name or a show-folder name can
# have {tmdb-NNN}, {imdb-ttNNN}, or {tvdb-NNN} after the year, in either
# order with {edition-...}. The id names the exact title. Thus, if the id
# is present, it selects the metadata source. Fitzflix then does not
# search by title.

ID_TAG_BLOCK = r"\{(?:tmdb-\d+|tvdb-\d+|imdb-tt\d+)\}"
NAME_TAG_BLOCK = rf"(?:{ID_TAG_BLOCK}|\{{edition-[^{{}}]+\}})"

ID_TAG_RE = re.compile(
    r"\{(?:(?P<source>tmdb|tvdb)-(?P<value>\d+)|imdb-(?P<imdb_value>tt\d+))\}"
)
EDITION_TAG_RE = re.compile(r"\{edition-(?P<edition>[^{}]+)\}")


def parse_name_tags(tags_text):
    """Split the brace-tag region of a name into its external-id tags and
    its edition.

    Return ([(source, value, raw_text), ...], edition_or_None).
    """

    id_tags = []
    for match in ID_TAG_RE.finditer(tags_text or ""):
        source = match.group("source") or "imdb"
        value = match.group("value") or match.group("imdb_value")
        id_tags.append((source, value, match.group(0)))
    edition_match = EDITION_TAG_RE.search(tags_text or "")
    return id_tags, edition_match.group("edition") if edition_match else None


def preferred_id_tag(id_tags):
    """Return the preferred tag if a name has several. tmdb outranks imdb.
    imdb outranks tvdb."""

    order = {"tmdb": 0, "imdb": 1, "tvdb": 2}
    return min(id_tags, key=lambda tag: order[tag[0]], default=None)


def resolve_external_id_tag(source, value, kind, log=True):
    """Resolve an imdb/tvdb tag to a TMDB id through the TMDB /find endpoint.

    Thus, TMDB stays the single metadata source. kind ("movie" or "tv")
    selects the /find results bucket to read. Return the TMDB id. Return
    False if TMDB answered and knows nothing under that id. Then the
    caller must reject the file. It must not guess by title. Return None
    if TMDB was not reachable. The caller can accept that.
    """

    try:
        body = tmdb_json(
            f"/find/{value}", {"external_source": f"{source}_id"}, timeout=30
        )
        if log:
            current_app.logger.debug(body)
        results = body.get(f"{kind}_results")

    except Exception as e:
        response = getattr(e, "response", None)
        if response is not None and response.status_code == 404:
            # TMDB answered. There is no such external id.
            return False
        current_app.logger.warning(traceback.format_exc())
        return None

    if not results:
        return False

    return results[0].get("id") or False


# The rejection reason of a yearless name whose id tag needs TMDB while
# TMDB cannot be reached. The import tries again. It does not reject.

TMDB_UNREACHABLE = "tmdb unreachable"
TMDB_RETRIES = 48

# The longest source name that leaves room for the names of the work files.

MAX_NAME_BYTES = 230


class FilenameRejection:
    """A falsy evaluate_filename result that names the reason for the
    rejection.

    Thus, the reject is loud. The file goes into a labeled rejects
    subfolder, not into the generic "incorrect filename" folder. A
    reason of None is the generic reject. Then the rejects folder, the
    log line, and the text of the filename tester stay generic. detail
    names the rule that rejected the name, for the log and the tests."""

    def __init__(self, reason, detail=None):
        self.reason = reason
        self.detail = detail or reason

    def __bool__(self):
        return False

    def __repr__(self):
        return f"FilenameRejection({self.reason!r}, {self.detail!r})"


# The name formats. A movie name can have Plex id and edition tags between
# the year and the dash. The yearless Plex form "Title {tmdb-NNN}"
# is also accepted, but only with an id tag. Thus, "Title -
# [Quality].ext" stays rejected.

# The title is lazy. The first "(YYYY)" is the year of the film. An extra
# can carry a year in its own name: "Star Wars (1977) - Featurettes -
# Star Wars (1977) - Original Trailer".

MOVIE_NAME_RE = re.compile(
    r"(?P<title>.+?) \((?P<year>\d{4})\)"
    rf"(?P<tags>(?: {NAME_TAG_BLOCK})*)"
    r" \-(?: (?P<version>.+) | )\[(?P<quality_title>.+)\]\.(?P<extension>.+)"
)
YEARLESS_MOVIE_NAME_RE = re.compile(
    r"(?P<title>.+?)"
    rf"(?P<tags>(?: {NAME_TAG_BLOCK})+)"
    r" \-(?: (?P<version>.+) | )\[(?P<quality_title>.+)\]\.(?P<extension>.+)"
)
TV_NAME_SEARCH_RE = re.compile(
    r"(.+) \- S(\d+)E(\d+)(?:\-E(\d+))? \-(?: (.+) | )\[(.+)\]\.(.+)"
)
TV_NAME_RE = re.compile(
    r"(?P<title>.+) \- S(?P<season>\d+)E(?P<episode>\d+)"
    r"(?:\-E(?P<last_episode>\d+))? \-(?: (?P<version>.+) | )"
    r"\[(?P<quality_title>.+)\]\.(?P<extension>.+)"
)


def evaluate_filename(file_path, tmdb_id=None, log=True, selections=True):
    """Examine a file name and return the details of its movie or TV show.

    Return a FilenameRejection for a name that Fitzflix cannot import.
    Pass log=False for a preview of a filename (for example, the admin
    filename tester). Then the dry run does not fill the log as a real
    import does. Pass selections=False for a file whose record is
    known, as the rename of a TMDB refresh does. Then the name of a
    film with a selected title does not give the file to that film
    (_film_with_the_selected_name).
    """

    basename = os.path.basename(file_path)

    # Try the TV series match first. If not, a TV series with a year in the
    # name (for example, "Doctor Who (2005) - S01E01 - [DVD].mkv") matches
    # as movie: "Doctor Who", year: 2005, version: "S01E01".

    if TV_NAME_SEARCH_RE.search(basename):
        return _parse_tv(basename, log)
    if MOVIE_NAME_RE.match(basename) or YEARLESS_MOVIE_NAME_RE.match(basename):
        return _parse_movie(basename, tmdb_id, log, selections)
    return FilenameRejection(None, "no name format matches")


def _clean(value):
    """Return value with each run of white space as 1 space, or None for an empty value."""

    return " ".join(value.split()).strip() if value else None


def _details(
    media_library,
    dirname,
    basename,
    plex_title,
    title,
    edition,
    quality_title,
    fullscreen,
    extension,
    **numbers,
):
    """Return the details dict of a parsed name, with its text fields cleaned.

    numbers go between title and edition, in their order: year, tmdb_id
    and feature_type_name for a film, or tmdb_id, season, episode and
    last_episode for an episode. The import log prints the dict."""

    return {
        "media_library": _clean(media_library),
        "file_path": _clean(os.path.join(dirname, basename)),
        "dirname": _clean(dirname),
        "basename": _clean(basename),
        "plex_title": _clean(plex_title),
        "title": _clean(title),
        **numbers,
        "edition": _clean(edition),
        "quality_title": _clean(quality_title),
        "fullscreen": True if fullscreen else None,
        "extension": _clean(extension),
    }


def _standard_full_screen(version):
    """Split a version text at " - ". Return (parts, fullscreen).

    Each part that reads as full screen, in any case and with or without
    a space, becomes "Full Screen". fullscreen is True if a part did."""

    parts = version.split(" - ")
    fullscreen = False
    for i, part in enumerate(parts):
        if part.upper().replace(" ", "") == "FULLSCREEN":
            fullscreen = True
            parts[i] = "Full Screen"
    return parts, fullscreen


def _put_full_screen_last(parts):
    """Move the first "Full Screen" part of a version to the end."""

    parts.pop(parts.index("Full Screen"))
    parts.append("Full Screen")


def _known_quality(quality_title):
    """Return True if quality_title is the title of a RefQuality row."""

    return RefQuality.query.filter_by(quality_title=quality_title).first() is not None


def _parse_tv(basename, log):
    """Return the details of an episode name, or a FilenameRejection."""

    tv = TV_NAME_RE.match(basename)
    media_library = "TV Shows"
    title = tv.group("title")

    # A Plex id tag is on the show-folder part of the name. Remove
    # it from the series title, but keep it. The show folder keeps the
    # tag, because Plex reads it there.

    series_id_tag = None
    series_tmdb_id = None
    title_tags = re.fullmatch(rf"(?P<base>.+?)(?P<tags>(?: {ID_TAG_BLOCK})+)", title)
    if title_tags:
        title = title_tags.group("base")
        series_id_tag = preferred_id_tag(parse_name_tags(title_tags.group("tags"))[0])

    if series_id_tag:
        series_tmdb_id = _resolve_series_tag(series_id_tag, basename, log)
        if isinstance(series_tmdb_id, FilenameRejection):
            return series_tmdb_id

        # The id names the exact series. Use the title of the record that
        # already owns that id. Do not use the spelling of the filename.

        if series_tmdb_id:
            existing_series = TVSeries.query.filter_by(tmdb_id=series_tmdb_id).first()
            if existing_series:
                title = existing_series.title

    title = _canonical_series_title(title)

    # A tag names the series exactly. When no record has its id yet, the
    # title can belong to a different series. "The Office {tmdb-2316}"
    # must not join the UK "The Office". There, its episodes could also
    # delete the episodes of that series as worse. Series titles are
    # unique. Thus, the file waits in rejects for a name with the year of
    # the series.

    if series_tmdb_id:
        holder = TVSeries.query.filter_by(title=title).first()
        if holder is not None and holder.tmdb_id not in (None, series_tmdb_id):
            if log:
                current_app.logger.error(
                    f"'{basename}' names TMDB series {series_tmdb_id}, but "
                    f"'{title}' is TMDB series {holder.tmdb_id}. Rename the "
                    f"file with the year of its series"
                )
            return FilenameRejection("title of another series")

    season = int(tv.group("season"))
    episode = int(tv.group("episode"))
    last_episode = int(tv.group("last_episode") or tv.group("episode"))

    # A reversed span ("S01E05-E03") is a bad name. The row, the
    # lock key, and the worse-file rule would all see a bad span.

    if last_episode < episode:
        if log:
            current_app.logger.error(
                f"'{basename}' has a reversed episode span, "
                f"E{episode:02d} to E{last_episode:02d}"
            )
        return FilenameRejection(None, "reversed episode span")

    # If the file quality name is not an expected name, reject the file.

    quality_title = tv.group("quality_title")
    if not _known_quality(quality_title):
        return FilenameRejection(None, "unknown quality")

    extension = tv.group("extension")

    # Remove spaces and periods from the end of the folder name, as Sonarr
    # does:
    # https://github.com/Sonarr/Sonarr/blob/phantom-develop/src/NzbDrone.Core/Organizer/FileNameBuilder.cs#L353

    # The show folder starts as the episode names do, with no period.

    folder_title = visible_name(title, sanitized=False)
    if not folder_title:
        return FilenameRejection("no visible title")
    while folder_title.endswith(" ") or folder_title.endswith("."):
        folder_title = folder_title.strip(" ")
        folder_title = folder_title.strip(".")

    # Tag in, tag out. The show folder keeps the id tag that came with the
    # name. After the tag resolves, it has the canonical tmdb form.

    if series_id_tag:
        folder_tag = (
            f"{{tmdb-{series_tmdb_id}}}" if series_tmdb_id else series_id_tag[2]
        )
        folder_title = f"{folder_title} {folder_tag}"

    if season == 0:
        dirname = os.path.join(media_library, folder_title, "Specials")

    else:
        dirname = os.path.join(
            media_library, folder_title, f"Season {tv.group('season').zfill(2)}"
        )

    fullscreen = False
    version = None
    if tv.group("version"):
        version_strings, fullscreen = _standard_full_screen(tv.group("version"))
        if fullscreen:
            _put_full_screen_last(version_strings)
        version = " - ".join(version_strings)

    # The episode code keeps the digits of the name.

    plex_title, basename = library_name(
        {
            "library": media_library,
            "title": title,
            "season": tv.group("season"),
            "episode": tv.group("episode"),
            "last_episode": tv.group("last_episode"),
            "version": version,
            "quality_title": quality_title,
            "extension": extension,
        }
    )

    return _details(
        media_library,
        dirname,
        basename,
        plex_title,
        title,
        version,
        quality_title,
        fullscreen,
        extension,
        tmdb_id=series_tmdb_id,
        season=season,
        episode=episode,
        last_episode=last_episode,
    )


def _series_record_of_tag(series_id_tag):
    """Return the matched series record that owns the id of a tag, or None.

    This makes no network call. An imdb or tvdb tag matches the stored
    external id of a record that has a TMDB id."""

    source, value, _ = series_id_tag
    if source == "tmdb":
        return TVSeries.query.filter_by(tmdb_id=int(value)).first()
    return (
        TVSeries.query.filter(
            (TVSeries.tvdb_id == int(value))
            if source == "tvdb"
            else (TVSeries.imdb_id == value)
        )
        .filter(TVSeries.tmdb_id != None)
        .first()
    )


def _resolve_series_tag(series_id_tag, basename, log):
    """Return the TMDB id that a series id tag names, None, or a FilenameRejection.

    None means that TMDB could not be reached. A matched series already
    stores its external ids. Thus, an imdb/tvdb tag usually resolves
    without the network."""

    source, value, _ = series_id_tag
    if source == "tmdb":
        return int(value)
    existing = _series_record_of_tag(series_id_tag)
    if existing:
        return existing.tmdb_id
    resolved = resolve_external_id_tag(source, value, "tv", log=log)
    if resolved is False:
        if log:
            current_app.logger.error(
                f"'{basename}' carries {{{source}-{value}}}, "
                f"but TMDB knows no series under that id"
            )
        return FilenameRejection("id not found")
    return resolved


def _canonical_series_title(title):
    """Return the title of the series record that a parsed series title names.

    This is the convention of the movie branch. Sonarr can name a file
    with or without the year of the series. Either form must go to the
    record that already owns the show. It must not split the show into a
    second series. A name WITH A YEAR attaches to the bare-titled record
    only if the year is the same as its TMDB first-air year. "Batman
    (1992)" never goes to the 1966 series. A BARE name attaches to a
    year-suffixed record only if exactly 1 such record exists.

    The result is always the title of the record, not the spelling of
    the file. MariaDB compares titles with no case, no accents,
    and no trailing spaces. Thus, "MONK" finds the record "Monk". The
    dirname, the basename, and the lock key must use "Monk"."""

    existing = TVSeries.query.filter_by(title=title).first()
    if existing is not None:
        return existing.title
    year_form = re.fullmatch(r"(?P<base>.+) \((?P<year>\d{4})\)", title)

    if year_form:
        bare = TVSeries.query.filter_by(title=year_form.group("base")).first()
        if (
            bare is not None
            and bare.tmdb_first_air_date is not None
            and bare.tmdb_first_air_date.year == int(year_form.group("year"))
        ):
            return bare.title
        return title
    candidates = [
        series
        for series in TVSeries.query.filter(
            TVSeries.title.like(f"{title} (____)")
        ).all()
        if re.fullmatch(re.escape(title) + r" \(\d{4}\)", series.title)
    ]
    if len(candidates) == 1:
        return candidates[0].title
    return title


def _parse_movie(basename, tmdb_id, log, selections=True):
    """Return the details of a film name, or a FilenameRejection.

    An explicit tmdb_id (the TMDB-refresh rename path) outranks an id
    tag in the name."""

    movie_match = MOVIE_NAME_RE.match(basename)
    movie = movie_match or YEARLESS_MOVIE_NAME_RE.match(basename)

    media_library = "Movies"
    title = movie.group("title")
    year = int(movie.group("year")) if movie_match else None

    id_tags, edition = parse_name_tags(movie.group("tags"))
    id_tag = preferred_id_tag(id_tags)

    # The yearless Plex form has meaning only with an id tag. An edition
    # tag alone does not identify the film.

    if year is None and id_tag is None:
        return FilenameRejection(None, "no year and no id tag")

    # If the file quality name is not an expected name, reject the file.

    quality_title = movie.group("quality_title")
    if not _known_quality(quality_title):
        return FilenameRejection(None, "unknown quality")

    identity = _resolve_movie_identity(
        basename, title, year, id_tag, tmdb_id, log, selections
    )
    if isinstance(identity, FilenameRejection):
        return identity
    title, year, tmdb_id = identity

    # A title of only periods leaves no name that the disk shows.

    # The reason gives the file a rejects folder of its own and a true
    # log line. When the title comes from the record of TMDB, a new
    # name for the file does not help.

    if not visible_name(title):
        return FilenameRejection("no visible title")

    if log:
        current_app.logger.info(f"File: {basename}")
        current_app.logger.info(f"Movie: {title} ({year})")

    # Tag in, tag out. A name that came with an id tag keeps one on its
    # library paths. After the tag resolves, it has the canonical tmdb
    # form, with {edition-...} after it.

    id_tag_text = None
    if id_tag:
        id_tag_text = f"{{tmdb-{tmdb_id}}}" if tmdb_id else id_tag[2]

    extension = movie.group("extension")
    layout = _movie_layout(
        {
            "library": media_library,
            "title": title,
            "year": year,
            "id_tag": id_tag_text,
            "edition": edition,
            "quality_title": quality_title,
            "extension": extension,
        },
        movie.group("version"),
    )
    if isinstance(layout, FilenameRejection):
        return layout
    dirname, plex_title, basename, version, feature_type, fullscreen = layout
    basename = sanitize_filename(unidecode(basename))

    return _details(
        media_library,
        dirname,
        basename,
        plex_title,
        title,
        version,
        quality_title,
        fullscreen,
        extension,
        year=year,
        tmdb_id=tmdb_id,
        feature_type_name=_clean(feature_type),
    )


def _movie_layout(parts, version_text):
    """Return (dirname, plex_title, basename, version, feature_type, fullscreen) of a film.

    parts holds the library_name parts of the film with no version. The
    basename is not sanitized yet. A special feature type in the version
    text puts the file in the folder of that type, with only its own
    name. "Full Screen" goes at the end of the version. A feature type
    with no name after it is a FilenameRejection. It parsed as an
    extra with the edition of its type and the name of the film."""

    edition = parts["edition"]
    feature_type = None
    special_feature = None

    # The folder is the plex title of the film with no version.

    folder, _ = library_name(parts)
    dirname = os.path.join(parts["library"], sanitize_filename(unidecode(folder)))

    if not version_text:
        plex_title, basename = library_name(parts)
        return dirname, plex_title, basename, edition, feature_type, False

    version_strings, fullscreen = _standard_full_screen(version_text)

    # Get a list of the current possible special feature types.

    special_feature_types = db.session.query(RefFeatureType.feature_type).all()
    special_feature_types = [result[0] for result in special_feature_types]

    if fullscreen == True:
        # Move "Full Screen" in the version string. "Full Screen" must go at
        # the end of the version string if there is no special feature type:
        #
        # Fullscreen - Director's Cut
        # - must become -
        # Director's Cut - Full Screen
        #
        # This is because the file is a full screen version of the
        # Director's Cut. It is not a Director's Cut of the full screen
        # version.
        #
        # But do not put "Full Screen" after a special feature type if it is
        # not already there. If you do, you get:
        #
        # Clang Clang Boogie (2019) - Interviews - Full Screen - I Like Salad [Bluray-1080p].mkv
        # - becomes -
        # Clang Clang Boogie (2019)/Interviews/Full Screen - I Like Salad.mkv
        # - but the correct result is -
        # Clang Clang Boogie (2019)/Interviews/I Like Salad.mkv

        # Compare the uppercase versions of the special feature types. Then
        # the case is not important (for example, "Behind the Scenes" and
        # "Behind The Scenes" are the same).

        if not bool(
            set([v.upper() for v in version_strings]).intersection(
                [t.upper() for t in special_feature_types]
            )
        ):
            _put_full_screen_last(version_strings)

    for type in special_feature_types:
        # If the string has a special feature identifier, get all text after
        # the identifier. Use that text as the name of the special feature.

        if type.upper() in [string.upper() for string in version_strings]:
            type_position = [string.upper() for string in version_strings].index(
                type.upper()
            ) + 1
            special_feature = " - ".join(version_strings[type_position:])
            if not visible_name(special_feature):
                return FilenameRejection(None, f"a {type} extra with no name")
            feature_type = type
            dirname = os.path.join(dirname, sanitize_filename(unidecode(feature_type)))
            break

    # The file name of a special feature is only the special feature. It
    # has no movie title, year, or version. The version string is now the
    # name.

    # An extra of an edition keeps the edition. Else the extras of 2
    # editions with the same name replace each other, and a TMDB rename
    # moves the extra out of its edition folder.

    if special_feature:
        plex_title, basename = library_name(
            dict(parts, feature_type=feature_type, version=special_feature)
        )
        return dirname, plex_title, basename, edition, feature_type, fullscreen

    # "Full Screen" is not part of the version of a film. The name puts
    # it before the quality tag. If the version string is only "Full
    # Screen", report the edition name as the edition, not the raw
    # version.

    if fullscreen:
        version_strings.remove("Full Screen")
    version = " - ".join(version_strings)
    plex_title, basename = library_name(
        dict(parts, version=version or None, fullscreen=fullscreen)
    )
    return dirname, plex_title, basename, version or edition, feature_type, fullscreen


def _file_name_forms(title, year):
    """Return each (title, year) that the parser reads from a file of a film.

    The name of a file loses some characters of a title, for example a
    colon or a question mark. Thus, the title goes through the name
    builder of the library, and the parser reads it back. The untouched
    name keeps the accents, and the library name does not. Both forms
    count."""

    _, name = library_name(
        {
            "library": MOVIES,
            "title": title,
            "year": year,
            "quality_title": "DVD",
            "extension": "mkv",
        }
    )
    forms = set()
    for form in (name, unidecode(name)):
        match = MOVIE_NAME_RE.match(" ".join(sanitize_filename(form).split()))
        if match:
            forms.add((match.group("title"), int(match.group("year"))))
    return forms


def _names_of_a_film(film):
    """Return each (title, year) that names the files of a film record.

    The library name has the title and the year of the record. The
    untouched name has the TMDB title and the year of the release date."""

    names = {(film.title, film.year)}
    if film.tmdb_title:
        tmdb_year = film.tmdb_release_date.year if film.tmdb_release_date else film.year
        names.add((film.tmdb_title, tmdb_year))
    return names


def film_with_the_file_name(title, year, movie):
    """Return another film whose files can have the name of this title and year, or None.

    Invariant: a selected title never gives a file the name that a file
    of another film has. A file with no id tag would have the same name
    for the 2 films, and the film with the selection would get it
    (_film_with_the_selected_name). The title page refuses such a
    selection. Letter case does not make 2 names different. A record
    with the TMDB id of movie is the same film, and a refresh merges
    it."""

    wanted = {
        (name.casefold(), form_year)
        for name, form_year in _file_name_forms(title, year)
    }
    films = Movie.query.filter(
        Movie.id != movie.id,
        db.or_(
            Movie.year == year,
            db.extract("year", Movie.tmdb_release_date) == year,
        ),
    ).order_by(Movie.date_created.asc())
    for film in films:
        if film.tmdb_id is not None and film.tmdb_id == movie.tmdb_id:
            continue
        for other_title, other_year in _names_of_a_film(film):
            forms = _file_name_forms(other_title, other_year)
            if wanted & {(name.casefold(), form_year) for name, form_year in forms}:
                return film
    return None


def _film_with_the_selected_name(title, year):
    """Return the film whose selected title gives a file this title and year, or None.

    Invariant: a new file with the name of a film that has a selected
    title belongs to that film. A restored rip has this name. The TMDB
    search finds a film by its main title first, and an other film of
    the year can have that name.

    The title of each such film is compared as a file shows it
    (_file_name_forms). Only a small number of films have a selection.

    A film with no selection that has exactly this title and year comes
    first. The title page refuses a selection with the file name of a
    film that the library has. Thus, such a film came into the library
    after the selection. The file goes through the TMDB search then, as
    a file of a film with no selection does."""

    exact = Movie.query.filter(
        Movie.title == title, Movie.year == year, Movie.chosen_title == None
    ).first()
    if exact is not None:
        return None
    films = Movie.query.filter(
        Movie.chosen_title != None, Movie.tmdb_id != None
    ).order_by(Movie.date_created.asc())
    for film in films:
        shown = film.tmdb_title or film.title
        shown_year = film.year
        if film.tmdb_title and film.tmdb_release_date:
            shown_year = film.tmdb_release_date.year
        if (title, year) in _file_name_forms(shown, shown_year):
            return film
    return None


def _resolve_movie_identity(
    basename, title, year, id_tag, tmdb_id, log, selections=True
):
    """Return the (title, year, tmdb_id) of a film, or a FilenameRejection.

    This is the only network part of the parse, and the only part that
    knows TMDB_UNREACHABLE. An id tag selects the metadata source.
    A tmdb tag is the id itself. An imdb/tvdb tag resolves through the
    library first (a matched movie stores its imdb id), then through the
    TMDB /find endpoint. An id that TMDB definitely does not know is a
    loud reject. A fallback to a title search could attach the wrong
    film. An explicit tmdb_id argument (the TMDB-refresh rename path)
    outranks the tag."""

    tmdb_unreachable = False
    id_from_tag = False
    if id_tag and not tmdb_id:
        source, value, _ = id_tag
        id_from_tag = True
        if source == "tmdb":
            tmdb_id = int(value)
        else:
            if source == "imdb":
                existing = (
                    Movie.query.filter_by(imdb_id=value)
                    .filter(Movie.tmdb_id != None)
                    .order_by(Movie.date_created.asc())
                    .first()
                )
                if existing:
                    tmdb_id = existing.tmdb_id
            if not tmdb_id:
                resolved = resolve_external_id_tag(source, value, "movie", log=log)
                if resolved is False:
                    if log:
                        current_app.logger.error(
                            f"'{basename}' carries {{{source}-{value}}}, "
                            f"but TMDB knows no movie under that id"
                        )
                    return FilenameRejection("id not found")
                if resolved is None:
                    tmdb_unreachable = bool(current_app.config.get("TMDB_API_KEY"))
                tmdb_id = resolved

    # Name the film as TMDB names it, because a film can have alternate
    # titles or spellings. For example:
    # A Fistful of Dynamite == Duck, You Sucker
    # Fifth Avenue Girl == 5th Avenue Girl

    tmdb_result = None
    m = None
    if selections and not tmdb_id and id_tag is None and year:
        # The record with the selection gives the id, with no search
        # (_film_with_the_selected_name).

        selected = _film_with_the_selected_name(title, year)
        if selected is not None:
            tmdb_id = selected.tmdb_id

    if tmdb_id:
        # A record that already owns this id has the canonical title and
        # year. The network is not necessary.

        m = (
            Movie.query.filter_by(tmdb_id=tmdb_id)
            .order_by(Movie.date_created.asc())
            .first()
        )

    if m:
        if log:
            current_app.logger.info(f"Existing movie with this TMDB id: {m}")
        title = m.title
        year = m.year

    elif tmdb_id or id_tag is None:
        # If an id tag did not resolve (TMDB not reachable), never run the
        # title search. It could attach the wrong film.

        try:
            if tmdb_id:
                # This reads only the id, the title, and the release date.
                # Thus, it requests no appended blocks.
                params = {}
                url = "/movie/" + str(tmdb_id)
            else:
                params = {"query": title, "primary_release_year": year}
                url = "/search/movie"
            body = tmdb_json(url, params, timeout=30)
            current_app.logger.debug(body)

        except Exception as e:
            # A TMDB API problem must not prevent the import of the file.
            # The exception is a 404 on an id that the filename named. That
            # id does not exist, and a guess would be worse.

            response = getattr(e, "response", None)
            if id_from_tag and response is not None and response.status_code == 404:
                if log:
                    current_app.logger.error(
                        f"'{basename}' carries {{tmdb-{tmdb_id}}}, "
                        f"but TMDB knows no movie under that id"
                    )
                return FilenameRejection("id not found")
            current_app.logger.warning(traceback.format_exc())
            tmdb_result = None

            # With no API key, a later try cannot do better.

            tmdb_unreachable = bool(current_app.config.get("TMDB_API_KEY"))

        else:
            tmdb_result = body

    if tmdb_result:
        if tmdb_id:
            # /movie/<id> returns the movie object itself, not a results
            # array.
            tmdb_results = [tmdb_result] if tmdb_result.get("id") else None
        else:
            tmdb_results = tmdb_result.get("results")
        if tmdb_results:
            current_app.logger.debug(f"TMDB results: {tmdb_results}")
            tmdb_film = tmdb_results[0]

            # Look for this tmdb_id in the database.

            m = (
                Movie.query.filter_by(tmdb_id=tmdb_film.get("id"))
                .order_by(Movie.date_created.asc())
                .first()
            )

            if log:
                current_app.logger.info(f"Existing movie with this TMDB id: {m}")

            # If it exists, use the existing film title and year. Do not use
            # the parsed values.

            if m:
                title = m.title
                year = m.year

            # If not, use the title and year from TMDB.

            else:
                title = tmdb_film.get("title", title)
                release_date = tmdb_film.get("release_date") or (
                    f"{year}-01-01" if year else None
                )
                if release_date:
                    release_date = datetime.strptime(release_date, "%Y-%m-%d")
                    year = release_date.year

    # The yearless form has no fallback. Without a library record or a
    # reachable TMDB, there is no year to file the movie under.

    # A TMDB outage is not a bad name. The import tries again later.

    if year is None and tmdb_unreachable:
        if log:
            current_app.logger.warning(
                f"'{basename}' has no year, and TMDB could not be reached "
                f"to resolve its id tag"
            )
        return FilenameRejection(TMDB_UNREACHABLE)

    if year is None:
        if log:
            current_app.logger.error(
                f"'{basename}' has no year, and its id tag couldn't be "
                f"resolved to one"
            )
        return FilenameRejection("id not resolvable")

    return title, year, tmdb_id


def iso_639_3_native_language():
    """Return the ISO-639-3 code for the native language.

    MakeMKV uses ISO-639-3 when it writes its MKV files. But the Matroska
    specification calls for ISO-639-*2* bibliographic language codes. The
    codes are the same in most cases, but some languages differ. For
    example, a French MKV from MakeMKV has the 639-3 code "fra" as its
    language code. But the mkvtoolnix tools do not know "fra". They expect
    "fre". If MakeMKV made the file, Fitzflix must convert the native
    language code of the user from 639-2 to 639-3. Then it can check if
    the language exists in the file.
    https://www.makemkv.com/forum/viewtopic.php?t=3271
    """

    iso = {
        # 639   639
        #  -2    -3
        "alb": "sqi",
        "arm": "hye",
        "baq": "eus",
        "bur": "mya",
        "chi": "zho",
        "cze": "ces",
        "dut": "nld",
        "fre": "fra",
        "geo": "kat",
        "ger": "deu",
        "gre": "ell",
        "ice": "isl",
        "mac": "mkd",
        "mao": "mri",
        "may": "msa",
        "per": "fas",
        "rum": "ron",
        "slo": "slk",
        "tib": "bod",
        "wel": "cym",
    }

    native_language = current_app.config["NATIVE_LANGUAGE"]
    if native_language in iso:
        current_app.logger.info(
            f"Native language '{native_language}' has different codes "
            f"for ISO-639-2 and ISO-639-3; switching to '{iso.get(native_language)}'"
        )
        native_language = iso.get(native_language)

    else:
        current_app.logger.info(
            f"Native language is '{native_language}', no need to translate ISO code"
        )

    return native_language


def move_to_rejects(file_path, reason=""):
    """Move a file to the rejects directory, best effort.

    If a volume is not available, return False. Do not raise. A dead mount
    must not turn 1 failure into a cascade. The file stays where it is for
    a later re-import.

    A cross-volume move goes through a hidden name. This function promotes
    the file only after the copy is complete. Thus, a failure during the
    copy can never leave a partial file in the rejects directory under an
    importable name.
    """

    basename = os.path.basename(file_path)
    reject_directory = os.path.join(current_app.config["REJECTS_DIR"], reason)
    destination = os.path.join(reject_directory, basename)
    hidden_destination = hidden(os.path.join(reject_directory, basename), ".partial")

    try:
        # The rejects directory is on the external SSD. The SSD can drop
        # during a long task. Then the file stays in place

        makedirs_on_mounted_volume(reject_directory)
        try:
            os.rename(file_path, destination)
        except OSError:
            # The destination is a different volume, or the filesystem refused
            # the rename. Copy to the hidden name, promote it, then delete the
            # source. If a step fails, remove both destinations. Then the
            # state is exactly "the source stays where it is". The result is
            # complete or nothing.

            try:
                shutil.copy2(file_path, hidden_destination)
                os.replace(hidden_destination, destination)
                os.remove(file_path)
            except OSError:
                for stray in (hidden_destination, destination):
                    try:
                        os.remove(stray)
                    except OSError:
                        pass
                raise

    except OSError as e:
        current_app.logger.error(
            f"'{basename}' Could not be moved to the rejects "
            f"directory ({e}); leaving it in place"
        )
        return False

    current_app.logger.info(f"'{basename}' Moved to rejects directory")
    return True


def sanitize_string(
    string, additional_bad_characters=[], additional_good_characters=[]
):
    """Remove or replace the bad characters in a string and convert it to ASCII."""

    original_string = string

    # The default map of bad to good characters is based on the character
    # replacement of Sonarr:
    # https://github.com/Sonarr/Sonarr/blob/phantom-develop/src/NzbDrone.Core/Organizer/FileNameBuilder.cs#L329

    # fmt: off
    bad_characters  = ["\\", "/", "<", ">", "?", "!", "*", ":", "|", '"',   "…", "“", "”", "‘", "’"]
    good_characters = ["+",  "+",  "",  "",  "",  "", "-", "-",  "",  "", "...",  "",  "", "'", "'"]
    # fmt: on

    if len(additional_bad_characters) != len(additional_good_characters):
        raise IndexError(
            f"{additional_bad_characters} and {additional_good_characters} "
            f"are different lengths"
        )

    bad_characters = bad_characters + additional_bad_characters
    good_characters = good_characters + additional_good_characters

    # Do the unidecode first, because it can add an unwanted character.

    string = unidecode(string)

    # Replace the bad characters with the good characters.

    for i, bad_char in enumerate(bad_characters):
        string = string.replace(bad_char, good_characters[i])

    # Make sure the string is correct for the filesystem.

    string = sanitize_filename(string)

    # Remove duplicate spaces.

    while "  " in string:
        string = string.replace("  ", " ")

    string = string.strip()

    # A name must not start with a period. Then the file is not
    # invisible. Each period at the start goes, as in the library names.

    string = string.lstrip(". ")

    # Fail loudly. An empty name must not go into the construction of a file
    # name or an S3 key. There it would make degenerate paths.

    if not string:
        raise ValueError(
            f"'{original_string}' sanitizes to an empty string, so it can't be "
            f"used in a file or key name"
        )

    return string


def reconstruct_filename(file_id, series_title=None):
    """Reconstruct the untouched filename from the current details.

    A film gets the current title and year of its movie record. An
    episode gets the current title of its series, or series_title (a
    series rename passes the new title before it commits it). The other
    parts of an episode name stay as the untouched name has them."""

    f = File.query.filter_by(id=file_id).first()
    if not f:
        return False
    if f.media_library != MOVIES:
        return _reconstruct_episode_name(f, series_title)

    file = (
        db.session.query(File, Movie, RefQuality, RefFeatureType)
        .join(Movie, (Movie.id == File.movie_id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .outerjoin(RefFeatureType, (RefFeatureType.id == File.feature_type_id))
        .filter(File.id == file_id)
        .first()
    )

    if not file:
        return False

    f, m, q, ft = file

    _, ext = os.path.splitext(f.untouched_basename)

    # TMDB can send a title with an empty release date. Then the year of
    # the record stands in. The untouched file of a conversion is its
    # original. Its name keeps the quality of the original, not
    # the quality of the library file. Else a converge run moves
    # the archive to the wrong key.

    parts = {
        "library": MOVIES,
        "title": m.title,
        "year": m.year,
        "edition": f.edition,
        "quality_title": f.converted_from_quality or q.quality_title,
        "fullscreen": f.fullscreen == True,
        "extension": ext.lstrip("."),
    }
    if m.tmdb_title != None:
        parts["title"] = m.tmdb_title
        parts["year"] = m.tmdb_release_date.year if m.tmdb_release_date else m.year

    # Tag in, tag out. An untouched name that had a Plex id tag
    # keeps one. If the current tmdb id of the record is known, the tag
    # uses it.

    id_tag = ID_TAG_RE.search(f.untouched_basename)
    if id_tag:
        parts["id_tag"] = f"{{tmdb-{m.tmdb_id}}}" if m.tmdb_id else id_tag.group(0)

    # The untouched name of an extra holds its type and its name as the
    # version. A full screen copy keeps only "Full Screen".

    if f.fullscreen != True and f.feature_type_id != None:
        parts["version"] = f"{ft.feature_type} - {f.plex_title}"

    _, reconstructed_filename = library_name(parts)
    reconstructed_filename = sanitize_filename(reconstructed_filename)
    return " ".join(reconstructed_filename.split()).strip()


def _reconstruct_episode_name(f, series_title=None):
    """Return the untouched name of an episode with the current title of its series.

    The season, the episode numbers, the version, the quality and the
    extension stay as the untouched name has them. An id tag on the
    series part stays. It gets the current TMDB id of the series. A name
    that does not parse stays as it is."""

    match = TV_NAME_RE.match(f.untouched_basename or "")
    series = f.tv_series
    if match is None or series is None:
        return f.untouched_basename

    title = series_title or series.title
    title_tags = re.fullmatch(
        rf"(?P<base>.+?)(?P<tags>(?: {ID_TAG_BLOCK})+)", match.group("title")
    )
    if title_tags:
        tags = f" {{tmdb-{series.tmdb_id}}}" if series.tmdb_id else title_tags["tags"]
        title = f"{title}{tags}"

    _, name = library_name(
        {
            "library": TV_SHOWS,
            "title": title,
            "season": match.group("season"),
            "episode": match.group("episode"),
            "last_episode": match.group("last_episode"),
            "version": match.group("version"),
            "quality_title": match.group("quality_title"),
            "extension": match.group("extension"),
        }
    )
    return sanitize_filename(name)
