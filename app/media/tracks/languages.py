"""The language codes and the language names of the tracks."""

import re
import subprocess
import traceback
from functools import lru_cache
from flask import current_app
from app import db
from app.cache import cached_fetch
from app.models import FileAudioTrack
from app.models import FileSubtitleTrack
from app.models import Movie

# These are the languages that a track can have. Fitzflix reads them
# from the table of mkvtoolnix. Thus, the file page can never offer a
# code that the edit would reject. The keys are the ISO 639-2 codes.
# The track records store those codes. The other columns become
# aliases, not choices

FALLBACK_LANGUAGES = (
    ("eng", "English"),
    ("und", "Undetermined"),
    ("zxx", "No linguistic content"),
)

# ISO 639-2 gives 20 languages 2 codes. The bibliographic code is the
# only one that mkvtoolnix lists. The terminological code is what
# MediaInfo reports for some files ("deu" not "ger", "fra" not "fre").
# Both codes are the same language. Thus, the terminological spellings
# must resolve to the codes that the table knows. Fitzflix must not
# refuse them as unknown

ISO_639_2_TERMINOLOGIC = {
    "bod": "tib",
    "ces": "cze",
    "cym": "wel",
    "deu": "ger",
    "ell": "gre",
    "eus": "baq",
    "fas": "per",
    "fra": "fre",
    "hye": "arm",
    "isl": "ice",
    "kat": "geo",
    "mkd": "mac",
    "mri": "mao",
    "msa": "may",
    "mya": "bur",
    "nld": "dut",
    "ron": "rum",
    "slk": "slo",
    "sqi": "alb",
    "zho": "chi",
}


def language_key(code):
    """Return the bibliographic ISO 639-2 spelling of a language code.

    A terminological code ("deu", "fra") becomes its bibliographic code
    ("ger", "fre"). Any other value comes back in lower case. Thus, 2
    spellings of the same language give the same key."""

    code = str(code or "").strip().lower()
    return ISO_639_2_TERMINOLOGIC.get(code, code)


def same_language(first, second):
    """Return True if 2 language codes name the same language."""

    return bool(first) and bool(second) and language_key(first) == language_key(second)


def file_language_spelling(code, tracks):
    """Return the spelling of a language as the tracks of a file write it.

    MediaInfo reports the terminological code for some writers, and
    the bibliographic code for others. The mkvmerge language filters
    match the spelling in the file. This returns code if no track has
    the language."""

    for track in tracks:
        if same_language(track.get("language"), code):
            return track["language"]
    return code


@lru_cache(maxsize=1)
def _language_table():
    """Return the language table of mkvtoolnix as (name, 639-3, 639-2, 639-1) rows.

    The cache lasts for the life of the process. The table belongs to
    the installed mkvtoolnix, not to 1 file. The result is empty if
    Fitzflix cannot read the table. The callers then use a fallback.
    """

    try:
        listing = subprocess.run(
            [current_app.config["MKVMERGE_BIN"], "--list-languages"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    except Exception:
        current_app.logger.error(traceback.format_exc())
        return ()

    rows = []
    for line in listing.splitlines():
        # The format is "English language name | ISO 639-3 | ISO 639-2 |
        # ISO 639-1". The header and rule rows fail the 3-character check.
        # A 639-3-only language (Cantonese "yue", Mandarin "cmn") keeps an
        # empty 639-2 column. The dropdowns leave it out, but a track can
        # hold its code, and mkvpropedit sets it

        columns = [column.strip() for column in line.split("|")]
        if len(columns) < 4:
            continue
        if len(columns[2]) != 3 and not (not columns[2] and len(columns[1]) == 3):
            continue

        rows.append(tuple(columns[:4]))

    return tuple(rows)


@lru_cache(maxsize=1)
def iso_639_2_languages():
    """Return each (code, name) pair that a track can have, sorted by name."""

    rows = _language_table()
    if not rows:
        # Without the table, the page can still offer the 3 codes that
        # this feature exists to correct between

        return FALLBACK_LANGUAGES

    return tuple(
        sorted(
            ((iso_639_2, name) for name, _, iso_639_2, _ in rows if iso_639_2),
            key=lambda language: language[1].lower(),
        )
    )


@lru_cache(maxsize=1)
def _code_names():
    """Map each code that a track can hold to its name: 639-2, or 639-3 with no 639-2."""

    return {
        iso_639_2 or iso_639_3: name
        for name, iso_639_3, iso_639_2, _ in _language_table()
    }


@lru_cache(maxsize=1)
def _language_aliases():
    """Map each name of a language, in lowercase, to its code.

    The code is the 639-2 code, or the 639-3 code of a language that has
    no 639-2 code. The names are the language name, each code column in
    the table, and the ISO 639-2/T spellings that are not in the table.
    See ISO_639_2_TERMINOLOGIC.
    """

    aliases = {}

    # The 639-2 rows come first. Thus, a name that a 639-3-only language
    # shares maps to the 639-2 code. All codes come before all names.
    # Thus, the language named "Lou" (loj) does not take the code "lou"
    # of Louisiana Creole.

    rows = sorted(_language_table(), key=lambda row: not row[2])
    for name, iso_639_3, iso_639_2, iso_639_1 in rows:
        for alias in (iso_639_3, iso_639_2, iso_639_1):
            if alias:
                aliases.setdefault(alias.lower(), iso_639_2 or iso_639_3)
    for name, iso_639_3, iso_639_2, iso_639_1 in rows:
        if name:
            aliases.setdefault(name.lower(), iso_639_2 or iso_639_3)

    for terminologic, bibliographic in ISO_639_2_TERMINOLOGIC.items():
        if bibliographic in aliases:
            aliases.setdefault(terminologic, aliases[bibliographic])

    if not aliases:
        for code, name in FALLBACK_LANGUAGES:
            aliases[code] = code
            aliases[name.lower()] = code

    return aliases


@lru_cache(maxsize=1)
def language_names():
    """Map each code that a track record can hold to its display name.

    The keys are the aliases, not only the canonical codes. Thus, a
    track stored as "deu" still shows as German.
    """

    names = _code_names() or dict(iso_639_2_languages())
    return {
        alias: names[code]
        for alias, code in _language_aliases().items()
        if code in names
    }


# Fitzflix builds this again each hour, not on each render. A language
# that is new to the library appears in the dropdowns in that time

LANGUAGE_CHOICES_KEY = "fitzflix:language-choices"
LANGUAGE_CHOICES_SECONDS = 3600


def library_language_choices():
    """Return the languages to offer on the File page, as (code, name).

    The result has each ISO 639-2 language that also has a 639-1 code.
    That is 183 of the 1006 languages in the table. In practice, it is
    the set with a real publishing presence. The result also has each
    language that this collection uses outside that set.

    The whole table is approximately 54 KB of options per track. A
    select repeats its options for each track. Thus, the Doctor Who
    disc with 21 tracks would have 1 MB of options. The 639-1 set is
    8 KB per track. It still covers each film that a person could
    buy. It leaves out the long tail of dead languages and collective
    groups (Akkadian, "Algonquian languages"). No audio track is in
    those languages.

    The languages of the collection are always in the result. Thus, a
    track can never hold a language that the dropdown cannot show. und
    and zxx have no 639-1 code and would be missing without this. The
    list also grows when films in new languages arrive.
    """

    # Three DISTINCT queries over the track tables take approximately
    # 40 ms. That is too much for each File page render. The answer
    # changes only when a new language enters the library

    choices = cached_fetch(
        LANGUAGE_CHOICES_KEY, LANGUAGE_CHOICES_SECONDS, _stored_language_choices
    )
    return tuple(tuple(pair) for pair in choices)


def file_language_choices(stored_codes):
    """Return the File page choices, with the languages of the file itself.

    The cached choices can predate a language that is new to the
    library. Then the dropdown of such a track selected nothing, and the
    browser posted the first option (Abkhazian) as a change. Thus, each
    resolved code of the file joins the choices."""

    choices = list(library_language_choices())
    offered = {code for code, _ in choices}
    names = _code_names() or dict(iso_639_2_languages())
    missing = {
        code
        for code in (resolve_language_code(value) for value in stored_codes)
        if code and code not in offered
    }
    if not missing:
        return tuple(choices)
    choices += [(code, names.get(code, code)) for code in missing]
    return tuple(sorted(choices, key=lambda language: language[1].lower()))


def _stored_language_choices():
    """Return the (code, name) choices of the library, for cached_fetch."""

    stored = set()
    for column in (
        FileAudioTrack.language,
        FileSubtitleTrack.language,
        Movie.tmdb_original_language,
    ):
        stored |= {value for (value,) in db.session.query(column).distinct() if value}

    codes = {resolve_language_code(value) for value in stored}
    codes |= {
        iso_639_2
        for name, iso_639_3, iso_639_2, iso_639_1 in _language_table()
        if iso_639_1 and iso_639_2
    }
    codes |= {
        "und",
        "zxx",
        resolve_language_code(current_app.config["NATIVE_LANGUAGE"]),
    }
    codes.discard(None)

    names = _code_names() or dict(iso_639_2_languages())
    return (
        sorted(
            ((code, names.get(code, code)) for code in codes),
            key=lambda language: language[1].lower(),
        ),
        True,
    )


def resolve_language_code(value):
    """Return the language code for the text typed in a language box.

    The code is ISO 639-2, or ISO 639-3 for a language without a 639-2
    code.

    The boxes hold language names. A bare code in one of the 3
    standards also works. The "German (ger)" pair also works. A person
    who types what they know must not have to guess the spelling that
    the table wants. Return None if nothing matches. The caller reports
    that. It does not guess.
    """

    value = (value or "").strip().lower()
    if not value:
        return None

    aliases = _language_aliases()
    if value in aliases:
        return aliases[value]

    paired = re.fullmatch(r"(?P<name>.+?)\s*\((?P<code>[a-z]{2,3})\)", value)
    if paired and paired.group("code") in aliases:
        return aliases[paired.group("code")]

    return None
