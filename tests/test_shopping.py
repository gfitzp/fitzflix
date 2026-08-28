"""The movie shopping list: liked-but-unowned movies (possible since the
Letterboxd import) appear as items to buy alongside owned upgrade
candidates."""

from app import db
from app.models import User, UserMovieReview
from app.videos import star_rating_fields
from tests.factories import make_movie, make_movie_file, make_tv_file, make_tv_series


def make_liked_review(user_id, movie, rating=None):
    review = UserMovieReview(
        user_id=user_id,
        movie_id=movie.id,
        review="",
        liked=True,
        **star_rating_fields(rating),
    )
    db.session.add(review)
    db.session.flush()
    return review


def test_liked_unowned_movie_appears_on_shopping_list(app, admin_client):
    with app.app_context():
        user_id = User.query.first().id
        wanted = make_movie("Wanted but Unowned", 1988)
        make_liked_review(user_id, wanted)

        # TV episode files carry a NULL movie_id; one NULL in the "owned
        # movies" NOT IN subquery would silently empty the liked-unowned
        # results (caught live before this regression test existed)

        series = make_tv_series("Null Trap Show")
        make_tv_file(series, 1, 1, "DVD")
        db.session.commit()

    page = admin_client.get("/shopping-list/movie").get_data(as_text=True)
    assert "Wanted but Unowned" in page
    assert "Buy on Blu-Ray" in page


def test_liked_owned_movie_is_not_duplicated(app, admin_client):
    """A liked movie that has files stays a single (upgrade) entry."""

    with app.app_context():
        user_id = User.query.first().id
        owned = make_movie("Liked and Owned", 1990)
        make_movie_file(owned, "DVD")
        make_liked_review(user_id, owned, rating=4)
        db.session.commit()

    page = admin_client.get("/shopping-list/movie").get_data(as_text=True)
    # The template renders each movie's linked title once per responsive
    # layout (two layouts), so a single listing shows the title twice
    assert page.count("Liked and Owned (1990)") == 2


def test_liked_unowned_movie_matches_shopping_search(app, admin_client):
    with app.app_context():
        user_id = User.query.first().id
        wanted = make_movie("Searchable Unowned Favorite", 1970)
        make_liked_review(user_id, wanted)
        db.session.commit()

    page = admin_client.get("/shopping-list/movie?q=Searchable+Unowned").get_data(
        as_text=True
    )
    assert "Searchable Unowned Favorite" in page


def test_liked_unowned_movie_stays_off_digital_list(app, admin_client):
    """The digital view lists owned digital-only titles; a movie with no
    files at all doesn't belong there."""

    with app.app_context():
        user_id = User.query.first().id
        wanted = make_movie("Not a Digital Copy", 1993)
        make_liked_review(user_id, wanted)
        db.session.commit()

    page = admin_client.get("/shopping-list/movie?media=digital").get_data(as_text=True)
    assert "Not a Digital Copy" not in page


def test_unliked_unowned_review_stays_off_shopping_list(app, admin_client):
    """Seen-but-unowned movies without a like are history, not wishlist."""

    with app.app_context():
        user_id = User.query.first().id
        seen = make_movie("Seen but Unliked", 1999)
        review = UserMovieReview(
            user_id=user_id,
            movie_id=seen.id,
            review="",
            liked=False,
            **star_rating_fields(3),
        )
        db.session.add(review)
        db.session.commit()

    page = admin_client.get("/shopping-list/movie").get_data(as_text=True)
    assert "Seen but Unliked" not in page


def test_shopping_titles_derive_from_filters_not_url(app, admin_client):
    """The heading comes from the active filters; a crafted ?title= query
    parameter can no longer put arbitrary text on the page."""

    page = admin_client.get("/shopping-list/movie?title=Totally+Fake+Heading").get_data(
        as_text=True
    )
    assert "Totally Fake Heading" not in page
    assert "Movies to upgrade" in page

    page = admin_client.get("/shopping-list/movie?library=criterion").get_data(
        as_text=True
    )
    assert "Criterion Collection movies to upgrade" in page

    page = admin_client.get("/shopping-list/movie?media=digital").get_data(as_text=True)
    assert "Digital downloads to get as physical media" in page

    with app.app_context():
        from app.models import RefQuality

        webrip = RefQuality.query.filter_by(quality_title="WEBRip-1080p").one().id
        hdtv = RefQuality.query.filter_by(quality_title="HDTV-720p").one().id

    page = admin_client.get(f"/shopping-list/movie?max_quality={webrip}").get_data(
        as_text=True
    )
    assert "Movies to upgrade (WEBRip-1080p quality and below)" in page

    page = admin_client.get(f"/shopping-list/movie?min_quality={hdtv}").get_data(
        as_text=True
    )
    assert "Movies to upgrade (HDTV-720p quality and above)" in page


def test_unowned_quality_gates_liked_unowned_films(app, admin_client):
    """Unowned films sit at the virtual bottom of the quality scale: on
    the list by default, excluded when the minimum rises, and alone when
    the range pins to "Not in library"."""

    with app.app_context():
        user_id = User.query.first().id
        wanted = make_movie("Unowned Gate Film", 1969)
        make_liked_review(user_id, wanted)
        owned = make_movie("Owned Gate Film", 1970)
        make_movie_file(owned, "DVD")
        db.session.commit()

        from app.models import RefQuality

        unowned_id = RefQuality.query.filter_by(quality_title="Not in library").one().id
        sdtv_id = RefQuality.query.filter_by(quality_title="SDTV").one().id

    page = admin_client.get("/shopping-list/movie").get_data(as_text=True)
    assert "Unowned Gate Film" in page

    page = admin_client.get(f"/shopping-list/movie?min_quality={sdtv_id}").get_data(
        as_text=True
    )
    assert "Unowned Gate Film" not in page
    assert "Owned Gate Film" in page

    page = admin_client.get(
        f"/shopping-list/movie?min_quality={unowned_id}&max_quality={unowned_id}"
    ).get_data(as_text=True)
    assert "Unowned Gate Film" in page
    assert "Owned Gate Film" not in page


def test_inverted_quality_range_clamps_by_preference(app, admin_client):
    """A minimum above the maximum collapses the range to the minimum
    quality only — and no longer drags unowned films along."""

    with app.app_context():
        user_id = User.query.first().id
        bluray = make_movie("Clamp Bluray Film", 1980)
        make_movie_file(bluray, "Bluray-1080p")
        dvd = make_movie("Clamp DVD Film", 1981)
        make_movie_file(dvd, "DVD")
        liked = make_movie("Clamp Unowned Film", 1982)
        make_liked_review(user_id, liked)
        db.session.commit()

        from app.models import RefQuality

        bluray_id = RefQuality.query.filter_by(quality_title="Bluray-1080p").one().id
        dvd_id = RefQuality.query.filter_by(quality_title="DVD").one().id

    page = admin_client.get(
        f"/shopping-list/movie?min_quality={bluray_id}&max_quality={dvd_id}"
    ).get_data(as_text=True)
    assert "Clamp Bluray Film" in page
    assert "Clamp DVD Film" not in page
    assert "Clamp Unowned Film" not in page


def test_not_in_library_pin_gets_descriptive_heading(app, admin_client):
    with app.app_context():
        from app.models import RefQuality

        nil_id = RefQuality.query.filter_by(quality_title="Not in library").one().id

    page = admin_client.get(
        f"/shopping-list/movie?min_quality={nil_id}&max_quality={nil_id}"
    ).get_data(as_text=True)
    assert (
        "Movies to upgrade that have been liked but aren&#39;t in the library" in page
        or ("Movies to upgrade that have been liked but aren't in the library" in page)
    )


def test_shopping_rows_carry_popover_anchor_and_live_ladder(app, admin_client):
    """Each row wears the gallery grammar: the poster link is armed for
    the popover, the details column is a /movie_states scope holding a
    blank live ladder, and the old averaged star glyphs are gone."""

    with app.app_context():
        user_id = User.query.first().id
        wanted = make_movie("Ladder Row Film", 1977)
        make_liked_review(user_id, wanted, rating=4)
        db.session.commit()
        movie_id = wanted.id

    page = admin_client.get("/shopping-list/movie").get_data(as_text=True)
    assert f'data-card-url="/movie_card?movie_id={movie_id}"' in page
    # Both responsive layouts render the scope, painted from one entry
    assert page.count(f'data-state-movie="{movie_id}"') == 2
    assert 'data-ladder-live="1"' in page
    assert f'action="/movie/{movie_id}"' in page
    assert "bi-star-fill" not in page
    assert "bi-star-half" not in page


def test_watchlist_shopping_view_groups_by_scarcity(app, admin_client):
    """#247: ?library=watchlist lists every user's watchlisted,
    unowned films grouped hardest-to-watch first — the worst case
    among a film's watchers decides its group — with owned and
    list-excluded films left out and no-availability films counting
    as unavailable."""

    from app.models import UserWatchlist
    from tests.conftest import MEMBER_EMAIL
    from tests.test_streaming import NETFLIX, plant_availability, subscribe

    subscribe(app, 8, "Netflix")
    subscribe(app, 1899, "Max", email=MEMBER_EMAIL)

    def availability(streaming=(), rent=()):
        return {
            "link": None,
            "flatrate": list(streaming),
            "ads": [],
            "rent": list(rent),
            "buy": [],
        }

    with app.app_context():
        # The user table survives across tests, and other files set
        # plex_username (which _watcher_name prefers) — pin both so
        # the name assertions hold in any test order

        admin = User.query.filter_by(admin=True).first()
        member = User.query.filter_by(email=MEMBER_EMAIL).one()
        admin.plex_username = None
        member.plex_username = None
        admin_id, member_id = admin.id, member.id

        # Watched by both: streams for the admin (Netflix), but the
        # member (Max only) can neither stream nor rent it — the worst
        # case wins, so it files under unavailable

        split = make_movie("Split Verdict", 1960, tmdb_id=9901)
        rentable = make_movie("Rent Me", 1961, tmdb_id=9902)
        streamable = make_movie("Stream Me", 1962, tmdb_id=9903)
        owned = make_movie("Owned Already", 1963, tmdb_id=9904)
        make_movie_file(owned, "Bluray-1080p")
        excluded = make_movie("Waved Off", 1964, tmdb_id=9905)
        excluded.shopping_list_exclude = True
        mystery = make_movie("No Data", 1965)
        db.session.commit()
        for user_id, movie in (
            (admin_id, split),
            (member_id, split),
            (admin_id, rentable),
            (admin_id, streamable),
            (admin_id, owned),
            (admin_id, excluded),
            (member_id, mystery),
        ):
            db.session.add(UserWatchlist(user_id=user_id, movie_id=movie.id))
        db.session.commit()

    plant_availability(app, 9901, availability(streaming=[NETFLIX]))
    plant_availability(app, 9902, availability(rent=[NETFLIX]))
    plant_availability(app, 9903, availability(streaming=[NETFLIX]))
    plant_availability(app, 9904, availability(streaming=[NETFLIX]))
    plant_availability(app, 9905, availability(streaming=[NETFLIX]))

    page = admin_client.get("/shopping-list/movie?library=watchlist").get_data(
        as_text=True
    )
    assert "Watchlisted movies to buy" in page

    # Group membership reads off the section ordering; the split film
    # and the no-data film land in unavailable, and the film watched
    # by two sorts ahead of the film watched by one

    unavailable_at = page.index("Not available to stream or rent")
    rent_at = page.index("Available to rent")
    streaming_at = page.index("Streaming on subscribed services")
    assert unavailable_at < page.index("Split Verdict") < rent_at
    assert unavailable_at < page.index("No Data (1965)") < rent_at
    assert page.index("Split Verdict") < page.index("No Data (1965)")
    assert rent_at < page.index("Rent Me") < streaming_at
    assert streaming_at < page.index("Stream Me")
    assert "Owned Already" not in page
    assert "Waved Off" not in page

    # A film whose watchers disagree names each with their own state

    assert "admin (streaming)" in page
    assert "member (unavailable)" in page
    assert "Buy on Blu-Ray" in page


def test_watchlist_view_leaves_default_list_alone(app, admin_client):
    """The default shopping list is untouched; the watchlist view is
    reachable through the nav item and the filter radio."""

    page = admin_client.get("/shopping-list/movie").get_data(as_text=True)
    assert "Not available to stream or rent" not in page
    assert 'href="/shopping-list/movie?library=watchlist"' in page
    assert "Watchlisted films not in the library" in page
