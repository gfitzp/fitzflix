"""Build the names of the library files and of their hidden work files.

library_name is the one builder of a library file name. The 2 name
parsers of app.importing, reconstruct_filename, the manual-import
proposal and the TMDB rename use it. retag_quality changes the quality
tag of a name. A conversion (#292) uses it, because its library file has
a different quality than its original. hidden gives the dot-prefixed
name of a work file next to its final file.

The names go to disk and to S3 keys. Thus, a change here changes the
keys of new archives. tests/test_filenames.py holds the corpus of the
current names.
"""

import os

from app.models import TV_SHOWS


def _number(value):
    """Return a season or episode number as text with at least 2 digits.

    The text of a parsed name keeps its own digits. Thus, "001" stays
    "001", and 1 becomes "01"."""

    return str(value).zfill(2)


def episode_code(season, episode, last_episode=None):
    """Return the episode code of a name, for example "S01E05" or "S01E05-E06"."""

    code = f"S{_number(season)}E{_number(episode)}"
    if last_episode is not None:
        code = f"{code}-E{_number(last_episode)}"
    return code


def library_name(parts):
    """Return (plex_title, basename) of a library file.

    parts is a mapping with these keys. A missing key counts as None.

    - library: "Movies" or "TV Shows"
    - title: the film title, or the series title of an episode
    - year: the year of a film
    - id_tag: the id tag of a film, for example "{tmdb-578}"
    - season, episode, last_episode: the numbers of an episode. The
      last_episode is None for an episode that is not a range.
    - version: the version text. For an extra, it is the name of the
      extra.
    - edition: the edition of a film, for its "{edition-...}" tag
    - feature_type: the type of an extra, for example "Trailers"
    - quality_title: the quality, for example "Bluray-1080p"
    - fullscreen: True for a full screen copy of a film
    - extension: the extension, with no period

    The basename is not sanitized. An episode keeps "Full Screen" in its
    version text. A film puts it before the quality tag."""

    version = parts.get("version")
    quality = parts.get("quality_title")
    extension = parts.get("extension")

    if parts.get("library") == TV_SHOWS:
        code = episode_code(
            parts.get("season"), parts.get("episode"), parts.get("last_episode")
        )
        if version:
            plex_title = f"{parts.get('title')} - {code} - {version}"
            return plex_title, f"{plex_title} [{quality}].{extension}"
        plex_title = f"{parts.get('title')} - {code}"
        return plex_title, f"{plex_title} - [{quality}].{extension}"

    if parts.get("feature_type") and version:
        return version, f"{version}.{extension}"

    head = f"{parts.get('title')} ({parts.get('year')})"
    if parts.get("id_tag"):
        head = f"{head} {parts['id_tag']}"
    if parts.get("edition"):
        head = f"{head} {{edition-{parts['edition']}}}"
    plex_title = f"{head} - {version}" if version else head
    if parts.get("fullscreen"):
        return plex_title, f"{plex_title} - Full Screen [{quality}].{extension}"
    if version:
        return plex_title, f"{plex_title} [{quality}].{extension}"
    return plex_title, f"{plex_title} - [{quality}].{extension}"


def retag_quality(name, old, new):
    """Return name with its quality tag "[old]" changed to "[new]".

    A conversion keeps the quality of its original in its untouched name.
    Its library file and its library archive have the converted quality."""

    return name.replace(f"[{old}]", f"[{new}]")


def with_extension(name, extension):
    """Return name with its last extension changed to extension (with no period)."""

    return f"{os.path.splitext(name)[0]}.{extension.lstrip('.')}"


def hidden(path, suffix=""):
    """Return the hidden work path of path: a period before its name, then suffix.

    A work file is hidden until it is complete. Then a rename gives it the
    final name. The import sweep and Plex do not see a hidden name."""

    directory, name = os.path.split(path)
    return os.path.join(directory, f".{name}{suffix}")
