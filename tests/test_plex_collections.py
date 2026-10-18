"""Test the Criterion collection sync into Plex (#281).

The sync selects the files with the rule of the criterion_collection
view. It mirrors them into a collection in each Plex movie library. It
matches the films to the Plex items by the file path.
"""

from urllib.parse import unquote

from tests.factories import make_movie, make_movie_file

LIBRARY = "/lib"
NAME = "Criterion Collection Films"


class FakePlex:
    """Fake Plex sections, items, and collections. Records each write."""

    def __init__(self, sections):
        # The shape of sections: {key: (type, title, {ratingKey: file path})}
        self.sections = sections
        # The shape of collections: {section key: {title: [ratingKey, members]}}
        self.collections = {}
        self.writes = []
        self.next_key = 9000
        # The collectionSort value of each collection ratingKey (#282).
        self.sorts = {}

    def get(self, path, params=None):
        params = params or {}
        if path == "/library/sections":
            return {
                "MediaContainer": {
                    "Directory": [
                        {"key": key, "type": kind, "title": title}
                        for key, (kind, title, _) in self.sections.items()
                    ]
                }
            }
        parts = path.split("/")
        if path.endswith("/all"):
            items = [
                {"ratingKey": rk, "Media": [{"Part": [{"file": file}]}]}
                for rk, file in self.sections[parts[3]][2].items()
            ]
            return self._page(items, params)
        if path.endswith("/collections"):
            return {
                "MediaContainer": {
                    "Metadata": [
                        {
                            "title": title,
                            "ratingKey": rk,
                            "collectionSort": self.sorts.get(rk),
                        }
                        for title, (rk, _) in self.collections.get(parts[3], {}).items()
                    ]
                }
            }
        if path.endswith("/children"):
            members = self._members(parts[3])
            return self._page([{"ratingKey": rk} for rk in sorted(members)], params)
        raise AssertionError(f"unexpected GET {path}")

    def _page(self, items, params):
        start = params.get("X-Plex-Container-Start", 0)
        size = params.get("X-Plex-Container-Size", len(items))
        return {
            "MediaContainer": {
                "totalSize": len(items),
                "Metadata": items[start : start + size],
            }
        }

    def _members(self, collection_key):
        for by_title in self.collections.values():
            for rk, members in by_title.values():
                if rk == collection_key:
                    return members
        raise AssertionError(f"no collection {collection_key}")

    def request(self, method, path, params=None):
        params = params or {}
        self.writes.append((method, path))
        keys = []
        if "uri" in params:
            keys = unquote(params["uri"]).rsplit("/", 1)[1].split(",")
        if method == "POST" and path == "/library/collections":
            self.next_key += 1
            rk = str(self.next_key)
            self.collections.setdefault(params["sectionId"], {})[params["title"]] = [
                rk,
                set(keys),
            ]
            return {"MediaContainer": {"Metadata": [{"ratingKey": rk}]}}
        parts = path.split("/")
        if method == "PUT" and path.endswith("/items"):
            self._members(parts[3]).update(keys)
            return {}
        if method == "PUT" and path.endswith("/prefs"):
            self.sorts[parts[3]] = params["collectionSort"]
            return {}
        if method == "DELETE":
            self._members(parts[3]).discard(parts[5])
            return {}
        raise AssertionError(f"unexpected {method} {path}")


def _setup(app, monkeypatch, sections):
    import app.plex_collections as plex_collections
    import app.plex_player as plex_player

    fake = FakePlex(sections)
    monkeypatch.setitem(app.config, "PLEX_URL", "http://plex.test")
    monkeypatch.setitem(app.config, "PLEX_TOKEN", "token")
    monkeypatch.setitem(app.config, "LIBRARY_DIR", LIBRARY)
    monkeypatch.setitem(app.config, "PLEX_CRITERION_COLLECTION", NAME)
    monkeypatch.setattr(plex_collections, "_plex_get", fake.get)
    monkeypatch.setattr(plex_collections, "_plex_request", fake.request)
    monkeypatch.setattr(plex_player, "_server_machine_id", lambda: "machine")
    return plex_collections, fake


def _path(file):
    return f"{LIBRARY}/{file.file_path}"


def _library(app):
    """Make 1 film that the rule selects and 4 that it does not select."""

    from app import db

    with app.app_context():
        ran = make_movie("Ran", 1985, criterion_spine_number=1, custom_poster="p.jpg")
        selected = make_movie_file(ran, "Bluray-1080p")
        extra = make_movie_file(ran, "Bluray-1080p", "Featurettes")

        # The best copy is digital. A DVD copy below it does not count.
        digital = make_movie(
            "Ikiru", 1952, criterion_spine_number=2, custom_poster="p.jpg"
        )
        web = make_movie_file(digital, "WEBDL-1080p")
        make_movie_file(digital, "DVD")

        # No custom poster.
        plain = make_movie("Kagemusha", 1980, criterion_spine_number=3)
        no_poster = make_movie_file(plain, "Bluray-1080p")

        # 2 films of a box set that share 1 spine.
        set_a = make_movie(
            "Olympia 1", 1938, criterion_spine_number=4, custom_poster="p"
        )
        set_b = make_movie(
            "Olympia 2", 1938, criterion_spine_number=4, custom_poster="p"
        )
        boxed = make_movie_file(set_a, "Bluray-1080p")
        make_movie_file(set_b, "Bluray-1080p")
        db.session.commit()
        return {
            "selected": _path(selected),
            "extra": _path(extra),
            "web": _path(web),
            "no_poster": _path(no_poster),
            "boxed": _path(boxed),
        }


def test_the_rule_matches_the_view(app, monkeypatch):
    """Only the physical best copy of a unique spine with a poster qualifies."""

    paths = _library(app)
    import app.plex_collections as plex_collections

    monkeypatch.setitem(app.config, "LIBRARY_DIR", LIBRARY)
    with app.app_context():
        assert plex_collections.criterion_paths() == {paths["selected"]}


def test_sync_creates_fills_and_trims_each_movie_library(app, monkeypatch):
    """The collection mirrors the rule in every movie library.

    It is created where it is missing. A member that the rule does not
    select is removed. A show library is not touched."""

    paths = _library(app)
    plex_collections, fake = _setup(
        app,
        monkeypatch,
        {
            "5": ("movie", "Movies", {"11": paths["selected"], "12": paths["web"]}),
            "7": (
                "movie",
                "Movies (4K)",
                {"21": paths["selected"], "22": paths["boxed"]},
            ),
            "4": ("show", "TV Shows", {}),
        },
    )
    fake.collections["5"] = {NAME: ["500", {"12"}]}

    assert plex_collections.sync_criterion_collection() is True

    assert fake.collections["5"][NAME][1] == {"11"}
    rk, members = fake.collections["7"][NAME]
    assert members == {"21"}
    assert ("DELETE", "/library/collections/500/items/12") in fake.writes
    assert "4" not in fake.collections

    # A second run changes nothing.

    fake.writes.clear()
    assert plex_collections.sync_criterion_collection() is True
    assert fake.writes == []


def test_sync_removes_nothing_when_the_match_is_broken(app, monkeypatch):
    """A selected file with no Plex item can mean a broken match.

    Then the sync adds what it can, and it removes nothing."""

    paths = _library(app)
    plex_collections, fake = _setup(
        app, monkeypatch, {"5": ("movie", "Movies", {"12": paths["web"]})}
    )
    fake.collections["5"] = {NAME: ["500", {"12"}]}

    assert plex_collections.sync_criterion_collection() is True
    assert fake.collections["5"][NAME][1] == {"12"}
    assert not [w for w in fake.writes if w[0] == "DELETE"]


def test_sync_does_nothing_without_plex(app, monkeypatch):
    import app.plex_collections as plex_collections

    monkeypatch.setitem(app.config, "PLEX_URL", None)
    assert plex_collections.sync_criterion_collection() is True


def test_enqueue_needs_plex(app, monkeypatch):
    """A poster change queues the sync only when Plex is configured."""

    import app.plex_collections as plex_collections

    with app.app_context():
        monkeypatch.setitem(app.config, "PLEX_URL", None)
        plex_collections.enqueue_criterion_sync()
        assert len(app.maintenance_queue) == 0

        monkeypatch.setitem(app.config, "PLEX_URL", "http://plex.test")
        monkeypatch.setitem(app.config, "PLEX_TOKEN", "token")
        plex_collections.enqueue_criterion_sync()
        assert len(app.maintenance_queue) == 1
        app.maintenance_queue.empty()


def test_sync_explains_a_collection_that_refuses_items(app, monkeypatch, caplog):
    """A collection without its tag answers 400 to each add.

    The sync logs how to fix it. It removes nothing from that collection,
    and it continues with the other libraries."""

    import requests

    paths = _library(app)
    plex_collections, fake = _setup(
        app,
        monkeypatch,
        {
            "5": ("movie", "Movies", {"11": paths["selected"], "12": paths["web"]}),
            "7": ("movie", "Movies (4K)", {"21": paths["selected"]}),
        },
    )
    fake.collections["5"] = {NAME: ["500", {"12"}]}
    real_request = fake.request

    def refuse(method, path, params=None):
        if method == "PUT" and path == "/library/collections/500/items":
            response = requests.Response()
            response.status_code = 400
            raise requests.HTTPError("400 Client Error", response=response)
        return real_request(method, path, params)

    monkeypatch.setattr(plex_collections, "_plex_request", refuse)
    with caplog.at_level("WARNING"):
        assert plex_collections.sync_criterion_collection() is True

    assert "(ratingKey 500) refuses new items" in caplog.text
    assert fake.collections["5"][NAME][1] == {"12"}
    assert fake.collections["7"][NAME][1] == {"21"}


def _box_sets(app):
    """Make 1 box set with 3 films, and 2 sets that get no collection."""

    from app import db

    with app.app_context():
        both = make_movie("Olympia Part One", 1938, criterion_set_title="Olympia Films")
        # The disc copy of this film is gone from the disk. Plex has the
        # digital copy.
        disc = make_movie_file(both, "DVD", plex_title="Olympia Part One (1938) disc")
        web = make_movie_file(both, "WEBDL-1080p")
        second = make_movie(
            "Olympia Part Two", 1938, criterion_set_title="Olympia Films"
        )
        bluray = make_movie_file(second, "Bluray-1080p")

        # A digital-only film of the set is not in the collection.
        digital = make_movie(
            "Olympia Outtakes", 1938, criterion_set_title="Olympia Films"
        )
        digital_only = make_movie_file(digital, "WEBRip-1080p")

        # A set with 1 film on disc gets no collection.
        alone = make_movie("Solo", 1950, criterion_set_title="Lonely Set")
        lonely = make_movie_file(alone, "Bluray-1080p")
        db.session.commit()
        return {
            "disc": _path(disc),
            "web": _path(web),
            "bluray": _path(bluray),
            "digital_only": _path(digital_only),
            "lonely": _path(lonely),
        }


def test_box_set_rule(app, monkeypatch):
    """A set film needs any main-feature file on disc. A set needs 2 films."""

    import app.plex_collections as plex_collections

    paths = _box_sets(app)
    monkeypatch.setitem(app.config, "LIBRARY_DIR", LIBRARY)
    with app.app_context():
        groups = plex_collections.box_set_groups()
    assert set(groups) == {"Olympia Films"}
    assert sorted(sorted(group) for group in groups["Olympia Films"]) == sorted(
        [sorted([paths["disc"], paths["web"]]), [paths["bluray"]]]
    )


def test_box_set_collection_mirrors_and_sorts_by_release(app, monkeypatch):
    """The set collection matches a film through its digital copy.

    It removes a stale member, and it sorts by release date. A second
    run writes nothing."""

    paths = _box_sets(app)
    plex_collections, fake = _setup(
        app,
        monkeypatch,
        {
            "5": (
                "movie",
                "Movies",
                {
                    "31": paths["web"],
                    "32": paths["bluray"],
                    "33": paths["digital_only"],
                    "34": paths["lonely"],
                },
            ),
        },
    )
    fake.collections["5"] = {"Olympia Films": ["600", {"33"}]}

    assert plex_collections.sync_criterion_collection() is True
    assert fake.collections["5"]["Olympia Films"][1] == {"31", "32"}
    assert fake.sorts["600"] == "0"
    assert "Lonely Set" not in fake.collections["5"]
    assert NAME not in fake.collections["5"]

    fake.writes.clear()
    assert plex_collections.sync_criterion_collection() is True
    assert fake.writes == []


def _editions(app):
    """Make 2 Criterion films with editions, also in 1 box set (#284)."""

    from app import db

    with app.app_context():
        jour = make_movie(
            "Jour de fete",
            1949,
            criterion_spine_number=10,
            custom_poster="p.jpg",
            criterion_set_title="Tati Set",
        )
        plain = make_movie_file(jour, "Bluray-1080p")
        re_edit = make_movie_file(
            jour,
            "Bluray-1080p",
            plex_title="Jour de fete (1949) {edition-1964 Re-Edit}",
            edition="1964 Re-Edit",
        )

        # This film exists only as editions. The first edition by name stays.
        only = make_movie(
            "Playtime",
            1967,
            criterion_spine_number=11,
            custom_poster="p.jpg",
            criterion_set_title="Tati Set",
        )
        cut_a = make_movie_file(
            only,
            "Bluray-1080p",
            plex_title="Playtime (1967) {edition-A Cut}",
            edition="A Cut",
        )
        cut_b = make_movie_file(
            only,
            "Bluray-1080p",
            plex_title="Playtime (1967) {edition-B Cut}",
            edition="B Cut",
        )
        db.session.commit()
        return {
            "plain": _path(plain),
            "re_edit": _path(re_edit),
            "cut_a": _path(cut_a),
            "cut_b": _path(cut_b),
        }


def test_collections_take_1_version_of_each_film(app, monkeypatch):
    """The version with no edition wins. Else the first edition by name."""

    import app.plex_collections as plex_collections

    paths = _editions(app)
    monkeypatch.setitem(app.config, "LIBRARY_DIR", LIBRARY)
    with app.app_context():
        assert plex_collections.criterion_paths() == {paths["plain"], paths["cut_a"]}
        groups = plex_collections.box_set_groups()["Tati Set"]
    assert sorted(sorted(group) for group in groups) == sorted(
        [[paths["plain"]], [paths["cut_a"]]]
    )


def test_sync_removes_edition_items(app, monkeypatch):
    """An edition item that is already in a collection leaves it (#284)."""

    paths = _editions(app)
    plex_collections, fake = _setup(
        app,
        monkeypatch,
        {
            "5": (
                "movie",
                "Movies",
                {
                    "41": paths["plain"],
                    "42": paths["re_edit"],
                    "43": paths["cut_a"],
                    "44": paths["cut_b"],
                },
            ),
        },
    )
    fake.collections["5"] = {NAME: ["700", {"41", "42", "43", "44"}]}

    assert plex_collections.sync_criterion_collection() is True
    assert fake.collections["5"][NAME][1] == {"41", "43"}
    assert fake.collections["5"]["Tati Set"][1] == {"41", "43"}
