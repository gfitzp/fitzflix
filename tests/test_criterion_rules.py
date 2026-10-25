"""Test the pure rules of the Criterion page and of the filmography merge."""

from types import SimpleNamespace


def test_criterion_settled(app):
    """The disc must be owned and the copy must meet the release, capped at 18."""

    from app.file_ranking import criterion_settled

    wide = SimpleNamespace(fullscreen=False)
    full = SimpleNamespace(fullscreen=True)
    bluray = SimpleNamespace(preference=18)
    dvd = SimpleNamespace(preference=9)

    assert criterion_settled(wide, bluray, 20, 18, True) is True
    assert criterion_settled(wide, bluray, 20, 18, False) is False
    assert criterion_settled(wide, dvd, 9, 18, True) is True
    assert criterion_settled(wide, dvd, None, 18, True) is False
    assert criterion_settled(full, bluray, 9, 18, True) is False


def test_catalog_rows_for_skips_the_consumed_the_excluded_and_the_set_shells(app):
    from app.main.library import catalog_rows_for

    releases = [
        {"tmdb_id": 1, "title": "OWNED", "year": 1950, "spine_number": 1},
        {"tmdb_id": 2, "title": "EXCLUDED", "year": 1951, "spine_number": 2},
        {"tmdb_id": None, "title": "SET", "year": 1952, "spine_number": 88},
        {
            "tmdb_id": 3,
            "title": "PART I",
            "label": "Part I",
            "year": 1944,
            "spine_number": 88,
            "set_title": "Ivan",
        },
        {"tmdb_id": 3, "title": "PART I", "year": 1944, "spine_number": 89},
        {"tmdb_id": None, "title": "PLAIN", "year": 1960, "spine_number": 90},
        {"tmdb_id": None, "title": "BY NAME", "year": 1961, "spine_number": 91},
    ]
    rows = catalog_rows_for(releases, {1}, {("BY NAME", 1961)}, {2})
    assert [(row["kind"], row["tmdb_id"], row["title"]) for row in rows] == [
        ("tmdb", 3, "Part I"),
        ("plain", None, "PLAIN"),
    ]


def test_criterion_sort_key_puts_set_members_at_the_spine_of_their_set(app):
    from app.main.library import criterion_sort_key

    rows = [
        {"spine": None, "title": "No Spine", "year": 2000},
        {"spine": 88, "title": "The Second", "year": 1958, "set_title": "Ivan"},
        {"spine": 88, "title": "A First", "year": 1944, "set_title": "Ivan"},
        {"spine": 2, "title": "Seven Samurai", "year": 1954},
    ]
    assert [row["title"] for row in sorted(rows, key=criterion_sort_key)] == [
        "Seven Samurai",
        "A First",
        "The Second",
        "No Spine",
    ]


def test_merge_credit_rows_folds_cast_and_crew_into_1_row(app):
    from app.main.library import merge_credit_rows

    credits = {
        "cast": [
            {"id": 7, "name": "Show", "first_air_date": "1999-01-01", "character": "A"},
            {"id": 7, "name": "Show", "character": "B"},
            {"id": None, "name": "Ghost"},
        ],
        "crew": [
            {"id": 7, "job": "Original Music Composer"},
            {"id": 7, "job": "Director"},
            {"id": 8, "name": "Other", "job": "Grip"},
        ],
    }
    rows = merge_credit_rows(credits, {7: "local"}, "name", "first_air_date")
    assert list(rows) == [7, 8]
    assert rows[7]["name"] == "Show"
    assert rows[7]["year"] == 1999
    assert rows[7]["characters"] == ["A", "B"]
    assert rows[7]["jobs"] == ["Director", "Composer"]
    assert rows[7]["local"] == "local"
    assert rows[8]["jobs"] == [] and rows[8]["local"] is None
    assert merge_credit_rows(None, {}, "title", "release_date") == {}
