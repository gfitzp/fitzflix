"""Setting a track's language from the File page (#218), and the markup
fix that page needed first (#222).

The language boxes are the only way to correct the und/zxx assignments
MediaInfo falls back to when a disc's headers are vague, so the value
that reaches mkvpropedit has to survive three hops: what a browser's
datalist puts in the box, the route's diff against what's stored, and
the argument mkvpropedit is handed. The end-to-end test does the last
hop against a real Matroska file, since a rejected --set language would
otherwise look like a silent no-op.

#222 rides along because it is the same template: a </div> that sat
outside {% if subtitle_tracks %} closed the left column early on any
file without subtitles, so the remux column escaped the grid row and
stretched the width of the page. The grid it broke is gone now — every
track control lives in the Tracks table — so the guard is a balance
check over the whole page, which is the class of bug that was.
"""

import os
import shutil
import subprocess

from html.parser import HTMLParser

import pytest

from tests.conftest import _TMP


@pytest.fixture(scope="module")
def undetermined_mkv(app):
    """A 1-second Matroska whose two audio tracks have no language set,
    which is the state the File page exists to correct."""

    base = os.path.join(_TMP, "undetermined-base.mp4")
    mkv = os.path.join(_TMP, "undetermined.mkv")
    if not os.path.exists(mkv):
        subprocess.run(
            [app.config["FFMPEG_BIN"]]
            + ["-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10"]
            + ["-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
            + ["-f", "lavfi", "-i", "sine=frequency=880:duration=1"]
            + ["-map", "0:v", "-map", "1:a", "-map", "2:a"]
            + ["-c:v", "libx264", "-c:a", "aac", "-shortest", "-y", base],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [app.config["MKVMERGE_BIN"], "-o", mkv]
            + ["--language", "1:und", "--language", "2:und"]
            + ["--default-track", "1:1"]
            + ["--default-track", "2:0"]
            + [base],
            check=True,
            capture_output=True,
        )
    return mkv


def _audio_languages(app, path):
    """The language code of each audio track, read the way the app reads it."""

    from app.tracks import get_audio_tracks_from_file

    with app.app_context():
        return [track["language"] for track in get_audio_tracks_from_file(path)]


def test_the_catalogue_is_the_codes_the_records_can_hold(app):
    """Every offered language is a 3-character ISO 639-2 code — the width
    of the column the answer is stored in — and the three this feature
    exists to move between are all there."""

    from app.tracks import iso_639_2_languages

    with app.app_context():
        languages = iso_639_2_languages()

    assert len(languages) > 100
    assert all(len(code) == 3 for code, name in languages)
    assert all(name for code, name in languages)

    codes = dict(languages)
    assert codes["eng"] == "English"
    assert "und" in codes and "zxx" in codes

    names = [name.lower() for code, name in languages]
    assert names == sorted(names), "the datalist reads alphabetically"


def test_a_typed_language_resolves_however_the_browser_filled_it_in(app):
    """Browsers disagree over whether a datalist option's label is
    offered for matching, so the name and the "English (eng)" pairing
    have to resolve as readily as the bare code."""

    from app.tracks import resolve_language_code

    with app.app_context():
        assert resolve_language_code("eng") == "eng"
        assert resolve_language_code(" ENG ") == "eng"
        assert resolve_language_code("English") == "eng"
        assert resolve_language_code("english") == "eng"
        assert resolve_language_code("English (eng)") == "eng"
        assert resolve_language_code("de") == "ger"

        # ISO 639-2 gives twenty languages two codes and mkvtoolnix lists
        # only the bibliographic one, but MediaInfo writes the
        # terminological one into some track records. Refusing those
        # would lock the whole property-edit form on 212 files

        assert resolve_language_code("deu") == "ger"
        assert resolve_language_code("fra") == "fre"
        assert resolve_language_code("German") == "ger"

        # Nothing is guessed at: an unknown entry comes back as None so
        # the caller can refuse the edit rather than write a bad code

        assert resolve_language_code("Gibberish") is None
        assert resolve_language_code("engg") is None
        assert resolve_language_code("") is None
        assert resolve_language_code(None) is None


def test_the_edit_rewrites_the_languages_in_the_file(app, undetermined_mkv):
    """The hop that can't be faked: mkvpropedit accepts the argument and
    the file comes back carrying the new codes."""

    from app import db
    from app.tracks import mkvpropedit_unlocked
    from app.models import FileAudioTrack
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        movie = make_movie("Language Test", 2021)
        file = make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        file_id = file.id
        library_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)

    os.makedirs(os.path.dirname(library_path), exist_ok=True)
    shutil.copy(undetermined_mkv, library_path)

    assert _audio_languages(app, library_path) == ["und", "und"]

    with app.app_context():
        assert mkvpropedit_unlocked(file_id, 1, None, None, {"a1": "eng"}) is True

    # Only the track that was asked about moves

    assert _audio_languages(app, library_path) == ["eng", "und"]

    # And the stored rows follow the file, since the edit rescans it

    with app.app_context():
        rows = (
            FileAudioTrack.query.filter_by(file_id=file_id)
            .order_by(FileAudioTrack.track)
            .all()
        )
        assert [(row.track, row.language) for row in rows] == [(1, "eng"), (2, "und")]
        assert rows[0].language_name == "English"


def _matroska_file_page(
    app, admin_client, *, subtitles, local=True, language="und", subtitle_default=True
):
    """A Matroska file with one audio track (and optionally one subtitle
    track), and its rendered File page. Returns the file's library path
    only when `local`, since that is what the caller has to clean up."""

    from app import db
    from app.models import FileAudioTrack, FileSubtitleTrack
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        movie = make_movie(
            f"Page Markup {language} {subtitle_default}", 2022 if subtitles else 2023
        )
        file = make_movie_file(movie, "Bluray-1080p", container="Matroska")
        db.session.flush()
        db.session.add(
            FileAudioTrack(
                file_id=file.id,
                track=1,
                language=language,
                language_name="Undetermined",
                format="DTS",
                codec="DTS",
                channels="6",
                default=True,
                streamorder=0,
            )
        )
        if subtitles:
            db.session.add(
                FileSubtitleTrack(
                    file_id=file.id,
                    track=1,
                    language="und",
                    language_name="Undetermined",
                    format="PGS",
                    elements=900,
                    default=subtitle_default,
                    forced=False,
                    streamorder=1,
                )
            )
        db.session.commit()
        file_id = file.id
        library_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)

    if local:
        os.makedirs(os.path.dirname(library_path), exist_ok=True)
        with open(library_path, "wb") as handle:
            handle.write(b"mkv bytes")
    else:
        library_path = None

    page = admin_client.get(f"/file/{file_id}").get_data(as_text=True)
    return file_id, library_path, page


class _Balance(HTMLParser):
    """Records tags that close without opening, and tags left open."""

    VOID = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.stray = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if tag not in self.stack:
            self.stray.append(tag)
            return
        while self.stack and self.stack.pop() != tag:
            pass


@pytest.mark.parametrize("subtitles", [False, True])
def test_the_page_markup_stays_balanced(app, admin_client, subtitles):
    """#222 was a </div> sitting outside its {% if subtitle_tracks %}: on
    a file with no subtitle tracks the page closed one element too many,
    the remux form escaped its grid row and stretched the width of the
    page. That grid is gone, but the branch still is — so check the thing
    that actually broke, on both sides of the condition."""

    _, library_path, page = _matroska_file_page(app, admin_client, subtitles=subtitles)
    try:
        balance = _Balance()
        balance.feed(page)
        balance.close()

        assert not balance.stray, f"closed without opening: {balance.stray}"
        assert not balance.stack, f"left open: {balance.stack}"
    finally:
        os.remove(library_path)


def test_the_page_offers_a_language_dropdown_per_track(app, admin_client):
    """#218: every audio and subtitle track gets a dropdown, showing the
    language it holds now."""

    _, library_path, page = _matroska_file_page(app, admin_client, subtitles=True)
    try:
        assert 'name="language_a1"' in page
        assert 'name="language_s1"' in page
        assert page.count("<select") == 2

        # Glenn's call (Aug 24 2026): languages read as names, not codes,
        # and (Aug 25 2026) they are picked, not typed

        assert '<option value="und" selected>Undetermined</option>' in page
        assert '<option value="eng">English</option>' in page
        assert 'list="iso-639-2-languages"' not in page, "still a text box"
    finally:
        os.remove(library_path)


def test_the_dropdown_offers_the_collection_not_the_whole_iso_table(app, admin_client):
    """All 1,006 ISO 639-2 languages is ~38 KB of options per track, and
    the library has a twenty-track disc. The list is the collection's own
    languages instead, so it stays small and still covers every track."""

    from app.tracks import iso_639_2_languages, library_language_choices

    _, library_path, page = _matroska_file_page(app, admin_client, subtitles=True)
    try:
        with app.app_context():
            choices = library_language_choices()
            assert len(choices) < len(iso_639_2_languages()) / 4

            # whatever a track already holds has to be offered, or saving
            # an untouched form would quietly change it

            codes = {code for code, name in choices}
            assert "und" in codes
            assert "eng" in codes, "the native language is always worth offering"

        assert page.count("<option") == 2 * len(choices)
    finally:
        os.remove(library_path)


class _Placement(HTMLParser):
    """Where each named control sits, and which form it submits with."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.open_tags = []
        self.controls = []
        self.form_ids = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form" and attrs.get("id"):
            self.form_ids.add(attrs["id"])
        if tag in ("input", "select") and attrs.get("name"):
            self.controls.append((attrs, tuple(self.open_tags)))
        if tag not in ("input", "br", "hr", "img", "option", "meta", "link"):
            self.open_tags.append(tag)

    def handle_endtag(self, tag):
        if tag in self.open_tags:
            while self.open_tags.pop() != tag:
                pass

    def named(self, name):
        return [(a, anc) for a, anc in self.controls if a.get("name") == name]


def test_every_track_control_lives_in_the_listing(app, admin_client):
    """Glenn's placement (Aug 24-25 2026): the language, the default and
    forced flags and the remux selection all belong on the track's own
    row, not in blocks below it. That puts them outside the forms they
    submit with, so each carries a `form` attribute naming one — drop
    that and the control silently stops being submitted."""

    _, library_path, page = _matroska_file_page(app, admin_client, subtitles=True)
    try:
        page_controls = _Placement()
        page_controls.feed(page)

        assert {"mkvpropedit-form", "mkvmerge-form"} <= page_controls.form_ids

        # name -> the form it has to reach. The property editor owns the
        # flags; the remuxer owns which tracks survive

        owners = {
            "language_a1": "mkvpropedit-form",
            "language_s1": "mkvpropedit-form",
            "default_audio": "mkvpropedit-form",
            "default_subtitle": "mkvpropedit-form",
            "forced_subtitles": "mkvpropedit-form",
            "audio_tracks": "mkvmerge-form",
            "subtitle_tracks": "mkvmerge-form",
        }
        for name, owner in owners.items():
            found = page_controls.named(name)
            assert found, f"{name} is missing from the page"
            for attrs, ancestors in found:
                assert "table" in ancestors, f"{name} left the track listing"
                assert "form" not in ancestors, f"{name} nested inside a form"
                assert attrs.get("form") == owner, f"{name} submits to the wrong form"
    finally:
        os.remove(library_path)


def test_the_remux_selection_starts_with_every_track_kept(app, admin_client):
    """The remux drops whatever isn't ticked, so an untouched form has to
    mean "change nothing"."""

    _, library_path, page = _matroska_file_page(app, admin_client, subtitles=True)
    try:
        page_controls = _Placement()
        page_controls.feed(page)

        for name in ("audio_tracks", "subtitle_tracks"):
            for attrs, _ in page_controls.named(name):
                assert "checked" in attrs, f"{name} would drop a track by default"
    finally:
        os.remove(library_path)


def test_a_file_with_no_default_subtitle_says_so(app, admin_client):
    """The subtitle default is a track OR nothing at all, and "nothing"
    has no row of its own to live on — so the listing grows one."""

    _, library_path, page = _matroska_file_page(
        app, admin_client, subtitles=True, subtitle_default=False
    )
    try:
        page_controls = _Placement()
        page_controls.feed(page)

        none_option = [
            attrs
            for attrs, _ in page_controls.named("default_subtitle")
            if attrs.get("value") == "0"
        ]
        assert none_option, "no way to say the file has no default subtitle"
        assert "checked" in none_option[0]
        assert "No default subtitle track" in page
    finally:
        os.remove(library_path)


def test_the_controls_are_disabled_when_the_file_is_not_local(app, admin_client):
    """Both forms disable themselves through their fieldsets, which can't
    reach controls living outside them — so each carries its own disabled
    attribute. Otherwise the listing offers edits the route can only
    refuse afterwards."""

    _, library_path, page = _matroska_file_page(
        app, admin_client, subtitles=True, local=False
    )
    assert library_path is None

    page_controls = _Placement()
    page_controls.feed(page)

    assert page_controls.controls
    for name in ("language_a1", "default_audio", "forced_subtitles", "audio_tracks"):
        found = page_controls.named(name)
        assert found, f"{name} is missing from the page"
        assert all("disabled" in attrs for attrs, _ in found), f"{name} stayed live"


def test_only_the_changed_languages_are_sent_to_the_edit(app, admin_client):
    """A submitted box that still holds the stored code isn't a change,
    so an untouched form must not rewrite any language."""

    import inspect

    from app.videos import mkvpropedit_task
    from tests.test_subtitle_triage import csrf_token_from

    file_id, library_path, page = _matroska_file_page(app, admin_client, subtitles=True)
    try:
        response = admin_client.post(
            f"/file/{file_id}",
            data={
                "csrf_token": csrf_token_from(page),
                "default_audio": "1",
                "default_subtitle": "1",
                "language_a1": "eng",
                "language_s1": "und",
                "mkvpropedit_submit": "Update MKV Properties",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200

        jobs = [
            job
            for job in app.file_queue.jobs
            if job.func_name == "app.videos.mkvpropedit_task"
        ]
        assert len(jobs) == 1
        inspect.signature(mkvpropedit_task).bind(*jobs[0].args)

        # The audio box moved und -> eng; the subtitle box was left alone

        assert jobs[0].args[4] == {"a1": "eng"}
    finally:
        os.remove(library_path)


def test_an_unknown_language_refuses_the_whole_edit(app, admin_client):
    """Nothing is guessed at and nothing partial is applied: a bad entry
    stops the flag edits too, so the page a visitor comes back to still
    shows what the file actually holds."""

    from tests.test_subtitle_triage import csrf_token_from

    file_id, library_path, page = _matroska_file_page(
        app, admin_client, subtitles=False
    )
    try:
        response = admin_client.post(
            f"/file/{file_id}",
            data={
                "csrf_token": csrf_token_from(page),
                "default_audio": "1",
                "default_subtitle": "0",
                "language_a1": "Gibberish",
                "mkvpropedit_submit": "Update MKV Properties",
            },
            follow_redirects=True,
        )
        assert "Unrecognized language" in response.get_data(as_text=True)

        assert not [
            job
            for job in app.file_queue.jobs
            if job.func_name == "app.videos.mkvpropedit_task"
        ]
    finally:
        os.remove(library_path)


def test_a_terminologic_code_reads_as_a_name_and_is_not_a_change(app, admin_client):
    """MediaInfo wrote "deu" into 212 of the library's files where
    mkvtoolnix's table only carries "ger". The dropdown has to show
    German, and picking that same entry back must not read as a request
    to rewrite the track — nor refuse the flag edits alongside it."""

    import inspect

    from app.videos import mkvpropedit_task
    from tests.test_subtitle_triage import csrf_token_from

    file_id, library_path, page = _matroska_file_page(
        app, admin_client, subtitles=False, language="deu"
    )
    try:
        assert '<option value="ger" selected>German</option>' in page

        response = admin_client.post(
            f"/file/{file_id}",
            data={
                "csrf_token": csrf_token_from(page),
                "default_audio": "1",
                "default_subtitle": "0",
                "language_a1": "ger",
                "mkvpropedit_submit": "Update MKV Properties",
            },
            follow_redirects=True,
        )
        assert "Unrecognized language" not in response.get_data(as_text=True)

        jobs = [
            job
            for job in app.file_queue.jobs
            if job.func_name == "app.videos.mkvpropedit_task"
        ]
        assert len(jobs) == 1
        inspect.signature(mkvpropedit_task).bind(*jobs[0].args)
        assert jobs[0].args[4] is None, "an untouched box rewrote the language"
    finally:
        os.remove(library_path)
