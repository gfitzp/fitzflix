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
