"""Test the HDR to SDR conversion of imports (#292)."""

import json
import os
import shutil
import subprocess
from datetime import datetime

import pytest

import app.importing as importing
import app.videos as videos
from app import db
from app.models import (
    ArchivedOriginal,
    File,
    HeldImport,
    Movie,
    RefQuality,
    TVSeries,
    movie_file_rank,
    tv_file_rank,
)
from app.sdr_conversion import (
    converted_file_details,
    converted_quality_title,
    describe_color,
    needs_conversion,
    real_rips_blocking,
)

from tests.conftest import _TMP
from tests.factories import (
    make_file,
    make_movie,
    make_movie_file,
    make_tv_file,
    make_tv_series,
)

# Pure rules


@pytest.mark.parametrize(
    "hdr_format,primaries,transfer,expected",
    [
        ("SMPTE ST 2086, HDR10 compatible", "BT.2020", "PQ", True),
        (None, "BT.2020", "HLG", True),
        (None, "BT.2020", "BT.2020 (10-bit)", True),
        ("Dolby Vision, Version 1.0, Profile 5", None, None, True),
        (None, "BT.709", "BT.709", False),
        # A DVD is BT.601. That is SDR. It is not converted.
        (None, "BT.601 NTSC", "BT.601", False),
        (None, None, None, False),
    ],
)
def test_needs_conversion(hdr_format, primaries, transfer, expected):
    assert needs_conversion(hdr_format, primaries, transfer) is expected


def test_describe_color(app):
    with app.app_context():
        assert (
            describe_color(
                "Dolby Vision, Version 1.0, Profile 7.6, dvhe.07.06, BL+EL+RPU, "
                "Blu-ray compatible / SMPTE ST 2086, HDR10 compatible",
                "BT.2020",
                "PQ",
            )
            == "Dolby Vision Profile 7"
        )
        assert (
            describe_color("SMPTE ST 2094 App 4, HDR10+ Profile B", "BT.2020", "PQ")
            == "HDR10+"
        )
        assert describe_color(None, "BT.2020", "HLG") == "HLG"
        assert describe_color("SMPTE ST 2086", "BT.2020", "PQ") == "HDR10"
        assert describe_color(None, "BT.2020", "BT.2020 (10-bit)") == "BT.2020 SDR"


@pytest.mark.parametrize(
    "source,target",
    [
        ("Bluray-2160p", "Bluray-1080p"),
        ("Bluray-2160p Remux", "Bluray-1080p"),
        ("WEBDL-2160p", "WEBDL-1080p"),
        ("WEBRip-2160p", "WEBRip-1080p"),
        ("Bluray-1080p", "Bluray-1080p"),
        ("WEBDL-1080p", "WEBDL-1080p"),
    ],
)
def test_converted_quality_title(source, target):
    assert converted_quality_title(source) == target


def test_converted_file_details_renames_the_quality():
    details = {
        "basename": "Amadeus (1984) - [Bluray-2160p].mkv",
        "file_path": "Movies/Amadeus (1984)/Amadeus (1984) - [Bluray-2160p].mkv",
        "dirname": "Movies/Amadeus (1984)",
        "plex_title": "Amadeus (1984)",
        "quality_title": "Bluray-2160p",
    }
    plan = {
        "source_quality": "Bluray-2160p",
        "target_quality": "Bluray-1080p",
        "source_format": "Dolby Vision Profile 7",
    }
    converted = converted_file_details(details, plan)
    assert converted["basename"] == "Amadeus (1984) - [Bluray-1080p].mkv"
    assert converted["file_path"].endswith("Amadeus (1984) - [Bluray-1080p].mkv")
    assert converted["dirname"] == "Movies/Amadeus (1984)"
    assert converted["quality_title"] == "Bluray-1080p"
    assert converted["converted_from_quality"] == "Bluray-2160p"
    assert converted["converted_from_format"] == "Dolby Vision Profile 7"
    # The original details do not change.
    assert details["quality_title"] == "Bluray-2160p"


# Ranking and replacement


def _ranks(rank_function, join_model, join_column):
    ranked = (
        db.session.query(File.id, rank_function())
        .join(join_model, join_model.id == join_column)
        .join(RefQuality, RefQuality.id == File.quality_id)
        .subquery()
    )
    return {row.id: row.rank for row in db.session.query(ranked.c.id, ranked.c.rank)}


def test_a_real_rip_ranks_above_a_conversion_of_the_same_quality(app):
    with app.app_context():
        movie = make_movie("Saving Private Ryan", 1998)
        conversion = make_movie_file(
            movie, "Bluray-1080p", converted_from_quality="Bluray-2160p"
        )
        real = make_file(
            "Saving Private Ryan (1998) - [Bluray-1080p] (real).mkv",
            "Movies/Saving Private Ryan (1998)",
            "Saving Private Ryan (1998)",
            "Movies",
            "Bluray-1080p",
            movie_id=movie.id,
        )
        ranks = _ranks(movie_file_rank, Movie, File.movie_id)
        assert ranks[real.id] == 1
        assert ranks[conversion.id] == 2

        series = make_tv_series("Planet Earth II")
        tv_conversion = make_tv_file(
            series, 1, 1, "Bluray-1080p", converted_from_quality="Bluray-2160p"
        )
        tv_real = make_file(
            "Planet Earth II - S01E01 - [Bluray-1080p] (real).mkv",
            "TV Shows/Planet Earth II/Season 01",
            "Planet Earth II - S01E01",
            "TV Shows",
            "Bluray-1080p",
            series_id=series.id,
            season=1,
            episode=1,
            last_episode=1,
        )
        ranks = _ranks(tv_file_rank, TVSeries, File.series_id)
        assert ranks[tv_real.id] == 1
        assert ranks[tv_conversion.id] == 2


def _incoming(movie, quality_title, **kwargs):
    """Return a File like the one that finalize builds for an import, in the session."""

    file = make_file(
        f"{movie.title} ({movie.year}) - [{quality_title}] (incoming).mkv",
        f"Movies/{movie.title} ({movie.year})",
        f"{movie.title} ({movie.year})",
        "Movies",
        quality_title,
        movie_id=movie.id,
        title=movie.title,
        year=movie.year,
        **kwargs,
    )
    file.quality_title = quality_title
    return file


def test_a_conversion_does_not_replace_a_real_rip_of_the_same_quality(app):
    with app.app_context():
        movie = make_movie("Saving Private Ryan", 1998)
        real = make_movie_file(movie, "Bluray-1080p")
        dvd = make_movie_file(movie, "DVD")
        conversion = _incoming(
            movie, "Bluray-1080p", converted_from_quality="Bluray-2160p"
        )
        worse = conversion.find_worse_files()
        assert real not in worse
        assert dvd in worse


def test_the_import_choice_replaces_the_real_rip(app):
    with app.app_context():
        movie = make_movie("Saving Private Ryan", 1998)
        real = make_movie_file(movie, "Bluray-1080p Remux")
        conversion = _incoming(
            movie, "Bluray-1080p", converted_from_quality="Bluray-2160p"
        )
        conversion.replace_existing = True
        assert conversion.find_better_files() == []
        assert real in conversion.find_worse_files()


def test_a_conversion_replaces_an_hdr_file_of_a_higher_quality(app):
    with app.app_context():
        movie = make_movie("Evangelion 3.0+1.0 Thrice Upon a Time", 2021)
        original = make_movie_file(
            movie,
            "Bluray-2160p",
            color_primaries="BT.2020",
            transfer_characteristics="BT.2020 (10-bit)",
        )
        conversion = _incoming(
            movie, "Bluray-1080p", converted_from_quality="Bluray-2160p"
        )
        assert conversion.find_better_files() == []
        assert original in conversion.find_worse_files()


def test_an_sdr_4k_file_still_blocks_a_conversion(app):
    with app.app_context():
        movie = make_movie("Star Wars", 1977)
        sdr_4k = make_movie_file(movie, "WEBDL-2160p", color_primaries="BT.709")
        conversion = _incoming(
            movie, "WEBDL-1080p", converted_from_quality="WEBDL-2160p"
        )
        assert conversion.find_better_files() == [sdr_4k]


def test_a_real_rip_replaces_a_conversion(app):
    with app.app_context():
        movie = make_movie("Saving Private Ryan", 1998)
        conversion = make_movie_file(
            movie, "Bluray-1080p", converted_from_quality="Bluray-2160p"
        )
        real = _incoming(movie, "Bluray-1080p")
        assert real.find_better_files() == []
        assert conversion in real.find_worse_files()


def test_a_later_conversion_replaces_an_earlier_conversion(app):
    with app.app_context():
        movie = make_movie("Amadeus", 1984)
        earlier = make_movie_file(
            movie, "WEBDL-1080p", converted_from_quality="WEBDL-2160p"
        )
        later = _incoming(movie, "Bluray-1080p", converted_from_quality="Bluray-2160p")
        assert later.find_better_files() == []
        assert earlier in later.find_worse_files()


def test_real_rips_blocking(app):
    with app.app_context():
        movie = make_movie("Saving Private Ryan", 1998)
        real = make_movie_file(movie, "Bluray-1080p")
        make_movie_file(movie, "DVD")
        details = {
            "media_library": "Movies",
            "title": "Saving Private Ryan",
            "year": 1998,
            "plex_title": "Saving Private Ryan (1998)",
            "edition": None,
            "dirname": "Movies/Saving Private Ryan (1998)",
            "quality_title": "Bluray-1080p",
        }
        assert real_rips_blocking(details) == [real]

        # A conversion of the title does not block. Nor does a lower quality.
        real.converted_from_quality = "Bluray-2160p"
        db.session.flush()
        assert real_rips_blocking(details) == []


def test_archive_only_originals_claim_their_keys(app):
    from app.aws_storage import untouched_key_still_claimed

    with app.app_context():
        assert not untouched_key_still_claimed("untouched/Amadeus.mkv")
        db.session.add(
            ArchivedOriginal(
                media_library="Movies",
                quality_title="Bluray-2160p",
                untouched_basename="Amadeus (1984) - [Bluray-2160p].mkv",
                aws_untouched_key="untouched/Amadeus.mkv",
                reason="archive only",
            )
        )
        db.session.flush()
        assert untouched_key_still_claimed("untouched/Amadeus.mkv")


def test_the_library_file_of_a_conversion_never_replaces_its_archive(app):
    from app.aws_storage import upload_task

    with app.app_context():
        movie = make_movie("Amadeus", 1984)
        conversion = make_movie_file(
            movie,
            "Bluray-1080p",
            converted_from_quality="Bluray-2160p",
            aws_untouched_key="untouched/Amadeus (1984) - [Bluray-2160p].mkv",
        )
        db.session.commit()
        assert conversion.archive_is_library_copy is False
        assert upload_task(conversion.id, "untouched", True) is False
        db.session.refresh(conversion)
        assert conversion.aws_untouched_key == (
            "untouched/Amadeus (1984) - [Bluray-2160p].mkv"
        )


# The import chain, with the real tools


@pytest.fixture(scope="module")
def hdr10_mkv(app):
    """Build a 1-second HDR10 Matroska file with an English audio track."""

    path = os.path.join(_TMP, "sample-hdr10.mkv")
    if not os.path.exists(path):
        subprocess.run(
            [
                app.config["FFMPEG_BIN"],
                "-f",
                "lavfi",
                "-i",
                "testsrc=duration=1:size=640x360:rate=24",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=1",
                "-c:v",
                "libx265",
                "-pix_fmt",
                "yuv420p10le",
                "-x265-params",
                "hdr10=1:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc"
                ":max-cll=1000,400:log-level=error",
                "-c:a",
                "aac",
                "-metadata:s:a:0",
                "language=eng",
                "-shortest",
                "-y",
                path,
            ],
            check=True,
            capture_output=True,
        )
    return path


@pytest.fixture(autouse=True)
def preset_file(app, monkeypatch):
    """Use the preset file of the repository, as production does."""

    path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "handbrake_presets.json"
    )
    monkeypatch.setitem(app.config, "HANDBRAKE_PRESET_FILE", path)


@pytest.fixture
def archive_on(monkeypatch):
    """Turn the archive on, with a fake upload that returns the key of the file."""

    uploads = []
    real_get_setting = importing.get_setting

    def get_setting(name, *args, **kwargs):
        if name in ("ARCHIVE_ORIGINAL_MEDIA", "CONVERT_HDR_TO_SDR"):
            return True
        return real_get_setting(name, *args, **kwargs)

    def fake_upload(path, prefix="", **kwargs):
        uploads.append(path)
        key = f"{prefix}/{os.path.basename(path)}"
        return key, datetime.utcnow(), os.path.getsize(path)

    monkeypatch.setattr(importing, "get_setting", get_setting)
    monkeypatch.setattr(importing, "aws_upload", fake_upload)
    return uploads


def _place(directory, sample, basename):
    """Copy a sample into an incoming directory that the watchdog does not see."""

    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, basename)
    shutil.copy(sample, path)
    stamp = os.path.getmtime(path) - 3600
    os.utime(path, (stamp, stamp))
    return path


def _jobs(queue, name):
    return [job for job in queue.jobs if job.func_name == name]


def test_an_hdr_import_becomes_a_1080p_sdr_conversion(
    app, hdr10_mkv, incoming_dir, archive_on
):
    basename = "HDR Test (2020) - [Bluray-2160p].mkv"
    with app.app_context():
        source = _place(incoming_dir, hdr10_mkv, basename)
        assert importing.localization_task(source) is True

        # The conversion goes to the transcode queue, not straight to the move.
        assert _jobs(app.handoff_queue, "app.videos.move_localized_file") == []
        (convert_job,) = _jobs(app.transcode_queue, "app.videos.convert_localized_file")
        details = convert_job.args[1]
        assert details["basename"] == "HDR Test (2020) - [Bluray-1080p].mkv"
        assert details["untouched_basename"] == basename
        assert details["converted_from_format"] == "HDR10"

        # The archive is the untouched original.
        assert [os.path.basename(path) for path in archive_on] == [basename]

        assert videos.convert_localized_file(*convert_job.args) is True
        (move_job,) = _jobs(app.handoff_queue, "app.videos.move_localized_file")
        assert videos.move_localized_file(*move_job.args) is True
        (finalize_job,) = _jobs(app.sql_queue, "app.videos.finalize_localization")
        videos.finalize_localization(*finalize_job.args)

        file = File.query.filter_by(basename=details["basename"]).one()
        assert file.quality.quality_title == "Bluray-1080p"
        assert file.converted_from_quality == "Bluray-2160p"
        assert file.converted_from_format == "HDR10"
        assert file.color_primaries == "BT.709"
        assert file.transfer_characteristics == "BT.709"
        assert file.hdr_format is None
        assert file.aws_untouched_key == f"untouched/{basename}"
        assert [t.language for t in file.audiotrack] == ["eng"]

        output = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
        assert os.path.exists(output)
        assert not os.path.exists(source)
        assert os.listdir(app.config["STAGING_DIR"]) == []
        os.remove(output)


def test_an_hdr_import_waits_when_a_real_rip_exists(
    app, hdr10_mkv, incoming_dir, archive_on, monkeypatch
):
    import app.email as email_module

    sent = []
    monkeypatch.setattr(email_module, "send_email", lambda *a, **k: sent.append(k))
    monkeypatch.setitem(app.config, "SERVER_NAME", "fitzflix.example")
    basename = "Held Test (2020) - [Bluray-2160p].mkv"
    with app.app_context():
        movie = make_movie("Held Test", 2020)
        real = make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        source = _place(incoming_dir, hdr10_mkv, basename)

        assert importing.localization_task(source) is False
        held_path = os.path.join(app.config["HELD_DIR"], basename)
        assert os.path.exists(held_path)
        assert not os.path.exists(source)
        assert _jobs(app.transcode_queue, "app.videos.convert_localized_file") == []
        assert archive_on == []

        held = HeldImport.query.one()
        assert held.held_path == held_path
        assert held.existing_file_id == real.id
        assert held.source_format == "HDR10"
        assert held.details()["quality_title"] == "Bluray-1080p"

        # The email links to the decision page.
        (email,) = sent
        link = "http://fitzflix.example/maintenance/held-imports"
        assert link in email["text_body"]
        assert f'href="{link}"' in email["html_body"]

        # The "Import" choice sends the file through with replace_existing.
        held.decision = "import"
        db.session.commit()
        assert importing.localization_task(held_path) is True
        (convert_job,) = _jobs(app.transcode_queue, "app.videos.convert_localized_file")
        assert convert_job.args[1]["replace_existing"] is True

        for job in app.transcode_queue.jobs:
            job.delete()
        for path in os.listdir(app.config["STAGING_DIR"]):
            os.remove(os.path.join(app.config["STAGING_DIR"], path))
        os.remove(held_path)


def test_a_conversion_needs_the_archive(app, hdr10_mkv, incoming_dir, monkeypatch):
    basename = "No Archive (2020) - [Bluray-2160p].mkv"
    real_get_setting = importing.get_setting
    monkeypatch.setattr(
        importing,
        "get_setting",
        lambda name, *a, **k: (
            False
            if name == "ARCHIVE_ORIGINAL_MEDIA"
            else (
                True
                if name == "CONVERT_HDR_TO_SDR"
                else real_get_setting(name, *a, **k)
            )
        ),
    )
    with app.app_context():
        source = _place(incoming_dir, hdr10_mkv, basename)
        assert importing.localization_task(source) is False
        assert not os.path.exists(source)
        assert _jobs(app.transcode_queue, "app.videos.convert_localized_file") == []
        rejected = os.path.join(
            app.config["REJECTS_DIR"], "conversion not possible", basename
        )
        if os.path.exists(rejected):
            os.remove(rejected)


def test_the_flag_off_imports_hdr_as_it_is(app, hdr10_mkv, incoming_dir, monkeypatch):
    basename = "Flag Off (2020) - [Bluray-2160p].mkv"
    real_get_setting = importing.get_setting
    monkeypatch.setattr(
        importing,
        "get_setting",
        lambda name, *a, **k: (
            False if name == "CONVERT_HDR_TO_SDR" else real_get_setting(name, *a, **k)
        ),
    )
    with app.app_context():
        source = _place(incoming_dir, hdr10_mkv, basename)
        assert importing.localization_task(source) is True
        (move_job,) = _jobs(app.handoff_queue, "app.videos.move_localized_file")
        assert move_job.args[1]["basename"] == basename
        assert "converted_from_quality" not in move_job.args[1]
        move_job.delete()
        os.remove(move_job.args[3])
        os.remove(source)


def test_a_real_rip_keeps_the_original_of_the_conversion(app, monkeypatch):
    """A real rip reuses the record of a conversion with the same path.

    The original of the conversion becomes an archive-only original. Its
    S3 key is not deleted."""

    with app.app_context():
        movie = make_movie("Kept Original", 1998)
        conversion = make_movie_file(
            movie,
            "Bluray-1080p",
            converted_from_quality="Bluray-2160p",
            converted_from_format="Dolby Vision Profile 7",
            untouched_basename="Kept Original (1998) - [Bluray-2160p].mkv",
            aws_untouched_key="untouched/Kept Original (1998) - [Bluray-2160p].mkv",
            aws_untouched_date_uploaded=datetime.utcnow(),
        )
        db.session.commit()

        output_dir = os.path.join(app.config["LIBRARY_DIR"], conversion.dirname)
        os.makedirs(output_dir, exist_ok=True)
        hidden = os.path.join(output_dir, f".{conversion.basename}")
        with open(hidden, "wb") as f:
            f.write(b"real rip")
        source = os.path.join(_TMP, "incoming", conversion.basename)
        os.makedirs(os.path.dirname(source), exist_ok=True)
        with open(source, "wb") as f:
            f.write(b"real rip")

        details = {
            "media_library": "Movies",
            "file_path": conversion.file_path,
            "dirname": conversion.dirname,
            "basename": conversion.basename,
            "plex_title": conversion.plex_title,
            "title": "Kept Original",
            "year": 1998,
            "edition": None,
            "quality_title": "Bluray-1080p",
            "fullscreen": False,
            "extension": "mkv",
            "untouched_basename": conversion.basename,
            "aws_untouched_key": f"untouched/{conversion.basename}",
            "aws_untouched_date_uploaded": datetime.utcnow(),
            "aws_untouched_filesize_bytes": 8,
        }
        inspection = {
            "video": {"format": "AVC", "color_primaries": "BT.709"},
            "audio_tracks": [],
            "subtitle_tracks": [],
            "filesize_bytes": 8,
        }
        lock = app.lock_manager.lock(json.dumps({"title": "Kept Original"}), 10000)
        videos.finalize_localization(source, details, lock, hidden, inspection)

        db.session.expire_all()
        file = File.query.filter_by(file_path=conversion.file_path).one()
        assert file.converted_from_quality is None
        assert file.aws_untouched_key == f"untouched/{conversion.basename}"

        archived = ArchivedOriginal.query.one()
        assert archived.aws_untouched_key == (
            "untouched/Kept Original (1998) - [Bluray-2160p].mkv"
        )
        assert archived.quality_title == "Bluray-2160p"
        assert archived.hdr_format == "Dolby Vision Profile 7"
        assert archived.movie_id == movie.id
        assert archived.reason == "replaced by a real rip"

        deletes = _jobs(app.handoff_queue, "app.videos.aws_delete")
        assert [job.args[0] for job in deletes] == []
        os.remove(os.path.join(app.config["LIBRARY_DIR"], file.file_path))


def test_archive_only_uploads_the_held_file(app, monkeypatch):
    import app.aws_storage as aws_storage
    from app.sdr_conversion import archive_held_task

    with app.app_context():
        movie = make_movie("Archive Only", 1998)
        db.session.commit()
        held_path = os.path.join(
            app.config["HELD_DIR"], "Archive Only (1998) - [Bluray-2160p].mkv"
        )
        with open(held_path, "wb") as f:
            f.write(b"4k original")
        details = {
            "media_library": "Movies",
            "title": "Archive Only",
            "year": 1998,
            "plex_title": "Archive Only (1998)",
            "edition": None,
            "quality_title": "Bluray-1080p",
            "converted_from_quality": "Bluray-2160p",
        }
        held = HeldImport(
            held_path=held_path,
            untouched_basename=os.path.basename(held_path),
            file_details=json.dumps(details),
            source_format="HDR10",
            decision="archive",
        )
        db.session.add(held)
        db.session.commit()

        monkeypatch.setattr(
            aws_storage,
            "aws_upload",
            lambda path, prefix="", **kw: (
                f"{prefix}/{os.path.basename(path)}",
                datetime.utcnow(),
                os.path.getsize(path),
            ),
        )
        assert archive_held_task(held.id) is True
        assert not os.path.exists(held_path)
        assert HeldImport.query.count() == 0
        archived = ArchivedOriginal.query.one()
        assert archived.movie_id == movie.id
        assert archived.quality_title == "Bluray-2160p"
        assert archived.hdr_format == "HDR10"
        assert archived.reason == "archive only"


# The Held imports page


def _held_row(app, basename, decision=None):
    path = os.path.join(app.config["HELD_DIR"], basename)
    with open(path, "wb") as f:
        f.write(b"4k original")
    held = HeldImport(
        held_path=path,
        untouched_basename=basename,
        file_details=json.dumps({"quality_title": "Bluray-1080p"}),
        source_format="Dolby Vision Profile 7",
        filesize_bytes=11,
        decision=decision,
    )
    db.session.add(held)
    db.session.commit()
    return held.id, path


def test_the_page_lists_held_imports_and_archived_originals(app, admin_client):
    with app.app_context():
        _held_row(app, "Page Test (1998) - [Bluray-2160p].mkv")
        db.session.add(
            ArchivedOriginal(
                media_library="Movies",
                quality_title="Bluray-2160p",
                untouched_basename="Archived Test (1984) - [Bluray-2160p].mkv",
                aws_untouched_key="untouched/Archived Test (1984) - [Bluray-2160p].mkv",
                reason="archive only",
            )
        )
        db.session.commit()

    body = admin_client.get("/maintenance/held-imports").get_data(as_text=True)
    assert "Page Test (1998) - [Bluray-2160p].mkv" in body
    assert "Dolby Vision Profile 7" in body
    assert "to Bluray-1080p SDR" in body
    assert "Archived Test (1984) - [Bluray-2160p].mkv" in body

    maintenance = admin_client.get("/maintenance").get_data(as_text=True)
    assert "Decide held imports" in maintenance


def test_a_held_row_with_no_file_goes_away(app, admin_client):
    with app.app_context():
        _, path = _held_row(app, "Gone Test (1998) - [Bluray-2160p].mkv")
        os.remove(path)
    body = admin_client.get("/maintenance/held-imports").get_data(as_text=True)
    assert "Gone Test" not in body
    with app.app_context():
        assert HeldImport.query.count() == 0


def _decide(admin_client, held_id, button):
    from tests.test_rejects import csrf_token_from

    page = admin_client.get("/maintenance/held-imports").get_data(as_text=True)
    return admin_client.post(
        "/maintenance/held-imports",
        data={
            "csrf_token": csrf_token_from(page),
            "held_id": str(held_id),
            button: "x",
        },
        follow_redirects=True,
    ).get_data(as_text=True)


def test_import_sends_the_held_file_back_through_the_import(app, admin_client):
    with app.app_context():
        held_id, path = _held_row(app, "Import Test (1998) - [Bluray-2160p].mkv")
    body = _decide(admin_client, held_id, "import_submit")
    assert "Import in progress" in body
    with app.app_context():
        assert db.session.get(HeldImport, held_id).decision == "import"
        jobs = _jobs(app.import_queue, "app.videos.localization_task")
        assert [
            job.kwargs.get("file_path", job.args[0] if job.args else None)
            for job in jobs
        ] == [path]
    os.remove(path)


def test_archive_only_queues_the_upload(app, admin_client):
    with app.app_context():
        held_id, path = _held_row(app, "Archive Test (1998) - [Bluray-2160p].mkv")
    body = _decide(admin_client, held_id, "archive_submit")
    assert "Archive in progress" in body
    with app.app_context():
        (job,) = _jobs(app.file_queue, "app.videos.archive_held_task")
        assert job.args == (held_id,)
    os.remove(path)


def test_discard_moves_the_held_file_to_rejects(app, admin_client):
    basename = "Discard Test (1998) - [Bluray-2160p].mkv"
    with app.app_context():
        held_id, path = _held_row(app, basename)
    _decide(admin_client, held_id, "discard_submit")
    assert not os.path.exists(path)
    rejected = os.path.join(app.config["REJECTS_DIR"], "discarded", basename)
    assert os.path.exists(rejected)
    os.remove(rejected)
    with app.app_context():
        assert HeldImport.query.count() == 0


def test_a_restore_needs_the_password(app, admin_client):
    from tests.conftest import ADMIN_PASSWORD
    from tests.test_rejects import csrf_token_from

    with app.app_context():
        archived = ArchivedOriginal(
            media_library="Movies",
            quality_title="Bluray-2160p",
            untouched_basename="Restore Test (1984) - [Bluray-2160p].mkv",
            aws_untouched_key="untouched/Restore Test (1984) - [Bluray-2160p].mkv",
            reason="archive only",
        )
        db.session.add(archived)
        db.session.commit()
        archived_id = archived.id

    def restore(password):
        page = admin_client.get("/maintenance/held-imports").get_data(as_text=True)
        return admin_client.post(
            "/maintenance/held-imports",
            data={
                "csrf_token": csrf_token_from(page),
                "archived_id": str(archived_id),
                "password": password,
                "restore_submit": "Restore",
            },
            follow_redirects=True,
        ).get_data(as_text=True)

    assert "Incorrect password" in restore("wrong")
    with app.app_context():
        assert _jobs(app.request_queue, "app.videos.aws_restore") == []
    assert "Requesting a restore" in restore(ADMIN_PASSWORD)
    with app.app_context():
        (job,) = _jobs(app.request_queue, "app.videos.aws_restore")
        assert job.args == ("untouched/Restore Test (1984) - [Bluray-2160p].mkv",)
