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
stretched the width of the page.
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


def _matroska_file_page(app, admin_client, *, subtitles, local=True):
    """A Matroska file with one audio track (and optionally one subtitle
    track), and its rendered File page. Returns the file's library path
    only when `local`, since that is what the caller has to clean up."""

    from app import db
    from app.models import FileAudioTrack, FileSubtitleTrack
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        movie = make_movie("Page Markup", 2022 if subtitles else 2023)
        file = make_movie_file(movie, "Bluray-1080p", container="Matroska")
        db.session.flush()
        db.session.add(
            FileAudioTrack(
                file_id=file.id,
                track=1,
                language="und",
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
                    default=True,
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


class _DivNesting(HTMLParser):
    """Records the ancestor div classes in force at each div open."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.opened = []

    def handle_starttag(self, tag, attrs):
        if tag != "div":
            return
        classes = dict(attrs).get("class", "")
        self.opened.append((classes, tuple(self.stack)))
        self.stack.append(classes)

    def handle_endtag(self, tag):
        if tag == "div" and self.stack:
            self.stack.pop()

    def ancestors_of(self, classes):
        """The ancestor chains of every div opened with exactly `classes`."""

        return [chain for opened, chain in self.opened if opened == classes]


@pytest.mark.parametrize("subtitles", [False, True])
def test_the_remux_column_stays_inside_its_grid_row(app, admin_client, subtitles):
    """#222: the two MKV forms are grid columns of one row. A file with
    no subtitle tracks used to close the left column an element early,
    which pushed the remux column out of the row and let it stretch the
    full width of the page — so the with-subtitles case is the control."""

    _, library_path, page = _matroska_file_page(app, admin_client, subtitles=subtitles)
    try:
        nesting = _DivNesting()
        nesting.feed(page)

        chains = nesting.ancestors_of("col-md-4")
        assert chains, "the remux column is missing from the page"
        assert all(
            "row align-items-end" in chain for chain in chains
        ), "the remux column escaped its row and will stretch the page width"
    finally:
        os.remove(library_path)


def test_the_page_offers_a_language_box_per_track(app, admin_client):
    """#218: every audio and subtitle track gets a box, prefilled with
    what is stored, backed by one shared language list."""

    _, library_path, page = _matroska_file_page(app, admin_client, subtitles=True)
    try:
        assert 'name="language_a1"' in page
        assert 'name="language_s1"' in page
        assert page.count('list="iso-639-2-languages"') == 2
        assert page.count('<datalist id="iso-639-2-languages">') == 1
        assert '<option value="eng" label="English">' in page
    finally:
        os.remove(library_path)


class _BoxPlacement(HTMLParser):
    """Where the language boxes sit, and what form they submit with."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.open_tags = []
        self.boxes = []
        self.form_ids = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form" and attrs.get("id"):
            self.form_ids.add(attrs["id"])
        if tag == "input" and attrs.get("name", "").startswith("language_"):
            self.boxes.append((attrs, tuple(self.open_tags)))
        if tag not in ("input", "br", "hr", "img", "option"):
            self.open_tags.append(tag)

    def handle_endtag(self, tag):
        if tag in self.open_tags:
            while self.open_tags.pop() != tag:
                pass


def test_the_boxes_edit_in_the_track_listing_and_save_with_the_edit_form(
    app, admin_client
):
    """Glenn's placement (Aug 24 2026): the boxes belong inline in the
    Tracks table, not in a block of their own. That puts them outside the
    form they submit with, so each one carries a `form` attribute naming
    it — drop that and the edits silently never arrive."""

    _, library_path, page = _matroska_file_page(app, admin_client, subtitles=True)
    try:
        placement = _BoxPlacement()
        placement.feed(page)

        assert len(placement.boxes) == 2
        assert "mkvpropedit-form" in placement.form_ids

        for attrs, ancestors in placement.boxes:
            assert "table" in ancestors, "the box left the track listing"
            assert "form" not in ancestors, "a form nested inside the table"
            assert attrs.get("form") == "mkvpropedit-form"
    finally:
        os.remove(library_path)


def test_the_boxes_are_disabled_when_the_file_is_not_local(app, admin_client):
    """The property-edit form disables itself through its fieldset, which
    can't reach boxes living outside it — so they carry their own disabled
    attribute. Otherwise the listing offers an edit the route can only
    refuse afterwards."""

    _, library_path, page = _matroska_file_page(
        app, admin_client, subtitles=True, local=False
    )
    assert library_path is None

    placement = _BoxPlacement()
    placement.feed(page)

    assert len(placement.boxes) == 2
    assert all("disabled" in attrs for attrs, ancestors in placement.boxes)


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
                "language_a1": "English",
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
