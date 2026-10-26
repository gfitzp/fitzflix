"""Test the HDR to SDR conversion of imports (#292)."""

import json
import os
import shutil
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
from app.file_ranking import find_better_files, find_worse_files, real_rips_blocking
from app.sdr_conversion import (
    converted_file_details,
    converted_quality_title,
    describe_color,
    needs_conversion,
)

from tests.conftest import _TMP
from tests.media import make_sample_mkv
from tests.test_scheduling import scheduled_jobs
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
    """Return (File, details) like the ones of an import, with the File in the session.

    details is the dict that evaluate_filename gives. find_better_files
    reads it. The File is the row that finalize builds. find_worse_files
    reads it."""

    file = make_file(
        f"{movie.title} ({movie.year}) - [{quality_title}] (incoming).mkv",
        f"Movies/{movie.title} ({movie.year})",
        f"{movie.title} ({movie.year})",
        "Movies",
        quality_title,
        movie_id=movie.id,
        **kwargs,
    )
    details = {
        "media_library": "Movies",
        "dirname": file.dirname,
        "plex_title": file.plex_title,
        "edition": None,
        "fullscreen": False,
        "title": movie.title,
        "year": movie.year,
        "quality_title": quality_title,
        **kwargs,
    }
    return file, details


def test_a_conversion_does_not_replace_a_real_rip_of_the_same_quality(app):
    with app.app_context():
        movie = make_movie("Saving Private Ryan", 1998)
        real = make_movie_file(movie, "Bluray-1080p")
        dvd = make_movie_file(movie, "DVD")
        conversion, _ = _incoming(
            movie, "Bluray-1080p", converted_from_quality="Bluray-2160p"
        )
        worse = find_worse_files(conversion)
        assert real not in worse
        assert dvd in worse


def test_the_import_choice_replaces_the_real_rip(app):
    with app.app_context():
        movie = make_movie("Saving Private Ryan", 1998)
        real = make_movie_file(movie, "Bluray-1080p Remux")
        conversion, details = _incoming(
            movie, "Bluray-1080p", converted_from_quality="Bluray-2160p"
        )
        details["replace_existing"] = True
        assert find_better_files(details) == []
        assert real in find_worse_files(conversion, replace_existing=True)


def test_a_conversion_replaces_an_hdr_file_of_a_higher_quality(app):
    with app.app_context():
        movie = make_movie("Evangelion 3.0+1.0 Thrice Upon a Time", 2021)
        original = make_movie_file(
            movie,
            "Bluray-2160p",
            color_primaries="BT.2020",
            transfer_characteristics="BT.2020 (10-bit)",
        )
        conversion, details = _incoming(
            movie, "Bluray-1080p", converted_from_quality="Bluray-2160p"
        )
        assert find_better_files(details) == []
        assert original in find_worse_files(conversion)


def test_an_sdr_4k_file_still_blocks_a_conversion(app):
    with app.app_context():
        movie = make_movie("Star Wars", 1977)
        sdr_4k = make_movie_file(movie, "WEBDL-2160p", color_primaries="BT.709")
        _, details = _incoming(
            movie, "WEBDL-1080p", converted_from_quality="WEBDL-2160p"
        )
        assert find_better_files(details) == [sdr_4k]


def test_a_real_rip_replaces_a_conversion(app):
    with app.app_context():
        movie = make_movie("Saving Private Ryan", 1998)
        conversion = make_movie_file(
            movie, "Bluray-1080p", converted_from_quality="Bluray-2160p"
        )
        real, details = _incoming(movie, "Bluray-1080p")
        assert find_better_files(details) == []
        assert conversion in find_worse_files(real)


def test_a_later_conversion_replaces_an_earlier_conversion(app):
    with app.app_context():
        movie = make_movie("Amadeus", 1984)
        earlier = make_movie_file(
            movie, "WEBDL-1080p", converted_from_quality="WEBDL-2160p"
        )
        later, details = _incoming(
            movie, "Bluray-1080p", converted_from_quality="Bluray-2160p"
        )
        assert find_better_files(details) == []
        assert earlier in find_worse_files(later)


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
                aws_key="untouched/Amadeus.mkv",
                reason="archive only",
            )
        )
        db.session.flush()
        assert untouched_key_still_claimed("untouched/Amadeus.mkv")


def test_the_library_file_of_a_conversion_goes_to_transcoded(app, monkeypatch):
    """The upload of a conversion archives its 1080p file in transcoded/ (#299).

    The original in untouched/ never changes."""

    import app.aws_storage as aws_storage

    uploads = []

    def fake_upload(path, key_prefix="", key_name=None, **kwargs):
        uploads.append((key_prefix, key_name))
        return f"{key_prefix}/{key_name}", datetime.utcnow(), 5

    monkeypatch.setattr(aws_storage, "aws_upload", fake_upload)
    with app.app_context():
        movie = make_movie("Amadeus", 1984)
        conversion = make_movie_file(
            movie,
            "Bluray-1080p",
            converted_from_quality="Bluray-2160p",
            aws_key="untouched/Amadeus (1984) - [Bluray-2160p].mkv",
        )
        db.session.commit()
        assert aws_storage.upload_task(conversion.id) is True
        db.session.expire_all()
        conversion = db.session.get(File, conversion.id)
        assert conversion.aws_key == ("untouched/Amadeus (1984) - [Bluray-2160p].mkv")
        assert conversion.aws_transcoded_key == (
            "transcoded/Amadeus (1984) - [Bluray-1080p].mkv"
        )
        assert conversion.restore_key == conversion.aws_transcoded_key
        assert uploads == [("transcoded", "Amadeus (1984) - [Bluray-1080p].mkv")]


# The import chain, with the real tools


@pytest.fixture(scope="session")
def hdr10_mkv(app):
    """Build a 1-second HDR10 Matroska file with an English audio track."""

    return make_sample_mkv(
        os.path.join(_TMP, "sample-hdr10.mkv"), video="hdr10", size="640x360"
    )


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

    import app.aws_storage as aws_storage

    monkeypatch.setattr(importing, "get_setting", get_setting)
    monkeypatch.setattr(aws_storage, "aws_upload", fake_upload)
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

        # The reservation covers the localized file and the conversion (#294).
        from app.importing import STAGING_RESERVED_KEY

        hidden = convert_job.args[3]
        entry = json.loads(app.redis.hget(STAGING_RESERVED_KEY, basename))
        assert entry["bytes"] == int(os.path.getsize(hidden) * 1.8)

        assert videos.convert_localized_file(*convert_job.args) is True
        entry = json.loads(app.redis.hget(STAGING_RESERVED_KEY, basename))
        assert entry["bytes"] == os.path.getsize(hidden)
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
        assert file.aws_key == f"untouched/{basename}"
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
            aws_key="untouched/Kept Original (1998) - [Bluray-2160p].mkv",
            aws_date_uploaded=datetime.utcnow(),
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
            "aws_key": f"untouched/{conversion.basename}",
            "aws_date_uploaded": datetime.utcnow(),
            "aws_filesize_bytes": 8,
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
        assert file.aws_key == f"untouched/{conversion.basename}"

        archived = ArchivedOriginal.query.one()
        assert archived.aws_key == (
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
                aws_key="untouched/Archived Test (1984) - [Bluray-2160p].mkv",
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
    from tests.conftest import csrf_token_from

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
    from tests.conftest import csrf_token_from

    with app.app_context():
        archived = ArchivedOriginal(
            media_library="Movies",
            quality_title="Bluray-2160p",
            untouched_basename="Restore Test (1984) - [Bluray-2160p].mkv",
            aws_key="untouched/Restore Test (1984) - [Bluray-2160p].mkv",
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


def test_the_untouched_name_of_a_conversion_keeps_the_original_quality(app):
    """A TMDB rename must not move the archive of a conversion to a 1080p key."""

    from app.importing import reconstruct_filename

    with app.app_context():
        movie = make_movie("Renamed Original", 1941)
        conversion = make_movie_file(
            movie,
            "Bluray-1080p",
            converted_from_quality="Bluray-2160p",
            untouched_basename="Renamed Original (1941) - [Bluray-2160p].mkv",
        )
        real = make_file(
            "Renamed Original (1941) - [DVD].mkv",
            "Movies/Renamed Original (1941)",
            "Renamed Original (1941)",
            "Movies",
            "DVD",
            movie_id=movie.id,
            untouched_basename="Renamed Original (1941) - [DVD].mkv",
        )
        db.session.commit()
        assert reconstruct_filename(conversion.id) == (
            "Renamed Original (1941) - [Bluray-2160p].mkv"
        )
        assert reconstruct_filename(real.id) == "Renamed Original (1941) - [DVD].mkv"


def test_a_tmdb_rename_keeps_the_library_name_of_a_conversion(app):
    """The library file of a conversion keeps its 1080p tag and its extension.

    On 2026-10-24 a TMDB refresh renamed the 1080p conversion of The
    Maltese Falcon to the 2160p name of its original."""

    from app.importing import evaluate_filename, reconstruct_filename
    from app.tmdb_refresh import library_name_details

    with app.app_context():
        movie = make_movie("Renamed Library", 1941)
        conversion = make_movie_file(
            movie,
            "Bluray-1080p",
            converted_from_quality="Bluray-2160p",
            untouched_basename="Renamed Library (1941) - [Bluray-2160p].m2ts",
        )
        db.session.commit()
        conversion.untouched_basename = reconstruct_filename(conversion.id)
        details = library_name_details(
            conversion, evaluate_filename(conversion.untouched_basename)
        )
        assert details["basename"] == "Renamed Library (1941) - [Bluray-1080p].mkv"
        assert details["file_path"] == (
            "Movies/Renamed Library (1941)/Renamed Library (1941) - [Bluray-1080p].mkv"
        )
        assert details["extension"] == "mkv"


def test_a_conversion_that_reuses_a_record_takes_the_untouched_name(app):
    """The Import choice reuses the record of the real rip at the same path."""

    with app.app_context():
        movie = make_movie("Reused Record", 1984)
        real = make_movie_file(
            movie,
            "Bluray-1080p",
            untouched_basename="Reused Record (1984) - [Bluray-1080p].mkv",
            aws_key="untouched/Reused Record (1984) - [Bluray-1080p].mkv",
            aws_date_uploaded=datetime.utcnow(),
        )
        db.session.commit()

        output_dir = os.path.join(app.config["LIBRARY_DIR"], real.dirname)
        os.makedirs(output_dir, exist_ok=True)
        hidden = os.path.join(output_dir, f".{real.basename}")
        with open(hidden, "wb") as f:
            f.write(b"conversion")
        source = os.path.join(
            _TMP, "incoming", "Reused Record (1984) - [Bluray-2160p].mkv"
        )
        os.makedirs(os.path.dirname(source), exist_ok=True)
        with open(source, "wb") as f:
            f.write(b"4k original")

        details = {
            "media_library": "Movies",
            "file_path": real.file_path,
            "dirname": real.dirname,
            "basename": real.basename,
            "plex_title": real.plex_title,
            "title": "Reused Record",
            "year": 1984,
            "edition": None,
            "quality_title": "Bluray-1080p",
            "fullscreen": False,
            "extension": "mkv",
            "converted_from_quality": "Bluray-2160p",
            "converted_from_format": "HDR10",
            "replace_existing": True,
            "untouched_basename": "Reused Record (1984) - [Bluray-2160p].mkv",
            "aws_key": "untouched/Reused Record (1984) - [Bluray-2160p].mkv",
            "aws_date_uploaded": datetime.utcnow(),
            "aws_filesize_bytes": 11,
        }
        inspection = {
            "video": {"format": "AVC", "color_primaries": "BT.709"},
            "audio_tracks": [],
            "subtitle_tracks": [],
            "filesize_bytes": 10,
        }
        lock = app.lock_manager.lock(json.dumps({"title": "Reused Record"}), 10000)
        videos.finalize_localization(source, details, lock, hidden, inspection)

        db.session.expire_all()
        file = File.query.filter_by(file_path=real.file_path).one()
        assert file.converted_from_quality == "Bluray-2160p"
        assert file.untouched_basename == "Reused Record (1984) - [Bluray-2160p].mkv"
        assert file.aws_key == ("untouched/Reused Record (1984) - [Bluray-2160p].mkv")
        os.remove(os.path.join(app.config["LIBRARY_DIR"], file.file_path))


def test_a_missing_held_volume_prunes_no_row(app, monkeypatch, tmp_path):
    """A dropped volume makes each held file look gone (#319)."""

    import app.videos as videos
    from app import db
    from app.models import HeldImport
    from app.sdr_conversion import prune_held_imports

    held_dir = tmp_path / "held"
    held_dir.mkdir()
    monkeypatch.setitem(app.config, "HELD_DIR", str(held_dir))

    def row(name):
        """Add a HeldImport for a file in the held directory."""

        db.session.add(
            HeldImport(
                held_path=str(held_dir / name),
                untouched_basename=name,
                file_details="{}",
                source_format="HDR10",
            )
        )

    with app.app_context():
        row("Gone (2001) - [Bluray-2160p].mkv")
        row("Kept (2002) - [Bluray-2160p].mkv")
        (held_dir / "Kept (2002) - [Bluray-2160p].mkv").write_bytes(b"video")
        db.session.commit()

        # The volume is dead. Nothing goes.
        monkeypatch.setattr(videos, "_dead_volumes", lambda paths: ["/Volumes/x"])
        assert prune_held_imports() == 0
        assert HeldImport.query.count() == 2

        # The volume answers. Only the row of the missing file goes.
        monkeypatch.setattr(videos, "_dead_volumes", lambda paths: [])
        assert prune_held_imports() == 1
        assert [h.untouched_basename for h in HeldImport.query.all()] == [
            "Kept (2002) - [Bluray-2160p].mkv"
        ]

        # The held directory is not there (an unmounted mountpoint).
        monkeypatch.setitem(app.config, "HELD_DIR", str(tmp_path / "absent"))
        assert prune_held_imports() == 0
        assert HeldImport.query.count() == 1


def _foreign_lock(app, resource):
    """Return a lock tuple whose title another task holds now (#339)."""

    from redlock import Lock

    app.redis.set(resource, "other-task-token", px=60000)
    return Lock(0, resource, "expired-token")


def test_a_conversion_waits_while_another_task_holds_the_title_lock(
    app, tmp_path, monkeypatch
):
    """Test that convert_localized_file does not encode without its lock."""

    from app import sdr_conversion

    hidden = tmp_path / ".Locked (2001) - [Bluray-1080p].mkv"
    hidden.write_bytes(b"localized")
    source = tmp_path / "Locked (2001) - [Remux-2160p].mkv"
    source.write_bytes(b"source")
    encodes = []
    monkeypatch.setattr(sdr_conversion, "_run_encode", lambda *a: encodes.append(a))
    resource = '{"title": "Locked Conversion"}'

    with app.app_context():
        lock = _foreign_lock(app, resource)
        result = sdr_conversion.convert_localized_file(
            str(source),
            {"basename": "Locked (2001) - [Bluray-1080p].mkv"},
            lock,
            str(hidden),
            {"source_format": "HDR10", "source_quality": "x", "target_quality": "y"},
        )

    assert result is False
    assert encodes == []
    assert hidden.exists() and source.exists()
    assert app.redis.get(resource) == b"other-task-token"
    retries = [
        job for job in _jobs(app.transcode_queue, "app.videos.convert_localized_file")
    ] + [
        job
        for job in scheduled_jobs(app.transcode_queue)
        if job.func_name == "app.videos.convert_localized_file"
    ]
    assert len(retries) == 1
    assert retries[0].kwargs == {"lock_waits": 1}
    assert retries[0].id.endswith("lock_waits_1")


def test_a_conversion_that_loses_its_lock_keeps_the_localized_file(
    app, tmp_path, monkeypatch
):
    from app import sdr_conversion

    hidden = tmp_path / ".Lost (2001) - [Bluray-1080p].mkv"
    hidden.write_bytes(b"localized")
    source = tmp_path / "Lost (2001) - [Remux-2160p].mkv"
    source.write_bytes(b"source")

    def lose_lock(*args):
        raise sdr_conversion.TitleLockLost("Lost")

    monkeypatch.setattr(sdr_conversion, "_run_encode", lose_lock)
    monkeypatch.setattr(sdr_conversion, "probe_video", lambda path: {})

    with app.app_context():
        result = sdr_conversion.convert_localized_file(
            str(source),
            {"basename": "Lost (2001) - [Bluray-1080p].mkv"},
            None,
            str(hidden),
            {"source_format": "HDR10", "source_quality": "x", "target_quality": "y"},
        )

    assert result is False
    assert hidden.exists() and source.exists()
    assert any(
        job.func_name == "app.videos.convert_localized_file"
        for job in scheduled_jobs(app.transcode_queue)
    )


def test_a_move_waits_while_another_task_holds_the_title_lock(app, tmp_path):
    import app.videos as videos

    hidden = tmp_path / ".Moved (2001) - [DVD].mkv"
    hidden.write_bytes(b"localized")
    resource = '{"title": "Locked Move"}'
    with app.app_context():
        lock = _foreign_lock(app, resource)
        result = videos.move_localized_file(
            "/import/Moved (2001) - [DVD].mkv",
            {"basename": "Moved (2001) - [DVD].mkv", "dirname": "Movies/Moved (2001)"},
            lock,
            str(hidden),
        )

    assert result is False
    assert hidden.exists()
    retries = [
        job
        for job in scheduled_jobs(app.handoff_queue)
        if job.func_name == "app.videos.move_localized_file"
    ]
    assert len(retries) == 1
    assert retries[0].kwargs["lock_waits"] == 1
    assert app.redis.get(resource) == b"other-task-token"


class _RunningProcess:
    """A fake child process that runs until it is killed."""

    def __init__(self, lines):
        self.stdout = iter(lines)
        self.killed = False
        self.waited = False

    def poll(self):
        return -9 if self.killed else None

    def kill(self):
        self.killed = True

    def wait(self, *args, **kwargs):
        self.waited = True
        return -9


def test_any_failure_in_the_encode_loop_kills_the_encoder(app, monkeypatch):
    """A failure other than a lost lock also stops the encoder (#424).

    A job timeout or a Redis fault in report_progress went up and left
    the encoder writing into an output that the cleanup unlinks."""

    import pytest

    import app.sdr_conversion as sdr_conversion

    process = _RunningProcess(["Encoding: task 1 of 1, 5.00 %\n"])
    monkeypatch.setattr(sdr_conversion.subprocess, "Popen", lambda *a, **k: process)

    def broken(*args, **kwargs):
        raise RuntimeError("redis down")

    monkeypatch.setattr(sdr_conversion, "report_progress", broken)
    with app.app_context(), pytest.raises(RuntimeError):
        sdr_conversion._run_encode(["HandBrakeCLI"], None, "Film.mkv", None, 100)
    assert process.killed and process.waited


def test_a_failure_in_the_mkvmerge_loop_kills_mkvmerge(app, monkeypatch):
    """The shared mkvmerge progress loop stops its process on a failure (#424)."""

    import pytest

    import app.tracks as tracks

    process = _RunningProcess(["Progress: 5%\n"])

    def broken(*args, **kwargs):
        raise RuntimeError("redis down")

    monkeypatch.setattr(tracks, "report_progress", broken)
    with app.app_context(), pytest.raises(RuntimeError):
        tracks.watch_mkvmerge_progress(process, None, "Film.mkv", "Remuxing")
    assert process.killed and process.waited


def test_a_child_that_ignores_the_kill_does_not_hold_the_task(app, monkeypatch, caplog):
    """A child stuck in NFS I/O ignores SIGKILL (follow-up #424).

    The wait after the kill has a limit. A warning names the process,
    and the original fault still goes up."""

    import subprocess

    import pytest

    import app.tracks as tracks

    class StuckProcess(_RunningProcess):
        pid = 4242

        def poll(self):
            return None

        def wait(self, timeout=None):
            self.timeout = timeout
            raise subprocess.TimeoutExpired("mkvmerge", timeout)

    process = StuckProcess(["Progress: 5%\n"])

    def broken(*args, **kwargs):
        raise RuntimeError("redis down")

    monkeypatch.setattr(tracks, "report_progress", broken)
    with app.app_context(), pytest.raises(RuntimeError, match="redis down"):
        tracks.watch_mkvmerge_progress(process, None, "Film.mkv", "Remuxing")
    assert process.killed
    assert process.timeout == tracks.STOP_PROCESS_WAIT_SECONDS
    assert "Process 4242 did not end" in caplog.text
