"""Test app/file_ranking.py: the one best-copy rule and its readers (#361)."""

import re

import pytest

from app import db


def _rip_and_conversion(make_file, *args):
    """Make a 1080p SDR conversion and then a real rip of the same tier.

    The conversion has the lower file id. Thus, an order without the
    conversion tiebreak returns the conversion first."""

    conversion = make_file(*args, converted_from_quality="Bluray-2160p")
    conversion.basename = "Conversion - [Bluray-1080p].mkv"
    conversion.file_path = f"{conversion.dirname}/{conversion.basename}"
    db.session.flush()
    rip = make_file(*args)
    rip.basename = "Rip - [Bluray-1080p].mkv"
    db.session.flush()
    return rip, conversion


def _movie_pair():
    from tests.factories import make_movie, make_movie_file

    movie = make_movie("Tiebreak", 1984)
    rip, conversion = _rip_and_conversion(make_movie_file, movie, "Bluray-1080p")
    return movie, rip, conversion


def _episode_pair():
    from tests.factories import make_tv_file, make_tv_series

    series = make_tv_series("Tiebreak Show")
    rip, conversion = _rip_and_conversion(make_tv_file, series, 1, 1, "Bluray-1080p")
    return series, rip, conversion


def test_every_movie_best_copy_reader_prefers_the_real_rip(app, monkeypatch):
    """Each movie best-copy reader selects the real rip over the conversion."""

    import app.dvr as dvr
    from app.frames import _best_file
    from app.main.library import _films_and_features
    from app.main.search import _movie_search_results
    from app.models import ranked_movie_files

    with app.app_context():
        movie, rip, conversion = _movie_pair()
        db.session.commit()

        assert _best_file(movie.id)[0].id == rip.id

        monkeypatch.setattr(dvr, "_on_disk", lambda file: True)
        assert dvr._best_files_by_movie()[movie.id][1].id == rip.id

        films, _ = _films_and_features(movie.id)
        assert [film.id for film in films] == [rip.id, conversion.id]

        results = _movie_search_results("%Tiebreak%")
        assert results[0]["best_file"].id == rip.id

        ranked = ranked_movie_files()
        ranks = dict(db.session.query(ranked.c.id, ranked.c.rank))
        assert ranks == {rip.id: 1, conversion.id: 2}


def test_the_movie_files_page_lists_the_real_rip_first(app, admin_client):
    with app.app_context():
        movie, _, _ = _movie_pair()
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get(f"/movie/{movie_id}/files").get_data(as_text=True)
    assert page.index("Rip - [Bluray-1080p].mkv") < page.index(
        "Conversion - [Bluray-1080p].mkv"
    )


def test_every_episode_best_copy_reader_prefers_the_real_rip(app, monkeypatch):
    """Each episode best-copy reader selects the real rip over the conversion."""

    import app.dvr as dvr
    from app.main.library import _season_files
    from app.models import ranked_tv_files

    with app.app_context():
        series, rip, conversion = _episode_pair()
        db.session.commit()

        monkeypatch.setattr(dvr, "_on_disk", lambda file: True)
        assert [file.id for file in dvr._series_episodes(series.id)] == [rip.id]

        rows = _season_files(series.id, 1)
        assert [(row[0].id, row[3]) for row in rows] == [
            (rip.id, 1),
            (conversion.id, 2),
        ]

        ranked = ranked_tv_files()
        ranks = dict(db.session.query(ranked.c.id, ranked.c.rank))
        assert ranks == {rip.id: 1, conversion.id: 2}


def test_the_filmography_key_puts_widescreen_before_a_better_fullscreen(app):
    """best_copy_key has the fullscreen rule and the conversion rule."""

    from app.file_ranking import best_copy_key
    from app.models import File, RefQuality

    with app.app_context():
        tiers = {q.quality_title: q for q in RefQuality.query.all()}
    widescreen_dvd = File(id=3, fullscreen=False)
    fullscreen_uhd = File(id=1, fullscreen=True)
    conversion = File(id=2, fullscreen=False, converted_from_quality="Bluray-2160p")
    rip = File(id=4, fullscreen=False)

    assert best_copy_key(widescreen_dvd, tiers["DVD"]) < best_copy_key(
        fullscreen_uhd, tiers["Bluray-2160p"]
    )
    assert best_copy_key(rip, tiers["Bluray-1080p"]) < best_copy_key(
        conversion, tiers["Bluray-1080p"]
    )
    assert best_copy_key(rip, tiers["DVD"]) < best_copy_key(None, None)


@pytest.mark.parametrize(
    "fullscreen, preference, criterion_pref, physical, expected",
    [
        (False, 18, None, False, False),
        (False, 17, None, False, True),
        (True, 21, None, False, True),
        (False, 9, None, True, False),
        (True, 9, None, True, False),
        (False, 18, 20, False, False),
        (False, 15, 15, False, False),
        (False, 14, 15, False, True),
        (True, 21, 15, False, True),
    ],
)
def test_copy_upgradable(
    app, fullscreen, preference, criterion_pref, physical, expected
):
    """The threshold is 18. A Criterion quality lowers the bar, never raises it."""

    from app.file_ranking import copy_upgradable

    assert (
        copy_upgradable(
            fullscreen, preference, 18, criterion_pref=criterion_pref, physical=physical
        )
        is expected
    )


def test_quality_anchors_read_the_tiers(app):
    from app.file_ranking import (
        lowest_physical_preference,
        quality_id,
        quality_preference,
        upgrade_threshold,
    )
    from app.models import RefQuality

    with app.app_context():
        remux = RefQuality.query.filter_by(quality_title="Bluray-2160p Remux").one()
        assert quality_id("Bluray-2160p Remux") == remux.id
        assert quality_preference("Bluray-2160p Remux") == remux.preference
        assert quality_preference("No such tier") is None
        assert upgrade_threshold() == 18
        assert lowest_physical_preference("Bluray-1080") == 18
        assert lowest_physical_preference("Bluray-2160") == 20


def _window(sql):
    """Return the normalized PARTITION BY ... ORDER BY text of a window clause."""

    text = re.sub(r"\s+", " ", sql.replace("`", "")).lower()
    text = text.replace("ref_quality.", "q.").replace("( ", "(").replace(" )", ")")
    return text.strip()


def test_the_view_ddl_has_the_order_of_the_python_ranks(app):
    """RANKED_FILES_VIEW and the 2 rank functions agree (#361).

    The test compiles each rank to the MySQL dialect. It compares the
    window clause with the clause of the view."""

    from sqlalchemy.dialects import mysql

    from app.file_ranking import RANKED_FILES_VIEW, movie_file_rank, tv_file_rank

    view_windows = [
        _window(clause)
        for clause in re.findall(
            r"OVER \((.*?)\) AS", RANKED_FILES_VIEW, flags=re.DOTALL
        )
    ]
    python_windows = []
    for rank in (movie_file_rank, tv_file_rank):
        compiled = str(rank().element.compile(dialect=mysql.dialect()))
        python_windows.append(_window(re.search(r"OVER \((.*)\)", compiled).group(1)))

    assert python_windows == view_windows


def test_the_view_ddl_is_the_definition_of_the_last_view_migration(app):
    """The migration that last created ranked_files runs RANKED_FILES_VIEW."""

    import importlib.util
    import os

    from app.file_ranking import RANKED_FILES_VIEW

    path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "migrations",
        "versions",
        "d4e2a9c71b38_hdr_to_sdr_conversions.py",
    )
    spec = importlib.util.spec_from_file_location("ranked_view_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert _window(migration.RANKED_FILES) == _window(RANKED_FILES_VIEW)
