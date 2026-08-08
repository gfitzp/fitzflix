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
    assert "Movies to upgrade (WEBRip-1080p and below)" in page

    page = admin_client.get(f"/shopping-list/movie?min_quality={hdtv}").get_data(
        as_text=True
    )
    assert "Movies to upgrade (HDTV-720p and above)" in page
