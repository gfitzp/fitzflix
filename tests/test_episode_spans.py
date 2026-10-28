"""Test that a multi-episode file competes with the singles of each episode it holds (#479).

The live case: Top Gear (2002) S19 holds a WEBDL-1080p "S19E06-E07" and
a later HDTV-720p "S19E07". The rules keyed on the first episode, so
the worse single imported and stayed."""

from app import db
from tests.factories import make_tv_file, make_tv_series


def _details(series, season, episode, last_episode, quality_title, dirname):
    return {
        "media_library": "TV Shows",
        "dirname": dirname,
        "title": series.title,
        "season": season,
        "episode": episode,
        "last_episode": last_episode,
        "edition": None,
        "quality_title": quality_title,
        "fullscreen": False,
    }


def test_a_span_blocks_a_worse_or_equal_later_single(app):
    from app.file_ranking import find_better_files

    with app.app_context():
        series = make_tv_series("Top Gear (2002)")
        span = make_tv_file(series, 19, 6, "WEBDL-1080p", last_episode=7)
        db.session.commit()

        for quality_title in ("HDTV-720p", "WEBDL-1080p"):
            details = _details(series, 19, 7, 7, quality_title, span.dirname)
            assert find_better_files(details) == [span], quality_title

        # A better single is not blocked. A single outside the span is not
        # blocked
        assert (
            find_better_files(_details(series, 19, 7, 7, "Bluray-1080p", span.dirname))
            == []
        )
        assert (
            find_better_files(_details(series, 19, 8, 8, "HDTV-720p", span.dirname))
            == []
        )
        db.session.rollback()


def test_a_span_removes_a_worse_later_single(app):
    from app.file_ranking import find_worse_files

    with app.app_context():
        series = make_tv_series("The Golden Girls")
        single = make_tv_file(series, 5, 26, "HDTV-720p")
        outside = make_tv_file(series, 5, 27, "HDTV-720p")
        better = make_tv_file(series, 5, 25, "Bluray-1080p")
        span = make_tv_file(series, 5, 25, "WEBDL-1080p", last_episode=26)
        db.session.commit()

        worse = find_worse_files(span)
        assert single in worse
        assert outside not in worse
        assert better not in worse
        db.session.rollback()


def _season_with_a_better_first_single(app, span_quality):
    """Return a series whose E01 single is better than the E01-E06 span."""

    series = make_tv_series(f"Span Show {span_quality}")
    single = make_tv_file(series, 1, 1, "Bluray-1080p", aws_key="untouched/e01.mkv")
    span = make_tv_file(
        series, 1, 1, span_quality, last_episode=6, aws_key="untouched/e01-e06.mkv"
    )
    db.session.commit()
    return series, single, span


def test_the_library_page_and_the_shopping_list_agree_on_a_span_season(
    app, user_client
):
    with app.app_context():
        series, _, _ = _season_with_a_better_first_single(app, "DVD")
        series_id = series.id

        from app.file_ranking import season_grades

        grade = season_grades([series_id])[(series_id, 1)]
        assert grade.quality_title == "DVD"
        assert grade.episodes == 6

    library = user_client.get("/library/tv?q=Span Show DVD").get_data(as_text=True)
    shopping = user_client.get("/shopping-list/tv?q=Span Show DVD").get_data(
        as_text=True
    )
    assert "DVD" in library
    assert "Bluray-1080p" not in library
    assert "DVD" in shopping


def test_the_badge_sees_the_worst_episode_of_a_span(app):
    from app.main.helpers import series_upgradable

    with app.app_context():
        series, _, _ = _season_with_a_better_first_single(app, "WEBDL-480p")
        assert series_upgradable([series.id]) == {series.id: True}


def test_every_best_holder_of_an_episode_restores(app):
    """A span that alone holds E02 to E06 restores beside the better E01 single."""

    from app.main.library import _season_files, _season_restorable, _series_restorable

    with app.app_context():
        series, single, span = _season_with_a_better_first_single(app, "WEBDL-480p")
        worse_single = make_tv_file(
            series, 1, 3, "SDTV", aws_key="untouched/e03-sdtv.mkv"
        )
        db.session.commit()

        restorable = _series_restorable(series.id)
        assert [file.id for file in restorable] == [single.id, span.id]
        assert worse_single not in restorable
        season = _season_restorable(_season_files(series.id, 1))
        assert [file.id for file in season] == [single.id, span.id]


def _top_gear_season(app):
    """Seed the live shape: a WEBDL span E06-E07, a later HDTV E07, an SDTV E05."""

    series = make_tv_series("Top Gear Holders")
    span = make_tv_file(
        series, 19, 6, "WEBDL-1080p", last_episode=7, aws_key="untouched/span.mkv"
    )
    single = make_tv_file(series, 19, 7, "HDTV-720p", aws_key="untouched/e07.mkv")
    other = make_tv_file(series, 19, 5, "SDTV", aws_key="untouched/e05.mkv")
    db.session.commit()
    return series, span, single, other


def test_the_season_page_badges_only_best_holders(app):
    """The later worse single is not "Best" beside the span (follow-up #479)."""

    from app.main.library import _season_files

    with app.app_context():
        series, span, single, other = _top_gear_season(app)
        ranks = {file.id: rank for file, _, _, rank in _season_files(series.id, 19)}
        assert ranks == {span.id: 1, single.id: 2, other.id: 1}


def test_the_missing_best_scan_groups_by_episode_number(app):
    from app.missing_files import ranked_file_rows

    with app.app_context():
        series, span, single, other = _top_gear_season(app)
        firsts = {
            key[3]: file_id
            for file_id, key, rank in ranked_file_rows()
            if key[0] == "tv" and key[1] == series.id and rank == 1
        }
        assert firsts == {5: other.id, 6: span.id, 7: span.id}
        assert single.id not in firsts.values()


def test_the_sync_restores_no_single_that_a_span_outranks(app, monkeypatch):
    """The paid Bulk restore takes only the best holders (follow-up #479)."""

    from app import aws_storage

    restored = []
    monkeypatch.setattr(
        aws_storage, "aws_restore", lambda key, **kw: restored.append(key)
    )
    monkeypatch.setitem(app.config, "AWS_BUCKET", "test-bucket")
    keys = ["untouched/span.mkv", "untouched/e07.mkv", "untouched/e05.mkv"]
    monkeypatch.setattr(
        aws_storage,
        "get_matching_s3_objects",
        lambda bucket, prefix="": (
            [{"Key": key, "Size": 10} for key in keys] if prefix == "untouched/" else []
        ),
    )
    with app.app_context():
        _top_gear_season(app)
        from app.models import File

        for file in File.query.filter(File.aws_key.in_(keys)):
            file.aws_date_uploaded = file.date_added
        db.session.commit()
        listing = aws_storage._list_archives()
        aws_storage._reconcile_files(listing, None)
    assert sorted(restored) == ["untouched/e05.mkv", "untouched/span.mkv"]


def test_the_column_grade_equals_the_orm_grade(app):
    """season_grades reads columns. Its answer is the answer of ORM pairs (follow-up #479)."""

    from app.file_ranking import best_episode_holders, season_grades
    from app.models import File, RefQuality

    with app.app_context():
        series, _, _, _ = _top_gear_season(app)
        make_tv_file(series, 19, 1, "Bluray-720p")
        make_tv_file(series, 19, 1, "DVD", last_episode=4)
        make_tv_file(series, 20, 1, "WEBDL-480p", fullscreen=True)
        make_tv_file(series, 20, 1, "SDTV")
        db.session.commit()

        pairs = (
            db.session.query(File, RefQuality)
            .join(RefQuality, RefQuality.id == File.quality_id)
            .filter(File.series_id == series.id)
            .all()
        )
        expected = {}
        for (series_id, season, number), (_, quality) in best_episode_holders(
            pairs
        ).items():
            held = expected.get((series_id, season))
            if (
                held is None
                or quality.preference < held[0]
                or (quality.preference == held[0] and not quality.physical_media)
            ):
                expected[(series_id, season)] = (
                    quality.preference,
                    bool(quality.physical_media),
                    quality.quality_title,
                    quality.id,
                )
        grades = season_grades([series.id])
        assert {key: tuple(grade)[:4] for key, grade in grades.items()} == expected
        assert grades[(series.id, 19)].episodes == 7
        assert grades[(series.id, 20)].quality_title == "SDTV"


def test_a_worse_copy_delete_removes_its_episode_thumb(app):
    """A deleted episode leaves no thumb behind (follow-up #469). A movie folder keeps its art."""

    import os

    from app.importing import _delete_worse_copies

    with app.app_context():
        tv_dir = os.path.join(app.config["TV_LIBRARY"], "Thumb Delete", "Season 01")
        movie_dir = os.path.join(app.config["MOVIE_LIBRARY"], "Thumb Film (2001)")
        paths = {}
        for key, directory, name in (
            ("tv", tv_dir, "Thumb Delete - S01E01 - [DVD]"),
            ("movie", movie_dir, "Thumb Film (2001) - [DVD]"),
        ):
            os.makedirs(directory, exist_ok=True)
            video = os.path.join(directory, f"{name}.mkv")
            thumb = os.path.join(directory, f"{name}.jpg")
            for path in (video, thumb):
                with open(path, "wb") as handle:
                    handle.write(b"x")
            paths[key] = (video, thumb)

        _delete_worse_copies(
            [(paths["tv"][0], 999901), (paths["movie"][0], 999902)], "/elsewhere.mkv"
        )
        assert not os.path.exists(paths["tv"][0])
        assert not os.path.exists(paths["tv"][1])
        assert not os.path.exists(paths["movie"][0])
        assert os.path.exists(paths["movie"][1])
        os.remove(paths["movie"][1])
