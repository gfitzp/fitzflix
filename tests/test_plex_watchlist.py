"""The Plex ↔ Fitzflix watchlist sync: the union bootstrap, the
two-way incremental reconcile, push-failure retry semantics, and the
anomaly guard that keeps an API hiccup from reading as a mass removal."""

import json

from app import db
from app.models import Movie, User, UserWatchlist
from tests.factories import make_movie


class FakePlex:
    """A stateful stand-in for the plex.tv discover/metadata APIs.

    Items can carry several tmdb guids (composites, like The
    Animatrix), and ids in `phantom` accept an add with 200 but never
    appear on the watchlist — the Buried Loot behavior."""

    def __init__(self, films, unmatched=(), composites=(), phantom=(), shows=()):
        # films: {tmdb_id: (title, year)} currently on the Plex watchlist
        self.items = {
            f"rk{tmdb_id}": {
                "ids": [tmdb_id],
                "title": title,
                "year": year,
                "listed": True,
                "type": "movie",
            }
            for tmdb_id, (title, year) in films.items()
        }
        # composites: [(rating_key, [tmdb ids], listed)]
        for rating_key, ids, listed in composites:
            self.items[rating_key] = {
                "ids": list(ids),
                "title": f"Composite {rating_key}",
                "year": 2000,
                "listed": listed,
                "type": "movie",
            }
        # shows: {tmdb_id: (title, year)} — TV items, whose tmdb guids
        # are TMDb TV-series ids
        for tmdb_id, (title, year) in dict(shows).items():
            self.items[f"tv{tmdb_id}"] = {
                "ids": [tmdb_id],
                "title": title,
                "year": year,
                "listed": True,
                "type": "show",
            }
        self.unmatched = set(unmatched)
        self.phantom = set(phantom)
        self.adds = []
        self.removes = []

    @property
    def films(self):
        return {
            tmdb_id
            for item in self.items.values()
            if item["listed"]
            for tmdb_id in item["ids"]
        }

    def _key_for(self, tmdb_id):
        for rating_key, item in self.items.items():
            if tmdb_id in item["ids"]:
                return rating_key
        self.items[f"rk{tmdb_id}"] = {
            "ids": [tmdb_id],
            "title": f"Film {tmdb_id}",
            "year": 2000,
            "listed": False,
            "type": "movie",
        }
        return f"rk{tmdb_id}"

    def get(self, url, params=None):
        params = params or {}
        if "plex.tv/api/v2/user" in url:
            return {"username": "gfitzpatrick"}
        if "watchlist/all" in url:
            items = [
                {
                    "title": item["title"],
                    "year": item["year"],
                    "type": item["type"],
                    "ratingKey": rating_key,
                    "Guid": [{"id": f"tmdb://{tmdb_id}"} for tmdb_id in item["ids"]],
                }
                for rating_key, item in sorted(self.items.items())
                if item["listed"]
            ]
            start = int(params.get("X-Plex-Container-Start", 0))
            size = int(params.get("X-Plex-Container-Size", 100))
            return {
                "MediaContainer": {
                    "totalSize": len(items),
                    "Metadata": items[start : start + size],
                }
            }
        if "metadata/matches" in url:
            tmdb_id = int(params["guid"].split("://")[1])
            if tmdb_id in self.unmatched:
                return {"MediaContainer": {"Metadata": []}}
            return {
                "MediaContainer": {
                    "Metadata": [{"guid": f"plex://movie/{self._key_for(tmdb_id)}"}]
                }
            }
        raise AssertionError(f"unexpected GET {url}")

    def put(self, path, rating_key):
        item = self.items[rating_key]
        if path == "addToWatchlist":
            self.adds.append(item["ids"][0])
            if not set(item["ids"]) & self.phantom:
                item["listed"] = True
        elif path == "removeFromWatchlist":
            self.removes.append(item["ids"][0])
            item["listed"] = False
        else:
            raise AssertionError(f"unexpected PUT {path}")


def wire(app, monkeypatch, fake):
    import app.plex_watchlist as plex_watchlist

    monkeypatch.setitem(app.config, "PLEX_TOKEN", "test-token")
    monkeypatch.setattr(plex_watchlist, "_plex_get", fake.get)
    monkeypatch.setattr(plex_watchlist, "_plex_put", fake.put)


def fitzflix_watchlist_tmdb_ids(user_id):
    return {
        tmdb_id
        for (tmdb_id,) in db.session.query(Movie.tmdb_id)
        .join(UserWatchlist, UserWatchlist.movie_id == Movie.id)
        .filter(UserWatchlist.user_id == user_id)
    }


def setup_user(app):
    user = User.query.first()
    user.plex_username = "gfitzpatrick"
    db.session.commit()
    return user.id


def test_first_run_is_a_full_union(app, monkeypatch):
    import app.plex_watchlist as plex_watchlist

    with app.app_context():
        user_id = setup_user(app)

        # Fitzflix wants B and C; Plex wants A and B; A has no local record
        movie_b = make_movie("Film B", 2000, tmdb_id=102)
        movie_c = make_movie("Film C", 2001, tmdb_id=103)
        db.session.add(UserWatchlist(user_id=user_id, movie_id=movie_b.id))
        db.session.add(UserWatchlist(user_id=user_id, movie_id=movie_c.id))
        db.session.commit()

        fake = FakePlex({101: ("Film A", 1999), 102: ("Film B", 2000)})
        wire(app, monkeypatch, fake)
        assert plex_watchlist.sync_plex_watchlist() is True

        # Union everywhere: Fitzflix gains A (record created), Plex gains C
        assert fitzflix_watchlist_tmdb_ids(user_id) == {101, 102, 103}
        assert fake.adds == [103]
        assert fake.removes == []
        created = Movie.query.filter_by(tmdb_id=101).one()
        assert created.title == "Film A"
        assert set(json.loads(app.redis.get(plex_watchlist.SNAPSHOT_KEY))) == {
            101,
            102,
            103,
        }

        # The created record heads into the standard refresh pipeline
        refreshes = [
            job
            for job in app.request_queue.jobs
            if job.func_name == "app.videos.refresh_tmdb_info"
            and job.args[1] == created.id
        ]
        assert len(refreshes) == 1

        # Idempotent: a second run changes nothing
        assert plex_watchlist.sync_plex_watchlist() is True
        assert fitzflix_watchlist_tmdb_ids(user_id) == {101, 102, 103}
        assert fake.adds == [103]


def test_incremental_reconciles_both_ways(app, monkeypatch):
    import app.plex_watchlist as plex_watchlist

    with app.app_context():
        user_id = setup_user(app)
        movie_a = make_movie("Film A", 1999, tmdb_id=101)
        movie_b = make_movie("Film B", 2000, tmdb_id=102)
        db.session.add(UserWatchlist(user_id=user_id, movie_id=movie_a.id))
        db.session.add(UserWatchlist(user_id=user_id, movie_id=movie_b.id))
        db.session.commit()
        app.redis.set(plex_watchlist.SNAPSHOT_KEY, json.dumps([101, 102, 103]))

        # Since the snapshot: Plex removed B and added D; Fitzflix
        # removed C (its row is already gone above)
        fake = FakePlex(
            {101: ("Film A", 1999), 103: ("Film C", 2001), 104: ("Film D", 2002)}
        )
        wire(app, monkeypatch, fake)
        assert plex_watchlist.sync_plex_watchlist() is True

        # Both sides converge on {A, D}
        assert fitzflix_watchlist_tmdb_ids(user_id) == {101, 104}
        assert set(fake.films) == {101, 104}
        assert fake.removes == [103]
        assert set(json.loads(app.redis.get(plex_watchlist.SNAPSHOT_KEY))) == {
            101,
            104,
        }


def test_push_failures_retry_instead_of_reading_as_removals(app, monkeypatch):
    import app.plex_watchlist as plex_watchlist

    with app.app_context():
        user_id = setup_user(app)
        movie = make_movie("Unmatchable", 2003, tmdb_id=105)
        db.session.add(UserWatchlist(user_id=user_id, movie_id=movie.id))
        db.session.commit()

        fake = FakePlex({}, unmatched={105})
        wire(app, monkeypatch, fake)
        assert plex_watchlist.sync_plex_watchlist() is True

        # Not pushed, but kept locally and left OUT of the snapshot
        assert fake.adds == []
        assert fitzflix_watchlist_tmdb_ids(user_id) == {105}
        assert json.loads(app.redis.get(plex_watchlist.SNAPSHOT_KEY)) == []

        # Next run retries the push; once matchable it lands
        fake.unmatched.clear()
        assert plex_watchlist.sync_plex_watchlist() is True
        assert fake.adds == [105]
        assert json.loads(app.redis.get(plex_watchlist.SNAPSHOT_KEY)) == [105]


def test_composite_item_syncs_by_its_tracked_id_only(app, monkeypatch):
    """A Plex item spraying several tmdb guids (The Animatrix: the
    compilation plus its segments) is represented by the id Fitzflix
    tracks — the sibling ids never pour into the watchlist."""

    import app.plex_watchlist as plex_watchlist

    with app.app_context():
        user_id = setup_user(app)
        compilation = make_movie("The Animatrix", 2003, tmdb_id=55931)
        db.session.add(UserWatchlist(user_id=user_id, movie_id=compilation.id))
        db.session.commit()

        fake = FakePlex(
            {},
            composites=[("comp1", [24357, 24358, 55931], True)],
        )
        wire(app, monkeypatch, fake)
        assert plex_watchlist.sync_plex_watchlist() is True

        # Stable: no pushes, no segment records, snapshot holds the
        # compilation alone

        assert fake.adds == []
        assert fitzflix_watchlist_tmdb_ids(user_id) == {55931}
        assert Movie.query.filter_by(tmdb_id=24357).first() is None
        assert json.loads(app.redis.get(plex_watchlist.SNAPSHOT_KEY)) == [55931]

        # And a second run still changes nothing
        assert plex_watchlist.sync_plex_watchlist() is True
        assert fitzflix_watchlist_tmdb_ids(user_id) == {55931}


def test_phantom_add_goes_unsyncable_not_removed(app, monkeypatch):
    """Plex answers 200 for adds it silently drops (Buried Loot). The
    film must stay on the Fitzflix watchlist, join the unsyncable set,
    and never read as a Plex-side removal."""

    import app.plex_watchlist as plex_watchlist

    with app.app_context():
        user_id = setup_user(app)
        movie = make_movie("Buried Loot", 1935, tmdb_id=130344)
        db.session.add(UserWatchlist(user_id=user_id, movie_id=movie.id))
        db.session.commit()

        fake = FakePlex({}, phantom={130344})
        wire(app, monkeypatch, fake)
        assert plex_watchlist.sync_plex_watchlist() is True

        # The add was attempted, verified missing, and quarantined
        assert fake.adds == [130344]
        assert fitzflix_watchlist_tmdb_ids(user_id) == {130344}
        assert json.loads(app.redis.get(plex_watchlist.SNAPSHOT_KEY)) == []
        assert app.redis.smembers(plex_watchlist.UNSYNCABLE_KEY) == {b"130344"}

        # Subsequent runs skip it without churn — no repeat add, row kept
        assert plex_watchlist.sync_plex_watchlist() is True
        assert fake.adds == [130344]
        assert fitzflix_watchlist_tmdb_ids(user_id) == {130344}


def test_tv_shows_on_the_plex_watchlist_are_ignored(app, monkeypatch):
    """A show's tmdb guid is a TMDb TV-series id — it must not become
    a bare Movie row on the Fitzflix watchlist (The Flight Attendant,
    Severance)."""

    import app.plex_watchlist as plex_watchlist

    with app.app_context():
        user_id = setup_user(app)

        fake = FakePlex(
            {101: ("Film A", 1999)},
            shows={95396: ("Severance", 2022)},
        )
        wire(app, monkeypatch, fake)
        assert plex_watchlist.sync_plex_watchlist() is True

        # Only the film syncs; the show never grows a record
        assert fitzflix_watchlist_tmdb_ids(user_id) == {101}
        assert Movie.query.filter_by(tmdb_id=95396).first() is None
        assert json.loads(app.redis.get(plex_watchlist.SNAPSHOT_KEY)) == [101]


def test_previously_leaked_shows_drop_off_without_touching_plex(app, monkeypatch):
    """A show that leaked in before the type filter (a bare Movie row
    on the watchlist, its id in the snapshot) falls off the Fitzflix
    watchlist on the next run — while the show itself stays untouched
    on the Plex side."""

    import app.plex_watchlist as plex_watchlist

    with app.app_context():
        user_id = setup_user(app)
        leaked = make_movie("Severance", 2022, tmdb_id=95396)
        db.session.add(UserWatchlist(user_id=user_id, movie_id=leaked.id))
        db.session.commit()
        app.redis.set(plex_watchlist.SNAPSHOT_KEY, json.dumps([95396]))

        fake = FakePlex({}, shows={95396: ("Severance", 2022)})
        wire(app, monkeypatch, fake)
        assert plex_watchlist.sync_plex_watchlist() is True

        # Gone from the Fitzflix watchlist, no removeFromWatchlist sent
        assert fitzflix_watchlist_tmdb_ids(user_id) == set()
        assert fake.removes == []
        assert fake.films == {95396}
        assert json.loads(app.redis.get(plex_watchlist.SNAPSHOT_KEY)) == []


def test_empty_plex_response_never_mass_removes(app, monkeypatch):
    import app.plex_watchlist as plex_watchlist

    with app.app_context():
        user_id = setup_user(app)
        movie = make_movie("Keeper", 2004, tmdb_id=106)
        db.session.add(UserWatchlist(user_id=user_id, movie_id=movie.id))
        db.session.commit()
        snapshot = list(range(200, 220)) + [106]
        app.redis.set(plex_watchlist.SNAPSHOT_KEY, json.dumps(snapshot))

        fake = FakePlex({})
        wire(app, monkeypatch, fake)
        assert plex_watchlist.sync_plex_watchlist() is True

        # Run aborted: watchlist row intact, snapshot untouched
        assert fitzflix_watchlist_tmdb_ids(user_id) == {106}
        assert json.loads(app.redis.get(plex_watchlist.SNAPSHOT_KEY)) == snapshot
