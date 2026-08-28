"""The shopping lists (the routes.py split): upgrade-worthy movies and
TV seasons, with exclusions and store links."""

import re


from flask import (
    current_app,
    render_template,
    flash,
    redirect,
    url_for,
    request,
)

# flask.Markup was removed in Flask 2.4; import from its actual home
from flask_login import current_user, login_required

from app import db
from app.main.forms import (
    LibrarySearchForm,
    MovieShoppingExcludeForm,
    MovieShoppingFilterForm,
    TVShoppingFilterForm,
)
from app.models import (
    File,
    Movie,
    RefQuality,
    TVSeries,
    User,
    UserMovieReview,
    UserWatchlist,
    movie_file_rank,
)
from app.streaming import (
    batch_title_availability,
    rental_matches,
    streaming_matches,
    user_provider_ids,
)
from app.main import bp

# The watchlist view's scarcity order (#247, Glenn's ranking): a film
# nobody's services carry can only be watched by buying it, so it
# outranks one that's rentable, which outranks one already streaming.
# A film's group is its WORST case across everyone watching it — if
# it's unavailable to one watcher, unavailable wins.

WATCHLIST_SCARCITY = {"streaming": 1, "rent": 2, "unavailable": 3}

WATCHLIST_GROUPS = (
    ("unavailable", "Not available to stream or rent"),
    ("rent", "Available to rent"),
    ("streaming", "Streaming on subscribed services"),
)


def _watcher_name(user):
    """A short display handle for a watcher — there's no name column,
    so the Plex username or the email's mailbox part stands in."""

    return user.plex_username or user.email.split("@")[0]


def watchlist_shopping_groups():
    """The shopping list's watchlist view (#247): every film on ANY
    user's watchlist with no local copy and no shopping-list
    exclusion, grouped hardest-to-watch first. Availability answers
    from the cache the nightly refresh keeps full (fetch_limit=0);
    a film with no cached payload — or no TMDB id at all — counts
    as unavailable, since nothing says otherwise. Within a group:
    the most-watched films first, then title."""

    watch_rows = (
        db.session.query(UserWatchlist.movie_id, User)
        .join(User, User.id == UserWatchlist.user_id)
        .join(Movie, Movie.id == UserWatchlist.movie_id)
        .filter(~Movie.files.any(File.feature_type_id.is_(None)))
        .filter(
            db.or_(
                Movie.shopping_list_exclude == False,
                Movie.shopping_list_exclude == None,
            )
        )
        .all()
    )
    watchers = {}
    for movie_id, user in watch_rows:
        watchers.setdefault(movie_id, []).append(user)
    if not watchers:
        return [
            {"state": state, "heading": heading, "rows": []}
            for state, heading in WATCHLIST_GROUPS
        ]

    movies = {
        movie.id: movie for movie in Movie.query.filter(Movie.id.in_(sorted(watchers)))
    }
    availability, _ = batch_title_availability(
        [movie.tmdb_id for movie in movies.values() if movie.tmdb_id],
        fetch_limit=0,
    )

    provider_sets = {}
    entries = []
    for movie_id, users in watchers.items():
        movie = movies.get(movie_id)
        if movie is None:
            continue
        payload = availability.get(movie.tmdb_id) if movie.tmdb_id else None
        states = []
        for user in sorted(users, key=_watcher_name):
            if user.id not in provider_sets:
                provider_sets[user.id] = user_provider_ids(user)
            provider_ids = provider_sets[user.id]
            if streaming_matches(payload, provider_ids):
                state = "streaming"
            elif rental_matches(payload, provider_ids):
                state = "rent"
            else:
                state = "unavailable"
            states.append((user, state))
        rank = max(WATCHLIST_SCARCITY[state] for _, state in states)
        mixed = len({state for _, state in states}) > 1
        entries.append(
            {
                "movie": movie,
                "rank": rank,
                "watchers": ", ".join(
                    f"{_watcher_name(user)} ({state})" if mixed else _watcher_name(user)
                    for user, state in states
                ),
                "watcher_count": len(states),
                "instruction": (
                    "Buy Criterion edition"
                    if (movie.criterion_spine_number or movie.criterion_set_title)
                    else "Buy on Blu-Ray"
                ),
            }
        )

    entries.sort(
        key=lambda entry: (
            -entry["rank"],
            -entry["watcher_count"],
            re.sub(
                r"^(The|A|An) ",
                "",
                entry["movie"].tmdb_title or entry["movie"].title or "",
            ).lower(),
        )
    )
    return [
        {
            "state": state,
            "heading": heading,
            "rows": [
                entry for entry in entries if entry["rank"] == WATCHLIST_SCARCITY[state]
            ],
        }
        for state, heading in WATCHLIST_GROUPS
    ]


@bp.route("/shopping-list/movie", methods=["GET", "POST"])
@login_required
def movie_shopping():
    """Show instructions on how to improve the quality of each movie in the library.

    Possible user queries:
    - q          : filter the movie list for only the films that contain this substring
    - min_quality: show all movies where the best quality is at least this good
                   (defaults to "Unknown")
    - max_quality: show all movies where the best quality is *below* this threshold
                   (defaults to "Bluray-2160p Remux")
    """

    page = request.args.get("page", 1, type=int)
    q = request.args.get("q", None, type=str)
    library = request.args.get("library", None, type=str)
    media = request.args.get("media", None, type=str)
    min_quality = request.args.get("min_quality", 0, type=str)
    max_quality = request.args.get(
        "max_quality",
        db.session.query(RefQuality.id)
        .filter(RefQuality.quality_title == "Bluray-2160p Remux")
        .scalar(),
        type=str,
    )

    # The page heading is derived from the active filters (below, once the
    # quality bounds are normalized) rather than read from a ?title= query
    # parameter — the old approach let any crafted URL put arbitrary text
    # in the heading

    # Form to filter the shopping list by Criterion release or quality

    filter_form = MovieShoppingFilterForm()
    if library == "criterion":
        criterion_release = True
        filter_form.filter_status.default = "criterion"

    elif library == "watchlist":
        criterion_release = None
        filter_form.filter_status.default = "watchlist"

    else:
        criterion_release = None
        filter_form.filter_status.default = "all"

    if media == "digital":
        filter_form.media.default = "digital"

    else:
        filter_form.media.default = "all"

    # Create the list of qualities for the dropdown filter

    qualities = (
        db.session.query(RefQuality.id, RefQuality.quality_title)
        .order_by(RefQuality.preference.asc())
        .all()
    )
    filter_form.min_quality.choices = [(str(id), title) for (id, title) in qualities]
    filter_form.max_quality.choices = [(str(id), title) for (id, title) in qualities]

    # If the min_quality ID doesn't exist in our RefQuality table, default
    # to "Not in library" — the virtual bottom of the scale, so the default view
    # includes liked-but-unowned films

    if not RefQuality.query.filter_by(id=int(min_quality)).first():
        min_quality = int(
            db.session.query(RefQuality.id)
            .filter(RefQuality.quality_title == "Not in library")
            .scalar()
        )

    # If the max_quality ID doesn't exist in our RefQuality table, default to "Bluray-1080p"

    if not RefQuality.query.filter_by(id=int(max_quality)).first():
        max_quality = int(
            db.session.query(RefQuality.id)
            .filter(RefQuality.quality_title == "Bluray-1080p")
            .filter(RefQuality.physical_media == True)
            .scalar()
        )

    # Find the preference associated with the quality ID, and set as the dropdown default

    min_preference = (
        db.session.query(RefQuality.preference).filter_by(id=int(min_quality)).scalar()
    )
    max_preference = (
        db.session.query(RefQuality.preference).filter_by(id=int(max_quality)).scalar()
    )

    # If the minimum quality outranks the maximum, collapse the range to
    # just the minimum. Compared by preference — quality ids don't
    # reliably follow quality order

    if min_preference > max_preference:
        max_quality = int(min_quality)
        max_preference = min_preference

    filter_form.min_quality.default = min_quality
    filter_form.max_quality.default = max_quality

    # Derive the heading from the filter state; the search branches below
    # override it with their own more specific titles

    if library == "criterion":
        title = "Criterion Collection movies to upgrade"
    elif media == "digital":
        title = "Digital downloads to get as physical media"
    else:
        title = "Movies to upgrade"

    not_in_library_quality = bottom_quality = (
        db.session.query(RefQuality.preference)
        .filter(RefQuality.quality_title == "Not in library")
        .scalar()
    )
    top_quality = (
        db.session.query(RefQuality.preference)
        .filter(RefQuality.quality_title == "Bluray-2160p Remux")
        .scalar()
    )
    min_quality_title = (
        db.session.query(RefQuality.quality_title)
        .filter_by(id=int(min_quality))
        .scalar()
    )
    max_quality_title = (
        db.session.query(RefQuality.quality_title)
        .filter_by(id=int(max_quality))
        .scalar()
    )
    if min_quality_title == max_quality_title:
        # Equal titles mean equal preferences, so testing one bound suffices
        if min_preference == not_in_library_quality:
            title = f"{title} that have been liked but aren't in the library"
        else:
            title = f"{title} ({min_quality_title} quality)"
    elif min_preference > bottom_quality and max_preference < top_quality:
        title = f"{title} (between {min_quality_title} and {max_quality_title} quality)"
    elif max_preference < top_quality:
        title = f"{title} ({max_quality_title} quality and below)"
    elif min_preference > bottom_quality:
        title = f"{title} ({min_quality_title} quality and above)"

    # Form to filter the shopping list by a particular substring

    library_search_form = LibrarySearchForm()
    if filter_form.validate_on_submit():
        return redirect(
            url_for(
                "main.movie_shopping",
                library=filter_form.filter_status.data,
                media=filter_form.media.data,
                min_quality=filter_form.min_quality.data,
                max_quality=filter_form.max_quality.data,
                q=q,
            )
        )

    # Apply the changes to the filter form
    # (not sure why this has to go at this point in the code, but putting it elsewhere
    #  didn't work **shrug emoji**)

    filter_form.process()

    if (
        library_search_form.search_submit.data
        and library_search_form.validate_on_submit()
    ):
        return redirect(
            url_for(
                "main.movie_shopping",
                library=library,
                media=media,
                min_quality=min_quality,
                max_quality=max_quality,
                q=library_search_form.search_query.data,
            )
        )

    # Subquery to get the best movie titles

    ranked_files = (
        db.session.query(
            File.id.label("file_id"),
            Movie.id.label("movie_id"),
            Movie.title,
            File.edition,
            RefQuality.quality_title,
            movie_file_rank(),
        )
        .join(Movie, (Movie.id == File.movie_id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .subquery()
    )

    # Subquery to get only physical-media movies

    physical_media = (
        db.session.query(Movie.id)
        .join(File, (File.movie_id == Movie.id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .filter(RefQuality.physical_media == True)
        .filter(File.feature_type_id == None)
        .subquery()
    )

    # Subquery to get the current user's average ratings for each movie.
    # The math on modified_rating, whole_stars, and half_stars mirrors what
    # is done when creating a review, computed here over the *average*. The
    # page no longer draws these columns — each row carries the live star
    # ladder, painted from /movie_states with the latest verdict — but they
    # still ride the row tuple the template unpacks.

    rating = (
        db.session.query(
            UserMovieReview.user_id,
            UserMovieReview.movie_id,
            db.func.avg(UserMovieReview.rating).label("rating"),
            (db.func.round(db.func.avg(UserMovieReview.rating) * 2) / 2).label(
                "modified_rating"
            ),
            db.func.floor(
                db.func.round(db.func.avg(UserMovieReview.rating) * 2) / 2
            ).label("whole_stars"),
            db.case(
                (
                    db.func.mod(
                        (db.func.round(db.func.avg(UserMovieReview.rating) * 2) / 2),
                        1,
                    )
                    == 0,
                    0,
                ),
                else_=(1),
            ).label("half_stars"),
        )
        .group_by(UserMovieReview.user_id, UserMovieReview.movie_id)
        .subquery()
    )

    # Subqueries to get the preference associated with different quality thresholds

    dvd_quality = (
        db.session.query(RefQuality.preference)
        .filter(RefQuality.quality_title == "DVD")
        .scalar()
    )
    bluray_quality = (
        db.session.query(db.func.min(RefQuality.preference))
        .filter(RefQuality.quality_title.like("Bluray-1080%"))
        .filter(RefQuality.physical_media == True)
        .scalar()
    )
    uhd_quality = (
        db.session.query(db.func.min(RefQuality.preference))
        .filter(RefQuality.quality_title.like("Bluray-2160%"))
        .filter(RefQuality.physical_media == True)
        .scalar()
    )

    CriterionQuality = db.aliased(RefQuality)

    # These CASE expressions are shared by every shopping query variant

    shopping_instruction_case = db.case(
        (Movie.shopping_list_exclude == True, "Already owned"),
        (
            db.and_(
                db.or_(
                    Movie.criterion_spine_number != None,
                    Movie.criterion_set_title != None,
                ),
                Movie.criterion_disc_owned == True,
                #                                 db.and_(
                #                                     db.or_(
                #                                         db.and_(
                #                                             CriterionQuality.preference == dvd_quality,
                #                                             RefQuality.preference >= dvd_quality,
                #                                         ),
                #                                         db.and_(
                #                                             CriterionQuality.preference
                #                                             >= bluray_quality,
                #                                             RefQuality.preference >= bluray_quality,
                #                                         ),
                #                                     ),
                #                                 ),
            ),
            "Already owned",
        ),
        (
            db.and_(
                db.or_(
                    Movie.criterion_spine_number != None,
                    Movie.criterion_set_title != None,
                ),
                Movie.criterion_disc_owned == False,
                CriterionQuality.preference == uhd_quality,
                # RefQuality.preference <= uhd_quality,
            ),
            "Buy Criterion edition on 4K UHD Blu-Ray",
        ),
        (
            db.and_(
                db.or_(
                    Movie.criterion_spine_number != None,
                    Movie.criterion_set_title != None,
                ),
                Movie.criterion_disc_owned == False,
                CriterionQuality.preference == bluray_quality,
                # RefQuality.preference <= bluray_quality,
            ),
            "Buy Criterion edition on Blu-Ray",
        ),
        (
            db.and_(
                db.or_(
                    Movie.criterion_spine_number != None,
                    Movie.criterion_set_title != None,
                ),
                Movie.criterion_disc_owned == False,
                CriterionQuality.preference == dvd_quality,
                # RefQuality.preference <= dvd_quality,
            ),
            "Buy Criterion edition on DVD",
        ),
        # A liked movie with no files (possible since the Letterboxd
        # import) is wanted but entirely unowned
        (File.id == None, "Buy on Blu-Ray"),
        (File.fullscreen == True, "Buy any non-fullscreen release"),
        (
            RefQuality.preference < dvd_quality,
            "Buy on DVD or Blu-Ray",
        ),
        (RefQuality.preference < bluray_quality, "Buy on Blu-Ray"),
        else_=("Already owned"),
    )

    shopping_urgency_order_case = db.case(
        (Movie.criterion_disc_owned == True, -1),
        (Movie.shopping_list_exclude == True, -1),
        (File.id == None, 1),
        (RefQuality.preference < bluray_quality, 1),
        (
            db.and_(
                db.or_(
                    Movie.criterion_spine_number != None,
                    Movie.criterion_set_title != None,
                ),
                db.or_(
                    Movie.criterion_disc_owned == False,
                    Movie.criterion_disc_owned == None,
                ),
            ),
            1,
        ),
        else_=(-1),
    )

    cart_priority_order_case = db.case(
        (Movie.criterion_disc_owned == True, 0),
        (
            db.and_(
                File.id == None,
                db.or_(
                    Movie.shopping_list_exclude == False,
                    Movie.shopping_list_exclude == None,
                ),
            ),
            Movie.shopping_cart_priority,
        ),
        (
            db.and_(
                db.or_(
                    Movie.shopping_list_exclude == False,
                    Movie.shopping_list_exclude == None,
                ),
                RefQuality.preference < bluray_quality,
            ),
            Movie.shopping_cart_priority,
        ),
        (
            db.and_(
                db.or_(
                    Movie.criterion_spine_number != None,
                    Movie.criterion_set_title != None,
                ),
                db.or_(
                    Movie.criterion_disc_owned == False,
                    Movie.criterion_disc_owned == None,
                ),
            ),
            Movie.shopping_cart_priority,
        ),
        else_=(0),
    )

    quality_order_case = db.case(
        (Movie.criterion_disc_owned == True, 99),
        # Nothing owned at all sorts ahead of even the worst owned quality
        (
            db.and_(
                File.id == None,
                db.or_(
                    Movie.shopping_list_exclude == False,
                    Movie.shopping_list_exclude == None,
                ),
            ),
            0,
        ),
        (
            db.and_(
                db.or_(
                    Movie.shopping_list_exclude == False,
                    Movie.shopping_list_exclude == None,
                ),
                RefQuality.preference < bluray_quality,
            ),
            RefQuality.preference,
        ),
        (
            db.and_(
                db.or_(
                    Movie.criterion_spine_number != None,
                    Movie.criterion_set_title != None,
                ),
                db.or_(
                    Movie.criterion_disc_owned == False,
                    Movie.criterion_disc_owned == None,
                ),
            ),
            RefQuality.preference,
        ),
        else_=(99),
    )

    cart_age_order_case = db.case(
        (Movie.criterion_disc_owned == True, 0),
        (
            db.and_(
                File.id == None,
                db.or_(
                    Movie.shopping_list_exclude == False,
                    Movie.shopping_list_exclude == None,
                ),
            ),
            Movie.shopping_cart_add_date,
        ),
        (
            db.and_(
                db.or_(
                    Movie.shopping_list_exclude == False,
                    Movie.shopping_list_exclude == None,
                ),
                RefQuality.preference < bluray_quality,
            ),
            Movie.shopping_cart_add_date,
        ),
        (
            db.and_(
                db.or_(
                    Movie.criterion_spine_number != None,
                    Movie.criterion_set_title != None,
                ),
                db.or_(
                    Movie.criterion_disc_owned == False,
                    Movie.criterion_disc_owned == None,
                ),
            ),
            Movie.shopping_cart_add_date,
        ),
        else_=(0),
    )

    # Movies with a liked review but no files (possible since the
    # Letterboxd import) belong on the shopping list too: they're wanted
    # films not owned in any form. Selecting File and RefQuality through
    # always-false outer joins yields NULL columns for them, so these rows
    # take the same shape as owned titles and can be UNIONed in below.

    liked_movie_ids = db.session.query(UserMovieReview.movie_id).filter(
        UserMovieReview.user_id == int(current_user.id),
        UserMovieReview.liked == True,
    )

    # TV episode files carry a NULL movie_id, and a single NULL in a NOT IN
    # subquery makes the predicate false for every row — filter them out

    owned_movie_ids = db.session.query(File.movie_id).filter(
        File.feature_type_id == None, File.movie_id != None
    )

    def liked_unowned_query():
        return (
            db.session.query(
                File,
                Movie,
                RefQuality,
                rating.c.rating,
                rating.c.modified_rating,
                rating.c.whole_stars,
                rating.c.half_stars,
                shopping_instruction_case.label("instruction"),
            )
            .select_from(Movie)
            .outerjoin(File, db.and_(File.movie_id == Movie.id, File.id == None))
            .outerjoin(RefQuality, RefQuality.id == File.quality_id)
            .outerjoin(
                CriterionQuality, (CriterionQuality.id == Movie.criterion_quality_id)
            )
            .outerjoin(
                rating,
                (rating.c.movie_id == Movie.id) & (rating.c.user_id == current_user.id),
            )
            .filter(Movie.id.in_(liked_movie_ids))
            .filter(Movie.id.not_in(owned_movie_ids))
        )

    watchlist_groups = None

    if q:
        if re.match(r"tmdb:(?P<tmdb_id>\d+)", q):
            tmdb_id = re.match(r"tmdb:(?P<tmdb_id>\d+)", q).group(1)
            movie = Movie.query.filter_by(tmdb_id=int(tmdb_id)).first()
            if not movie:
                title = f"Upgrade details for TMDB ID {tmdb_id}"
            else:
                title = f"Upgrade details for \"{movie.tmdb_title if movie.tmdb_title else movie.title} ({movie.tmdb_release_date.strftime('%Y') if movie.tmdb_title else movie.year})\""
            movies = (
                db.session.query(
                    File,
                    Movie,
                    RefQuality,
                    rating.c.rating,
                    rating.c.modified_rating,
                    rating.c.whole_stars,
                    rating.c.half_stars,
                    shopping_instruction_case.label("instruction"),
                )
                .join(Movie, (Movie.id == File.movie_id))
                .join(RefQuality, (RefQuality.id == File.quality_id))
                .outerjoin(
                    CriterionQuality,
                    (CriterionQuality.id == Movie.criterion_quality_id),
                )
                .outerjoin(
                    rating,
                    (rating.c.movie_id == Movie.id)
                    & (rating.c.user_id == current_user.id),
                )
                .join(ranked_files, (ranked_files.c.file_id == File.id))
                .filter(File.feature_type_id == None)
                .filter(ranked_files.c.rank == 1)
                .filter(RefQuality.preference >= min_preference)
                .filter(RefQuality.preference <= max_preference)
                .filter(Movie.tmdb_id == tmdb_id)
                .order_by(
                    db.func.regexp_replace(Movie.title, "^(The|A|An) ", "").asc(),
                    Movie.year.asc(),
                    File.edition.asc(),
                    RefQuality.preference.asc(),
                    File.date_added.asc(),
                )
                .paginate(page=page, per_page=100, error_out=False)
            )

        else:
            title = f"Movies to upgrade matching '{q}'"
            owned_matches = (
                db.session.query(
                    File,
                    Movie,
                    RefQuality,
                    rating.c.rating,
                    rating.c.modified_rating,
                    rating.c.whole_stars,
                    rating.c.half_stars,
                    shopping_instruction_case.label("instruction"),
                )
                .join(Movie, (Movie.id == File.movie_id))
                .join(RefQuality, (RefQuality.id == File.quality_id))
                .outerjoin(
                    CriterionQuality,
                    (CriterionQuality.id == Movie.criterion_quality_id),
                )
                .outerjoin(
                    rating,
                    (rating.c.movie_id == Movie.id)
                    & (rating.c.user_id == current_user.id),
                )
                .join(ranked_files, (ranked_files.c.file_id == File.id))
                .filter(File.feature_type_id == None)
                .filter(ranked_files.c.rank == 1)
                .filter(RefQuality.preference >= min_preference)
                .filter(RefQuality.preference <= max_preference)
                .filter(
                    db.or_(
                        Movie.title.ilike(f"%{q}%"), Movie.tmdb_title.ilike(f"%{q}%")
                    )
                )
            )
            liked_matches = liked_unowned_query().filter(
                db.or_(Movie.title.ilike(f"%{q}%"), Movie.tmdb_title.ilike(f"%{q}%"))
            )

            # Films with no local copy count as the virtual bottom quality, so they
            # only appear when the range's minimum reaches down to "Not in library"

            candidates = (
                owned_matches.union_all(liked_matches)
                if min_preference <= bottom_quality
                else owned_matches
            )
            movies = candidates.order_by(
                db.func.regexp_replace(Movie.title, "^(The|A|An) ", "").asc(),
                Movie.year.asc(),
                File.edition.asc(),
                RefQuality.preference.asc(),
                File.date_added.asc(),
            ).paginate(page=page, per_page=100, error_out=False)

    elif library == "watchlist":
        # The watchlist view (#247): cross-user and availability-
        # ranked, so it's built in Python from the availability cache
        # rather than in the quality-driven SQL below. Unpaginated on
        # purpose, like the watchlist page itself — the group
        # structure is the navigation

        title = "Watchlisted movies to buy"
        watchlist_groups = watchlist_shopping_groups()
        movies = None

    elif media == "digital":
        physical_media = (
            db.session.query(
                File.movie_id,
            )
            .join(Movie, (Movie.id == File.movie_id))
            .join(RefQuality, (RefQuality.id == File.quality_id))
            .filter(
                db.or_(
                    RefQuality.physical_media == True,
                    RefQuality.quality_title == "SDTV",
                    RefQuality.quality_title.ilike("HDTV-%"),
                )
            )
            .subquery()
        )

        movies = (
            db.session.query(
                File,
                Movie,
                RefQuality,
                rating.c.rating,
                rating.c.modified_rating,
                rating.c.whole_stars,
                rating.c.half_stars,
                shopping_instruction_case.label("instruction"),
            )
            .join(Movie, (Movie.id == File.movie_id))
            .join(RefQuality, (RefQuality.id == File.quality_id))
            .outerjoin(
                CriterionQuality, (CriterionQuality.id == Movie.criterion_quality_id)
            )
            .outerjoin(
                rating,
                (rating.c.movie_id == Movie.id) & (rating.c.user_id == current_user.id),
            )
            .join(ranked_files, (ranked_files.c.file_id == File.id))
            .filter(File.feature_type_id == None)
            .filter(ranked_files.c.rank == 1)
            .filter(RefQuality.preference >= min_preference)
            .filter(RefQuality.preference <= max_preference)
            .filter(Movie.id.not_in(db.select(physical_media.c.movie_id)))
            .filter(
                db.or_(
                    db.and_(
                        criterion_release == True,
                        db.or_(
                            Movie.criterion_spine_number != None,
                            Movie.criterion_set_title != None,
                        ),
                        # Movie.criterion_in_print == 1,
                        # CriterionQuality.preference >= RefQuality.preference,
                    ),
                    criterion_release != True,
                ),
            )
            .order_by(
                shopping_urgency_order_case.desc(),
                cart_priority_order_case.desc(),
                quality_order_case.asc(),
                cart_age_order_case.desc(),
                db.func.regexp_replace(Movie.title, "^(The|A|An) ", "").asc(),
                Movie.year.asc(),
                File.edition.asc(),
                File.date_added.asc(),
            )
            .paginate(page=page, per_page=100, error_out=False)
        )

    else:
        owned_titles = (
            db.session.query(
                File,
                Movie,
                RefQuality,
                rating.c.rating,
                rating.c.modified_rating,
                rating.c.whole_stars,
                rating.c.half_stars,
                shopping_instruction_case.label("instruction"),
            )
            .join(Movie, (Movie.id == File.movie_id))
            .join(RefQuality, (RefQuality.id == File.quality_id))
            .outerjoin(
                CriterionQuality, (CriterionQuality.id == Movie.criterion_quality_id)
            )
            .outerjoin(
                rating,
                (rating.c.movie_id == Movie.id) & (rating.c.user_id == current_user.id),
            )
            .join(ranked_files, (ranked_files.c.file_id == File.id))
            .filter(File.feature_type_id == None)
            .filter(ranked_files.c.rank == 1)
            .filter(RefQuality.preference >= min_preference)
            .filter(RefQuality.preference <= max_preference)
            .filter(
                db.or_(
                    db.and_(
                        criterion_release == True,
                        db.or_(
                            Movie.criterion_spine_number != None,
                            Movie.criterion_set_title != None,
                        ),
                        # Movie.criterion_in_print == 1,
                        # CriterionQuality.preference >= RefQuality.preference,
                    ),
                    criterion_release != True,
                ),
            )
        )
        liked_titles = liked_unowned_query().filter(
            db.or_(
                db.and_(
                    criterion_release == True,
                    db.or_(
                        Movie.criterion_spine_number != None,
                        Movie.criterion_set_title != None,
                    ),
                ),
                criterion_release != True,
            ),
        )
        # Films with no local copy count as the virtual bottom quality, so they only
        # appear when the range's minimum reaches down to "Not in library"

        candidates = (
            owned_titles.union_all(liked_titles)
            if min_preference <= bottom_quality
            else owned_titles
        )
        movies = candidates.order_by(
            shopping_urgency_order_case.desc(),
            cart_priority_order_case.desc(),
            quality_order_case.asc(),
            cart_age_order_case.desc(),
            db.func.regexp_replace(Movie.title, "^(The|A|An) ", "").asc(),
            Movie.year.asc(),
            File.edition.asc(),
            File.date_added.asc(),
        ).paginate(page=page, per_page=100, error_out=False)

    movie_shopping_exclude_form = MovieShoppingExcludeForm()

    next_url = (
        url_for(
            "main.movie_shopping",
            page=movies.next_num,
            q=q,
            media=media,
            library=library,
            min_quality=min_quality,
            max_quality=max_quality,
        )
        if movies is not None and movies.has_next
        else None
    )
    prev_url = (
        url_for(
            "main.movie_shopping",
            page=movies.prev_num,
            q=q,
            media=media,
            library=library,
            min_quality=min_quality,
            max_quality=max_quality,
        )
        if movies is not None and movies.has_prev
        else None
    )

    if (
        movie_shopping_exclude_form.add_submit.data
        and movie_shopping_exclude_form.validate_on_submit()
    ):
        movie = Movie.query.filter_by(
            id=int(movie_shopping_exclude_form.movie_id.data)
        ).first()
        movie.shopping_list_exclude = None
        db.session.commit()
        flash(f"Added '{movie.title}' to the shopping list")
        return redirect(
            url_for(
                "main.movie_shopping",
                page=page,
                q=q,
                library=library,
                media=media,
                min_quality=min_quality,
                max_quality=max_quality,
            ),
        )

    elif (
        movie_shopping_exclude_form.exclude_submit.data
        and movie_shopping_exclude_form.validate_on_submit()
    ):
        movie = Movie.query.filter_by(
            id=int(movie_shopping_exclude_form.movie_id.data)
        ).first()
        movie.shopping_list_exclude = 1
        db.session.commit()
        flash(f"Removed '{movie.title}' from the shopping list")
        return redirect(
            url_for(
                "main.movie_shopping",
                page=page,
                q=q,
                library=library,
                media=media,
                min_quality=min_quality,
                max_quality=max_quality,
            ),
        )

    return render_template(
        "shopping_movie.html",
        title=title,
        movies=movies.items if movies is not None else None,
        next_url=next_url,
        prev_url=prev_url,
        pages=movies,
        watchlist_groups=watchlist_groups,
        filter_form=filter_form,
        library_search_form=library_search_form,
        radarr_proxy_url=current_app.config["RADARR_PROXY_URL"],
        movie_shopping_exclude_form=movie_shopping_exclude_form,
    )


@bp.route("/shopping-list/tv", methods=["GET", "POST"])
@login_required
def tv_shopping():
    """Show instructions on how to improve the quality of each TV show season.

    Possible user queries:
    - q          : filter the list for only the tv series that contain this substring
    - min_quality: show all seasons where the worst quality is at least this good
                   (defaults to "Unknown")
    - max_quality: show all seasons where the worst quality is *below* this threshold
                   (defaults to "Bluray-1080p")
    """

    q = request.args.get("q", None, type=str)
    min_quality = request.args.get("min_quality", 0, type=str)
    max_quality = request.args.get(
        "max_quality",
        db.session.query(RefQuality.id)
        .filter(RefQuality.quality_title == "Bluray-2160p Remux")
        .scalar(),
        type=str,
    )

    # Form to filter the shopping list by quality

    filter_form = TVShoppingFilterForm()

    # Create the list of qualities for the dropdown filter

    # "Not in library" is the movie shopping list's virtual quality; TV has no
    # unowned rows, so it stays out of this dropdown

    qualities = (
        db.session.query(RefQuality.id, RefQuality.quality_title)
        .filter(RefQuality.quality_title != "Not in library")
        .order_by(RefQuality.preference.asc())
        .all()
    )
    filter_form.quality.choices = [(str(id), title) for (id, title) in qualities]

    # If the min_quality ID doesn't exist in our RefQuality table, default to "Unknown"

    if not RefQuality.query.filter_by(id=int(min_quality)).first():
        min_quality = int(
            db.session.query(RefQuality.id)
            .filter(RefQuality.quality_title == "Unknown")
            .scalar()
        )

    # If the max_quality ID doesn't exist in our RefQuality table, default to "Bluray-1080p"

    if not RefQuality.query.filter_by(id=int(max_quality)).first():
        max_quality = int(
            db.session.query(RefQuality.id)
            .filter(RefQuality.quality_title == "Bluray-1080p")
            .filter(RefQuality.physical_media == True)
            .scalar()
        )

    # Find the preference associated with the quality ID, and set as the dropdown default

    min_preference = (
        db.session.query(RefQuality.preference).filter_by(id=int(min_quality)).scalar()
    )
    max_preference = (
        db.session.query(RefQuality.preference).filter_by(id=int(max_quality)).scalar()
    )

    # If the minimum quality outranks the maximum, collapse the range to
    # the maximum. Compared by preference — quality ids don't reliably
    # follow quality order

    if min_preference > max_preference:
        min_quality = int(max_quality)
        min_preference = max_preference

    filter_form.quality.default = max_quality

    # Form to filter the shopping list by a particular substring

    library_search_form = LibrarySearchForm()
    if filter_form.validate_on_submit():
        return redirect(
            url_for("main.tv_shopping", max_quality=filter_form.quality.data, q=q)
        )

    # Apply the changes to the filter form
    # (not sure why this has to go at this point in the code, but putting it elsewhere
    #  didn't work **shrug emoji**)

    filter_form.process()

    if (
        library_search_form.search_submit.data
        and library_search_form.validate_on_submit()
    ):
        return redirect(
            url_for(
                "main.tv_shopping",
                max_quality=max_quality,
                q=library_search_form.search_query.data,
            )
        )

    # Subqueries to get the preference associated with different quality thresholds

    dvd_quality = (
        db.session.query(RefQuality.preference)
        .filter(RefQuality.quality_title == "DVD")
        .scalar()
    )
    bluray_quality = (
        db.session.query(db.func.min(RefQuality.preference))
        .filter(RefQuality.quality_title.like("Bluray-1080%"))
        .filter(RefQuality.physical_media == True)
        .scalar()
    )

    # Subquery to get the worst quality for each tv show season

    subquery = (
        db.session.query(
            File.series_id,
            File.season,
            db.func.count(db.func.distinct(File.episode)).label("episodes"),
            db.func.min(RefQuality.preference).label("preference"),
        )
        .group_by(File.series_id, File.season)
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .subquery()
    )

    # Run the season aggregate once for the whole library and bucket the rows
    # by series, rather than re-running the subquery once per series

    season_rows = (
        db.session.query(
            subquery.c.series_id,
            subquery.c.season,
            subquery.c.episodes,
            RefQuality.quality_title,
            db.case(
                (RefQuality.preference < dvd_quality, "Buy on DVD or Blu-Ray"),
                (RefQuality.preference < bluray_quality, "Buy on Blu-Ray"),
                else_="Already owned",
            ).label("instruction"),
        )
        .join(RefQuality, (RefQuality.preference == subquery.c.preference))
        .filter(RefQuality.preference >= min_preference)
        .filter(RefQuality.preference <= max_preference)
        .order_by(
            subquery.c.series_id,
            db.case((subquery.c.season == 0, 1), else_=0).asc(),
            subquery.c.season.asc(),
        )
        .all()
    )

    seasons_by_series = {}
    for series_id, season, num_episodes, min_quality, instruction in season_rows:
        seasons_by_series.setdefault(series_id, []).append(
            {
                "season": season,
                "episode_count": num_episodes,
                "min_quality": min_quality,
                "instruction": instruction,
            }
        )

    tv = []
    if q:
        title = f"TV Shows to upgrade matching '{q}'"
        q = q.replace(" ", "%")
        t = (
            TVSeries.query.filter(
                db.or_(
                    TVSeries.title.ilike(f"%{q}%"), TVSeries.tmdb_name.ilike(f"%{q}%")
                )
            )
            .order_by(db.func.regexp_replace(TVSeries.title, "^(The|A|An) ", "").asc())
            .all()
        )

    else:
        t = TVSeries.query.order_by(
            db.func.regexp_replace(TVSeries.title, "^(The|A|An) ", "").asc()
        ).all()
        title = "TV Shows to upgrade"

    for series in t:
        seasons = seasons_by_series.get(series.id, [])

        # Don't show any tv series where there aren't any seasons
        # (Needed because of the quality filter, otherwise we may show a tv series that
        #  doesn't have any seasons that reach the quality filter threshold.)

        if len(seasons) == 0:
            continue

        tv.append(
            {
                "id": series.id,
                "title": series.title,
                "tmdb_id": series.tmdb_id,
                "tmdb_name": series.tmdb_name,
                "first_air_year": (
                    series.tmdb_first_air_date.year
                    if series.tmdb_first_air_date
                    else None
                ),
                "tmdb_poster_path": series.tmdb_poster_path,
                "seasons": seasons,
            }
        )

    return render_template(
        "shopping_tv.html",
        title=title,
        filter_form=filter_form,
        library_search_form=library_search_form,
        series=tv,
    )
