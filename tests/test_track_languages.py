"""Test the track language edit on the File page (#218) and its markup fix (#222).

When the headers of a disc are vague, MediaInfo falls back to und or
zxx. The language boxes are the only way to correct those values. Thus,
the value that reaches mkvpropedit must survive 3 steps. The first step
is the value that the datalist of a browser puts in the box. The second
step is the diff of the route against the stored value. The third step
is the argument that Fitzflix gives to mkvpropedit. The end-to-end test
does the last step against a real Matroska file. Without that, a
rejected --set language looks like a silent no-op.

#222 goes with this because it is the same template. A </div> outside
{% if subtitle_tracks %} closed the left column early on each file
without subtitles. Thus, the remux column escaped the grid row and
stretched to the width of the page. That grid is gone now. Each track
control is in the Tracks table. Thus, the guard is a balance check over
the whole page. That is the class of bug that it was.
"""

import os
import shutil
import subprocess

from html.parser import HTMLParser

import pytest

from tests.conftest import _TMP


@pytest.fixture(scope="module")
def undetermined_mkv(app):
    """Build a 1-second Matroska with 2 audio tracks that have no language.

    That is the state that the File page exists to correct."""

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
    """Return the language code of each audio track, read the same way as the app."""

    from app.tracks import get_audio_tracks_from_file

    with app.app_context():
        return [track["language"] for track in get_audio_tracks_from_file(path)]


def test_the_catalogue_is_the_codes_the_records_can_hold(app):
    """Make sure that each offered language is a 3-character ISO 639-2 code.

    That is the width of the column that stores the answer. The 3
    languages that this feature exists to move between are all there."""

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
    """Resolve the name and the "English (eng)" pair the same as the bare code.

    Browsers do not agree about the use of the label of a datalist option
    for matching."""

    from app.tracks import resolve_language_code

    with app.app_context():
        assert resolve_language_code("eng") == "eng"
        assert resolve_language_code(" ENG ") == "eng"
        assert resolve_language_code("English") == "eng"
        assert resolve_language_code("english") == "eng"
        assert resolve_language_code("English (eng)") == "eng"
        assert resolve_language_code("de") == "ger"

        # ISO 639-2 gives 20 languages 2 codes. mkvtoolnix lists only the
        # bibliographic code. MediaInfo writes the terminological code
        # into some track records. If Fitzflix refuses those codes, the
        # whole property-edit form locks on 212 files.

        assert resolve_language_code("deu") == "ger"
        assert resolve_language_code("fra") == "fre"
        assert resolve_language_code("German") == "ger"

        # Fitzflix does not guess. An unknown entry comes back as None.
        # Then the caller can refuse the edit instead of write a bad code.

        assert resolve_language_code("Gibberish") is None
        assert resolve_language_code("engg") is None
        assert resolve_language_code("") is None
        assert resolve_language_code(None) is None


def test_the_edit_rewrites_the_languages_in_the_file(app, undetermined_mkv):
    """Test the step that cannot be faked.

    mkvpropedit accepts the argument, and the file comes back with the
    new codes."""

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

    # Only the track that the caller asked about changes.

    assert _audio_languages(app, library_path) == ["eng", "und"]

    # The stored rows follow the file, because the edit scans the file again.

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
    """Build a Matroska file with 1 audio track and render its File page.

    The file can also have 1 subtitle track. Return the library path of
    the file only when `local`, because that is what the caller must
    clean up."""

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
    """Record the tags that close without an opening tag, and the open tags."""

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
    """Check the tag balance on both sides of the subtitle condition (#222).

    #222 was a </div> outside its {% if subtitle_tracks %}. On a file
    with no subtitle tracks, the page closed 1 element too many. The
    remux form escaped its grid row and stretched to the width of the
    page. That grid is gone, but the branch remains. Thus, check the
    thing that broke."""

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
    """Give each audio and subtitle track a dropdown that shows its language (#218)."""

    _, library_path, page = _matroska_file_page(app, admin_client, subtitles=True)
    try:
        assert 'name="language_a1"' in page
        assert 'name="language_s1"' in page
        assert page.count("<select") == 2

        # Decision by Glenn (2026-08-24): languages read as names, not
        # codes. Decision by Glenn (2026-08-25): the user selects them and
        # does not type them.

        assert '<option value="und" selected>Undetermined</option>' in page
        assert '<option value="eng">English</option>' in page
        assert 'list="iso-639-2-languages"' not in page, "still a text box"
    finally:
        os.remove(library_path)


def test_the_dropdown_offers_the_639_1_set_not_the_whole_iso_table(app, admin_client):
    """Offer only the 183 languages with a 639-1 code, plus the codes in use.

    A select repeats its options for each track. All 1,006 ISO 639-2
    languages put a megabyte of options on the Doctor Who disc with 21
    tracks. The decision by Glenn (2026-08-25): offer the 183 languages
    that also have a 639-1 code. That is a quarter of the table, and it
    covers each language that a buyer can find. Also offer each code
    that this collection already uses, because und and zxx have no 639-1
    code."""

    from app import db
    from app.models import FileAudioTrack, FileSubtitleTrack
    from app.tracks import (
        _language_table,
        iso_639_2_languages,
        library_language_choices,
        resolve_language_code,
    )

    _, library_path, page = _matroska_file_page(app, admin_client, subtitles=True)
    try:
        with app.app_context():
            choices = library_language_choices()
            codes = {code for code, name in choices}
            assert len(choices) < len(iso_639_2_languages()) / 4

            # the whole 639-1 set

            major = {
                iso_639_2
                for name, iso_639_3, iso_639_2, iso_639_1 in _language_table()
                if iso_639_1
            }
            assert major and major <= codes

            # No code that a track holds is missing. If a code is missing,
            # a save of an untouched form changes the track silently.

            assert {"und", "zxx", "eng"} <= codes
            for model in (FileAudioTrack, FileSubtitleTrack):
                stored = {
                    value for (value,) in db.session.query(model.language).distinct()
                }
                assert {resolve_language_code(v) or v for v in stored} <= codes

        assert page.count("<option") == 2 * len(choices)
    finally:
        os.remove(library_path)


class _Placement(HTMLParser):
    """Return the position of each named control and the form that it submits with."""

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
    """Put each track control on the row of its track, with a `form` attribute.

    The placement by Glenn (2026-08-24 to 2026-08-25): the language, the
    default flag, the forced flag, and the remux selection are all on the
    row of the track, not in blocks below it. That puts them outside the
    forms that they submit with. Thus, each control has a `form`
    attribute that names a form. Without that attribute, the browser
    silently stops the submission of the control."""

    _, library_path, page = _matroska_file_page(app, admin_client, subtitles=True)
    try:
        page_controls = _Placement()
        page_controls.feed(page)

        assert {"mkvpropedit-form", "mkvmerge-form"} <= page_controls.form_ids

        # Maps the name to the form that it must reach. The property
        # editor owns the flags. The remuxer owns the tracks that remain.

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
    """Make sure that an untouched remux form means "change nothing".

    The remux drops each track that is not ticked."""

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
    """Add a row for the "no default subtitle" option.

    The subtitle default is a track OR nothing. "Nothing" has no row of
    its own. Thus, the list gets 1 more row."""

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
    """Give each control outside the fieldsets its own disabled attribute.

    Both forms disable themselves through their fieldsets. A fieldset
    cannot reach a control outside it. Without the attribute, the list
    offers edits that the route can only refuse later."""

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
    """Do not rewrite a language when the submitted box holds the stored code.

    That is not a change. Thus, an untouched form must not rewrite any
    language."""

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

        # The audio box changed from und to eng. The subtitle box did not change.

        assert jobs[0].args[4] == {"a1": "eng"}
    finally:
        os.remove(library_path)


def test_an_unknown_language_refuses_the_whole_edit(app, admin_client):
    """Refuse the whole edit on a bad entry.

    Fitzflix does not guess, and it does not apply a partial edit. A bad
    entry also stops the flag edits. Thus, the page that the visitor
    comes back to still shows the real content of the file."""

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
    """Show German for "deu", and treat the same selection as no change.

    MediaInfo wrote "deu" into 212 files of the library. The table of
    mkvtoolnix has only "ger". The dropdown must show German. When the
    user selects that same entry again, Fitzflix must not read that as a
    request to rewrite the track. It must not refuse the flag edits
    with it."""

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
