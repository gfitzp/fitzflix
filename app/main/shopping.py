"""Show the shopping lists. This module is the split from routes.py.

The lists show the movies and the TV seasons that are worth an upgrade,
with exclusions and store links."""

import re

from collections import namedtuple

from flask import (
    current_app,
    render_template,
    flash,
    redirect,
    url_for,
    request,
)

# Flask 2.4 removed flask.Markup. Import it from its real module.
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
    ranked_movie_files,
    sort_title,
    title_sort,
)
from app.streaming import (
    batch_title_availability,
    rental_matches,
    streaming_matches,
    user_provider_ids,
)
from app.main import bp
from app.main.helpers import decimal_id
from app.file_ranking import (
    lowest_physical_preference,
    quality_anchors,
    quality_id,
    quality_preference,
)

# This is the scarcity order of the watchlist view (#247, the ranking
# of Glenn). A film that no service of a watcher has can only be
# watched after a purchase. Thus, it outranks a rentable film. A
# rentable film outranks a film that is already streaming. The group
# of a film is its WORST case across each person who watches for it.
# If it is unavailable to 1 watcher, unavailable wins.

WATCHLIST_SCARCITY = {"streaming": 1, "rent": 2, "unavailable": 3}

WATCHLIST_GROUPS = (
    ("unavailable", "Not available to stream or rent"),
    ("rent", "Available to rent"),
    ("streaming", "Streaming on subscribed services"),
)


# A film with a Criterion release: a spine number or a set title.

HAS_CRITERION_RELEASE = db.or_(
    Movie.criterion_spine_number != None,
    Movie.criterion_set_title != None,
)

# A film that the shopping list does not exclude.

NOT_EXCLUDED = db.or_(
    Movie.shopping_list_exclude == False,
    Movie.shopping_list_exclude == None,
)

# A Criterion film whose disc is not owned.

DISC_NOT_OWNED = db.or_(
    Movie.criterion_disc_owned == False,
    Movie.criterion_disc_owned == None,
)

# The title order of the search views of the movie shopping list.

TITLE_ORDER = (
    title_sort(Movie, tmdb=False).asc(),
    Movie.year.asc(),
    File.edition.asc(),
    RefQuality.preference.asc(),
    File.date_added.asc(),
)

# The quality bounds of a shopping list, after the fallbacks and the
# collapse. bottom and top are the preferences of the ends of the scale.

QualityRange = namedtuple(
    "QualityRange",
    "min_id max_id min_preference max_preference min_title max_title bottom top",
)


def quality_range(min_arg, max_arg, min_fallback, max_fallback, collapse):
    """Return the QualityRange of the 2 quality bounds of a shopping list.

    A bound is a quality id from the query string. A value that is not a
    number, or the id of no quality, takes the quality title of its
    fallback (#306). If the minimum outranks the maximum, the range
    collapses to 1 quality. collapse "min" keeps the minimum (the movie
    list). collapse "max" keeps the maximum (the TV list). The test
    compares preferences, because the quality ids do not always follow
    the quality order."""

    tiers = {tier.id: (title, tier) for title, tier in quality_anchors().items()}

    def bound(value, fallback):
        """Return the quality id of 1 bound."""

        text = str(value)
        tier_id = int(text) if text.isdecimal() else None
        return tier_id if tier_id in tiers else quality_id(fallback)

    min_id = bound(min_arg, min_fallback)
    max_id = bound(max_arg, max_fallback)
    if tiers[min_id][1].preference > tiers[max_id][1].preference:
        if collapse == "min":
            max_id = min_id
        else:
            min_id = max_id
    return QualityRange(
        min_id,
        max_id,
        tiers[min_id][1].preference,
        tiers[max_id][1].preference,
        tiers[min_id][0],
        tiers[max_id][0],
        quality_preference("Not in library"),
        quality_preference("Bluray-2160p Remux"),
    )


def shopping_heading(qualities, library, media):
    """Return the heading of the movie shopping list from its filters.

    qualities is the QualityRange of the list. The heading never comes
    from the query string. With the old ?title= parameter, a crafted URL
    could put any text in the heading. The search views replace it with
    their own titles."""

    if library == "criterion":
        title = "Criterion Collection movies to upgrade"
    elif media == "digital":
        title = "Digital downloads to get as physical media"
    else:
        title = "Movies to upgrade"

    low, high = qualities.min_preference, qualities.max_preference
    if qualities.min_title == qualities.max_title:
        # Equal titles mean equal preferences. Thus, a test of 1 bound is
        # sufficient.
        if low == qualities.bottom:
            return f"{title} that have been liked but aren't in the library"
        return f"{title} ({qualities.min_title} quality)"
    if low > qualities.bottom and high < qualities.top:
        return (
            f"{title} (between {qualities.min_title} and "
            f"{qualities.max_title} quality)"
        )
    if high < qualities.top:
        return f"{title} ({qualities.max_title} quality and below)"
    if low > qualities.bottom:
        return f"{title} ({qualities.min_title} quality and above)"
    return title


def _watcher_name(user):
    """Return a short display handle for a watcher.

    There is no name column. Thus, the Plex username or the mailbox
    part of the email address replaces it."""

    return user.plex_username or user.email.split("@")[0]


def watchlist_shopping_groups():
    """Return the watchlist view of the shopping list (#247).

    This includes each film on the watchlist of ANY user with no local
    copy and no shopping-list exclusion. The groups show the hardest
    to watch first. The availability comes from the cache that the
    nightly refresh keeps full (fetch_limit=0). A film with no cached
    payload, or with no TMDB id, counts as unavailable, because
    nothing says otherwise. In a group, the most-watched films come
    first, then the title order."""

    watch_rows = (
        db.session.query(UserWatchlist.movie_id, User)
        .join(User, User.id == UserWatchlist.user_id)
        .join(Movie, Movie.id == UserWatchlist.movie_id)
        .filter(~Movie.files.any(File.feature_type_id.is_(None)))
        .filter(NOT_EXCLUDED)
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
            if streaming_matches(payload, provider_ids, tmdb_id=movie.tmdb_id):
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
            sort_title(entry["movie"].tmdb_title or entry["movie"].title).lower(),
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
    """Show how to improve the quality of each movie in the library.

    Possible user queries:
    - q          : show only the films that contain this substring
    - min_quality: show the movies whose best quality is at least this
                   good (default "Unknown")
    - max_quality: show the movies whose best quality is *below* this
                   limit (default "Bluray-2160p Remux")
    """

    page = request.args.get("page", 1, type=int)
    q = request.args.get("q", None, type=str)
    library = request.args.get("library", None, type=str)
    media = request.args.get("media", None, type=str)

    # The bounds of the quality range. A missing or unknown minimum is
    # "Not in library", the virtual bottom of the scale. Thus, the
    # default view includes the liked films that the user does not own.
    # An unknown maximum is "Bluray-1080p". A minimum above the maximum
    # collapses the range to the minimum only.

    qualities = quality_range(
        request.args.get("min_quality", 0, type=str),
        request.args.get("max_quality", quality_id("Bluray-2160p Remux"), type=str),
        "Not in library",
        "Bluray-1080p",
        collapse="min",
    )
    min_quality, max_quality = qualities.min_id, qualities.max_id
    min_preference, max_preference = (
        qualities.min_preference,
        qualities.max_preference,
    )
    bottom_quality = qualities.bottom
    title = shopping_heading(qualities, library, media)

    # This form filters the shopping list by Criterion release or quality.

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

    # Make the list of qualities for the dropdown filter.

    choices = [
        (str(id), quality_title)
        for (id, quality_title) in db.session.query(
            RefQuality.id, RefQuality.quality_title
        )
        .order_by(RefQuality.preference.asc())
        .all()
    ]
    filter_form.min_quality.choices = choices
    filter_form.max_quality.choices = choices
    filter_form.min_quality.default = min_quality
    filter_form.max_quality.default = max_quality

    # This form filters the shopping list by a substring.

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

    # Apply the changes to the filter form. The reason that this must be
    # at this point in the code is not known. Other positions did not
    # work.

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

    # This subquery gets the best movie files.

    ranked_files = ranked_movie_files()

    # These subqueries get the preference of each quality limit.

    dvd_quality = quality_preference("DVD")
    bluray_quality = lowest_physical_preference("Bluray-1080")
    uhd_quality = lowest_physical_preference("Bluray-2160")

    CriterionQuality = db.aliased(RefQuality)

    # Each shopping query variant shares these CASE expressions.

    shopping_instruction_case = db.case(
        (Movie.shopping_list_exclude == True, "Already owned"),
        (
            db.and_(
                HAS_CRITERION_RELEASE,
                Movie.criterion_disc_owned == True,
            ),
            "Already owned",
        ),
        (
            db.and_(
                HAS_CRITERION_RELEASE,
                Movie.criterion_disc_owned == False,
                CriterionQuality.preference == uhd_quality,
            ),
            "Buy Criterion edition on 4K UHD Blu-Ray",
        ),
        (
            db.and_(
                HAS_CRITERION_RELEASE,
                Movie.criterion_disc_owned == False,
                CriterionQuality.preference == bluray_quality,
            ),
            "Buy Criterion edition on Blu-Ray",
        ),
        (
            db.and_(
                HAS_CRITERION_RELEASE,
                Movie.criterion_disc_owned == False,
                CriterionQuality.preference == dvd_quality,
            ),
            "Buy Criterion edition on DVD",
        ),
        # A liked movie with no files is possible after the Letterboxd
        # import. The user wants it but does not own it in any form.
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
                HAS_CRITERION_RELEASE,
                DISC_NOT_OWNED,
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
                NOT_EXCLUDED,
            ),
            Movie.shopping_cart_priority,
        ),
        (
            db.and_(
                NOT_EXCLUDED,
                RefQuality.preference < bluray_quality,
            ),
            Movie.shopping_cart_priority,
        ),
        (
            db.and_(
                HAS_CRITERION_RELEASE,
                DISC_NOT_OWNED,
            ),
            Movie.shopping_cart_priority,
        ),
        else_=(0),
    )

    quality_order_case = db.case(
        (Movie.criterion_disc_owned == True, 99),
        # A film with no owned copy sorts before the worst owned quality.
        (
            db.and_(
                File.id == None,
                NOT_EXCLUDED,
            ),
            0,
        ),
        (
            db.and_(
                NOT_EXCLUDED,
                RefQuality.preference < bluray_quality,
            ),
            RefQuality.preference,
        ),
        (
            db.and_(
                HAS_CRITERION_RELEASE,
                DISC_NOT_OWNED,
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
                NOT_EXCLUDED,
            ),
            Movie.shopping_cart_add_date,
        ),
        (
            db.and_(
                NOT_EXCLUDED,
                RefQuality.preference < bluray_quality,
            ),
            Movie.shopping_cart_add_date,
        ),
        (
            db.and_(
                HAS_CRITERION_RELEASE,
                DISC_NOT_OWNED,
            ),
            Movie.shopping_cart_add_date,
        ),
        else_=(0),
    )

    # The order of the views that are not searches: the most urgent
    # purchase first.

    urgency_order = (
        shopping_urgency_order_case.desc(),
        cart_priority_order_case.desc(),
        quality_order_case.asc(),
        cart_age_order_case.desc(),
        title_sort(Movie, tmdb=False).asc(),
        Movie.year.asc(),
        File.edition.asc(),
        File.date_added.asc(),
    )

    # A movie with a liked review but no files is possible after the
    # Letterboxd import. It belongs on the shopping list too. The user
    # wants it but does not own it in any form. A select of File and
    # RefQuality through always-false outer joins gives NULL columns for
    # these movies. Thus, these rows have the same shape as the owned
    # titles, and the UNION below can include them.

    liked_movie_ids = db.session.query(UserMovieReview.movie_id).filter(
        UserMovieReview.user_id == int(current_user.id),
        UserMovieReview.liked == True,
    )

    # A TV episode file has a NULL movie_id. One NULL in a NOT IN subquery
    # makes the predicate false for each row. Thus, filter them out.

    owned_movie_ids = db.session.query(File.movie_id).filter(
        File.feature_type_id == None, File.movie_id != None
    )

    def liked_unowned_query():
        return (
            db.session.query(
                File,
                Movie,
                RefQuality,
                shopping_instruction_case.label("instruction"),
            )
            .select_from(Movie)
            .outerjoin(File, db.and_(File.movie_id == Movie.id, File.id == None))
            .outerjoin(RefQuality, RefQuality.id == File.quality_id)
            .outerjoin(
                CriterionQuality, (CriterionQuality.id == Movie.criterion_quality_id)
            )
            .filter(Movie.id.in_(liked_movie_ids))
            .filter(Movie.id.not_in(owned_movie_ids))
        )

    def shopping_rows():
        """Return the best owned copy of each film in the quality range, with its instruction."""

        return (
            db.session.query(
                File,
                Movie,
                RefQuality,
                shopping_instruction_case.label("instruction"),
            )
            .join(Movie, (Movie.id == File.movie_id))
            .join(RefQuality, (RefQuality.id == File.quality_id))
            .outerjoin(
                CriterionQuality, (CriterionQuality.id == Movie.criterion_quality_id)
            )
            .join(ranked_files, (ranked_files.c.id == File.id))
            .filter(File.feature_type_id == None)
            .filter(ranked_files.c.rank == 1)
            .filter(RefQuality.preference >= min_preference)
            .filter(RefQuality.preference <= max_preference)
        )

    # The Criterion view keeps only the films with a spine number or a set
    # title.

    def criterion_only(query):
        if criterion_release:
            query = query.filter(HAS_CRITERION_RELEASE)
        return query

    # The digital view keeps only the films with no physical-media,
    # SDTV or HDTV copy.

    def digital_only(query):
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
        return query.filter(Movie.id.not_in(db.select(physical_media.c.movie_id)))

    watchlist_groups = None

    if q:
        if re.match(r"tmdb:(?P<tmdb_id>\d+)", q):
            tmdb_id = re.match(r"tmdb:(?P<tmdb_id>\d+)", q).group(1)
            movie = Movie.query.filter_by(tmdb_id=int(tmdb_id)).first()
            if not movie:
                title = f"Upgrade details for TMDB ID {tmdb_id}"
            else:
                title = f'Upgrade details for "{movie.display_title}"'
            movies = (
                shopping_rows()
                .filter(Movie.tmdb_id == tmdb_id)
                .order_by(*TITLE_ORDER)
                .paginate(page=page, per_page=100, error_out=False)
            )

        else:
            title = f"Movies to upgrade matching '{q}'"

            # A space matches any run of characters, as in the other
            # searches. Thus, "spider man" finds "Spider-Man".

            pattern = q.replace(" ", "%")
            title_matches = db.or_(
                Movie.title.ilike(f"%{pattern}%"),
                Movie.tmdb_title.ilike(f"%{pattern}%"),
            )
            # The search narrows the selected view (#458). The Criterion
            # view keeps its filter. The digital view keeps only the owned
            # films with no physical copy, as when it has no search.

            owned_matches = criterion_only(shopping_rows().filter(title_matches))
            liked_matches = criterion_only(liked_unowned_query().filter(title_matches))
            if media == "digital":
                owned_matches = digital_only(owned_matches)

            # A film with no local copy counts as the virtual bottom
            # quality. Thus, it appears only if the minimum of the range
            # reaches down to "Not in library".

            candidates = (
                owned_matches.union_all(liked_matches)
                if min_preference <= bottom_quality and media != "digital"
                else owned_matches
            )
            movies = candidates.order_by(*TITLE_ORDER).paginate(
                page=page, per_page=100, error_out=False
            )

    elif library == "watchlist":
        # This is the watchlist view (#247). It is cross-user and ranked
        # by availability. Thus, Python builds it from the availability
        # cache, not the quality-driven SQL below. It has no pagination
        # on purpose, as the watchlist page itself. The group structure
        # is the navigation.

        title = "Watchlisted movies to buy"
        watchlist_groups = watchlist_shopping_groups()
        movies = None

    elif media == "digital":
        movies = (
            criterion_only(digital_only(shopping_rows()))
            .order_by(*urgency_order)
            .paginate(page=page, per_page=100, error_out=False)
        )

    else:
        owned_titles = criterion_only(shopping_rows())
        liked_titles = criterion_only(liked_unowned_query())

        # A film with no local copy counts as the virtual bottom quality.
        # Thus, it appears only if the minimum of the range reaches down to
        # "Not in library".

        candidates = (
            owned_titles.union_all(liked_titles)
            if min_preference <= bottom_quality
            else owned_titles
        )
        movies = candidates.order_by(*urgency_order).paginate(
            page=page, per_page=100, error_out=False
        )

    movie_shopping_exclude_form = MovieShoppingExcludeForm()

    # The links and the redirects keep each filter of the page.

    filters = {
        "q": q,
        "media": media,
        "library": library,
        "min_quality": min_quality,
        "max_quality": max_quality,
    }
    next_url = (
        url_for("main.movie_shopping", page=movies.next_num, **filters)
        if movies is not None and movies.has_next
        else None
    )
    prev_url = (
        url_for("main.movie_shopping", page=movies.prev_num, **filters)
        if movies is not None and movies.has_prev
        else None
    )

    def posted_movie():
        """Return the film of the add or exclude form, or None (#310).

        The id is a hidden field. A post without it, or with an unknown
        id, flashes a message. It does not give a 500."""

        movie_id = decimal_id(movie_shopping_exclude_form.movie_id.data)
        movie = db.session.get(Movie, movie_id) if movie_id is not None else None
        if movie is None:
            flash("That film is not in the library.", "warning")
        return movie

    # The shopping list is shared by every user. Thus, only an admin can
    # add a film to it or remove a film from it.

    if (
        movie_shopping_exclude_form.add_submit.data
        or movie_shopping_exclude_form.exclude_submit.data
    ) and not current_user.admin:
        flash("Only an admin user can change the shopping list.", "warning")
        return redirect(url_for("main.movie_shopping", page=page, **filters))

    if (
        movie_shopping_exclude_form.add_submit.data
        and movie_shopping_exclude_form.validate_on_submit()
    ):
        movie = posted_movie()
        if movie is None:
            return redirect(url_for("main.movie_shopping", page=page, **filters))
        movie.shopping_list_exclude = None
        db.session.commit()
        flash(f"Added '{movie.title}' to the shopping list")
        return redirect(url_for("main.movie_shopping", page=page, **filters))

    elif (
        movie_shopping_exclude_form.exclude_submit.data
        and movie_shopping_exclude_form.validate_on_submit()
    ):
        movie = posted_movie()
        if movie is None:
            return redirect(url_for("main.movie_shopping", page=page, **filters))
        movie.shopping_list_exclude = 1
        db.session.commit()
        flash(f"Removed '{movie.title}' from the shopping list")
        return redirect(url_for("main.movie_shopping", page=page, **filters))

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
    """Show how to improve the quality of each TV show season.

    Possible user queries:
    - q          : show only the TV series that contain this substring
    - min_quality: show the seasons whose worst quality is at least this
                   good (default "Unknown")
    - max_quality: show the seasons whose worst quality is *below* this
                   limit (default "Bluray-1080p")
    """

    q = request.args.get("q", None, type=str)

    # The bounds of the quality range. An unknown minimum is "Unknown".
    # An unknown maximum is "Bluray-1080p". A minimum above the maximum
    # collapses the range to the maximum only.

    qualities = quality_range(
        request.args.get("min_quality", 0, type=str),
        request.args.get("max_quality", quality_id("Bluray-2160p Remux"), type=str),
        "Unknown",
        "Bluray-1080p",
        collapse="max",
    )
    max_quality = qualities.max_id
    min_preference, max_preference = (
        qualities.min_preference,
        qualities.max_preference,
    )

    # This form filters the shopping list by quality.

    filter_form = TVShoppingFilterForm()

    # Make the list of qualities for the dropdown filter.

    # "Not in library" is the virtual quality of the movie shopping list.
    # TV has no unowned rows. Thus, it stays out of this dropdown.

    choices = (
        db.session.query(RefQuality.id, RefQuality.quality_title)
        .filter(RefQuality.quality_title != "Not in library")
        .order_by(RefQuality.preference.asc())
        .all()
    )
    filter_form.quality.choices = [
        (str(id), quality_title) for (id, quality_title) in choices
    ]
    filter_form.quality.default = max_quality

    # This form filters the shopping list by a substring.

    library_search_form = LibrarySearchForm()
    if filter_form.validate_on_submit():
        return redirect(
            url_for("main.tv_shopping", max_quality=filter_form.quality.data, q=q)
        )

    # Apply the changes to the filter form. The reason that this must be
    # at this point in the code is not known. Other positions did not
    # work.

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

    # These subqueries get the preference of each quality limit.

    dvd_quality = quality_preference("DVD")
    bluray_quality = lowest_physical_preference("Bluray-1080")

    # Grade each season by its episodes. The quality of an episode is the
    # best quality of the files that hold it, a multi-episode file
    # included. The grade of the season is the worst episode quality. An
    # old copy next to a better one does not lower the grade. A Bluray
    # "S13E01" does not hide that E02-E06 exist only in a DVD "S13E01-E06"
    # (tv_file_rank groups by the first episode, and would hide them).

    episode_quality = {}
    for series_id, season, episode, last_episode, preference in (
        db.session.query(
            File.series_id,
            File.season,
            File.episode,
            File.last_episode,
            RefQuality.preference,
        )
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .filter(File.series_id.isnot(None), File.episode.isnot(None))
        .all()
    ):
        last = last_episode if last_episode is not None else episode
        if last < episode or last - episode > 500:
            last = episode
        for number in range(episode, last + 1):
            slot = (series_id, season, number)
            episode_quality[slot] = max(
                episode_quality.get(slot, preference), preference
            )

    season_grades = {}
    for (series_id, season, _number), preference in episode_quality.items():
        grade = season_grades.setdefault((series_id, season), [preference, 0])
        grade[0] = min(grade[0], preference)
        grade[1] += 1

    quality_titles = dict(
        db.session.query(RefQuality.preference, RefQuality.quality_title).all()
    )

    def instruction_for(preference):
        if preference < dvd_quality:
            return "Buy on DVD or Blu-Ray"
        if preference < bluray_quality:
            return "Buy on Blu-Ray"
        return "Already owned"

    season_rows = [
        (
            series_id,
            season,
            episodes,
            quality_titles.get(preference),
            instruction_for(preference),
        )
        for (series_id, season), (preference, episodes) in sorted(
            season_grades.items(),
            key=lambda item: (item[0][0], item[0][1] == 0, item[0][1]),
        )
        if min_preference <= preference <= max_preference
    ]

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
            .order_by(title_sort(TVSeries, tmdb=False).asc())
            .all()
        )

    else:
        t = TVSeries.query.order_by(title_sort(TVSeries, tmdb=False).asc()).all()
        title = "TV Shows to upgrade"

    for series in t:
        seasons = seasons_by_series.get(series.id, [])

        # Do not show a TV series that has no seasons. This is necessary
        # because of the quality filter. Without it, the page could show
        # a TV series with no season that reaches the quality limit.

        if len(seasons) == 0:
            continue

        tv.append(
            {
                "id": series.id,
                "title": series.title,
                "tmdb_id": series.tmdb_id,
                "tmdb_name": series.tmdb_name,
                "first_air_year": series.display_year,
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
