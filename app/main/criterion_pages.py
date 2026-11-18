"""Serve the page of the Criterion Collection."""

import traceback
from flask import current_app, render_template, url_for, request
from flask_login import current_user, login_required
from app import db
from app.models import (
    CatalogExclusion,
    File,
    Movie,
    RefQuality,
    movie_genres,
    sort_title,
)
from app.main import bp
from app.media.file_ranking import criterion_settled, ranked_movie_files
from app.main.helpers import _upgrade_threshold
from app.discovery.funnel import might_interest, user_film_sets_by_id
from app.discovery.recommendations import (
    coarse_interest_score,
    marker_bar,
    recommended_movie_ids,
    stored_profile,
)
from app.availability.streaming import (
    batch_title_availability,
    request_warm,
    streaming_matches,
    user_provider_ids,
)
from app.criterion.criterion_catalog import set_title_key
from app.criterion.criterion_catalog import (
    criterion_release_lookups,
    stored_criterion_releases,
)

CRITERION_CHANNEL_PROVIDER_ID = 258


CRITERION_CATALOG_PER_PAGE = 120


def _page_window(current, last):
    """Return the page numbers for a pagination bar, with None as a gap.

    The shape is the same as the iter_pages output of Flask-SQLAlchemy
    on the people page. It has the first 2 and last 2 pages, a window
    around the current page, and ellipses between them.
    """

    numbers = []
    previous = 0
    for number in range(1, last + 1):
        if number <= 2 or abs(number - current) <= 2 or number > last - 2:
            if previous and number - previous > 1:
                numbers.append(None)
            numbers.append(number)
            previous = number
    return numbers


def _criterion_row_key(row):
    """Return the sort key of a (File, Movie, RefQuality) row of the
    Criterion page. The smallest key is the row that stands for the film.

    The editionless file comes first. Then a file that is not fullscreen,
    then the best quality, then the lowest file id."""

    file, _, quality = row
    return (
        file.edition is not None,
        bool(file.fullscreen),
        -(quality.preference or 0),
        file.id,
    )


def catalog_rows_for(releases, consumed_tmdb, consumed_title_year, excluded_tmdb):
    """Return the rows of the catalog releases that the library does not consume.

    The standalone entries come before the set entries in the cache.
    Thus, a film with both keeps its own spine. The releases without a
    TMDB id render as plain spine rows. Box-set CONTAINER items are
    redundant. Wikidata gives the spine to the set item, and no TMDB id
    (TMDB has no set entries). The member films arrive separately and
    show the set title. Thus, a row without a TMDB id, whose spine
    belongs to a set, would only shadow its own members ("#88 Ivan the
    Terrible" between the actual Parts I–III). The hand-excluded ids
    (Wikidata junk, see CatalogExclusion) do not render. A row without a
    TMDB id does not render when a release with a TMDB id has its title
    and year (#382). Wikidata can hold a film as a standalone release
    with a TMDB id and as a set member without one."""

    set_spines = {
        release["spine_number"] for release in releases if release.get("set_title")
    }
    # The first pass takes the title and year of each release with a
    # TMDB id. Thus, the order of the cache does not matter (#382).
    tmdb_title_years = {
        (release.get("title"), release.get("year"))
        for release in releases
        if release.get("tmdb_id") and release.get("tmdb_id") not in excluded_tmdb
    }
    rows = []
    keys = set()
    for release in releases:
        tmdb_id = release.get("tmdb_id")
        if (
            not tmdb_id
            and not release.get("set_title")
            and release.get("spine_number") in set_spines
        ):
            continue
        title_year = (release.get("title"), release.get("year"))
        if tmdb_id and (tmdb_id in excluded_tmdb or tmdb_id in consumed_tmdb):
            continue
        if title_year in consumed_title_year:
            continue
        if not tmdb_id and title_year in tmdb_title_years:
            continue
        key = tmdb_id or title_year
        if key in keys:
            continue
        keys.add(key)
        rows.append(
            {
                "kind": "tmdb" if tmdb_id else "plain",
                "movie": None,
                "tmdb_id": tmdb_id,
                "title": release.get("label"),
                "year": release.get("year"),
                "spine": release.get("spine_number"),
                "set_title": release.get("set_title"),
            }
        )
    return rows


def criterion_sort_key(row):
    """Return the spine order key of a Criterion page row.

    The order is 1 spine order across owned and unowned rows. The set
    members sort at the spine of their set (year, then title inside the
    set). The local rows without a spine keep their old place at the
    end. The spellings of 1 set title sort together (#697)."""

    spine = row.get("spine")
    title = sort_title(row.get("title"))
    return (
        0 if spine is not None else 1,
        spine if spine is not None else 0,
        set_title_key(row.get("set_title") or ""),
        row.get("year") or 9999,
        title.upper(),
    )


@bp.route("/library/criterion-collection")
@login_required
def criterion_collection():
    """Show the full Criterion Collection spine catalog, in and beyond the library.

    Each release from the Wikidata spine cache renders, not only the
    films of the library. The owned films keep their settled/amber
    verdicts. The releases that the library does not have render like
    TMDB search rows. Their row opens the log page. Thus, the user can
    add them to the watchlist. The small number of releases without a
    TMDB id in Wikidata list as plain spine rows. The ON STREAMING filter
    keeps the films on the Criterion Channel or on a subscribed service.
    """

    filter_status = request.args.get("filter", "all")
    if filter_status not in ("all", "library", "settled", "streaming"):
        filter_status = "all"
    page = max(request.args.get("page", 1, type=int) or 1, 1)

    # The whole spine catalog from the stored Wikidata copy. The page never
    # queries Wikidata (#283). Without a stored copy, the page shows the
    # library rows only, and a refresh goes on the queue.

    try:
        releases = stored_criterion_releases()
    except Exception:
        current_app.logger.warning(traceback.format_exc())
        releases = []
    by_tmdb_id, by_title_year = criterion_release_lookups(releases)
    release_tmdb_ids = [
        release["tmdb_id"] for release in releases if release.get("tmdb_id")
    ]

    # Library rows. These are the best main-feature file per film, for
    # the films with Criterion metadata OR a release match by TMDB id.
    # A film whose record is older than its release was never marked.
    # But the catalog knows its spine.

    ranked_files = ranked_movie_files()

    results = (
        db.session.query(File, Movie, RefQuality)
        .join(Movie, (Movie.id == File.movie_id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .join(ranked_files, (ranked_files.c.id == File.id))
        .filter(File.feature_type_id == None)
        .filter(ranked_files.c.rank == 1)
        .filter(
            db.or_(
                Movie.criterion_spine_number != None,
                Movie.criterion_set_title != None,
                Movie.tmdb_id.in_(release_tmdb_ids or [0]),
            )
        )
        .all()
    )

    # 1 row per film (#349). The editionless main feature stands for the
    # film. A film that has only edition-tagged main features (for
    # example, only a Director's Cut) is owned too. Then its best edition
    # stands for it. Before, an edition filter dropped such a film, and
    # its release showed as not owned.

    best_rows = {}
    for row in results:
        file, movie, quality = row
        held = best_rows.get(movie.id)
        if held is None or _criterion_row_key(row) < _criterion_row_key(held):
            best_rows[movie.id] = row
    results = list(best_rows.values())

    # A library row is SETTLED (criterion_settled) when the Criterion
    # disc is owned AND the local file matches the format of the
    # release. Settled means the Fitzflix library badge and nothing to
    # do. Each other row shows its amber quality tier. This means: go
    # find the Criterion version.

    movie_ids = [movie.id for _, movie, _ in results]
    CriterionQuality = db.aliased(RefQuality)
    criterion_prefs = dict(
        db.session.query(Movie.id, CriterionQuality.preference)
        .join(CriterionQuality, CriterionQuality.id == Movie.criterion_quality_id)
        .filter(Movie.id.in_(movie_ids or [0]))
    )
    threshold = _upgrade_threshold()

    # Each library film consumes its catalog release. The match is by
    # TMDB id first, then by title+year. This is the matching order of
    # the import. Thus, the rest renders as beyond-the-library rows. A
    # film with a standalone release and a set membership consumes both
    # through its shared TMDB id.

    consumed_tmdb = set()
    consumed_title_year = set()
    library_rows = []
    for file, movie, quality in results:
        release = by_tmdb_id.get(movie.tmdb_id) if movie.tmdb_id else None
        if release is None and movie.title and movie.year:
            release = by_title_year.get((movie.title.upper(), movie.year))
        if movie.tmdb_id:
            consumed_tmdb.add(movie.tmdb_id)
        if release:
            if release.get("tmdb_id"):
                consumed_tmdb.add(release["tmdb_id"])
            if release.get("title") and release.get("year"):
                consumed_title_year.add((release["title"], release["year"]))
        library_rows.append(
            {
                "kind": "library",
                "file": file,
                "movie": movie,
                "quality": quality,
                "settled": criterion_settled(
                    file,
                    quality,
                    criterion_prefs.get(movie.id),
                    threshold,
                    movie.criterion_disc_owned,
                ),
                "tmdb_id": movie.tmdb_id,
                "title": movie.tmdb_title or movie.title,
                "year": movie.display_year,
                "spine": movie.criterion_spine_number
                or (release or {}).get("spine_number"),
                "set_title": movie.criterion_set_title
                or (release or {}).get("set_title"),
            }
        )

    # The rest of the catalog (catalog_rows_for).

    catalog_rows = catalog_rows_for(
        releases,
        consumed_tmdb,
        consumed_title_year,
        {tmdb_id for (tmdb_id,) in db.session.query(CatalogExclusion.tmdb_id)},
    )

    # The file-less local records (logged or watchlisted unowned films)
    # give their catalog rows the stored title, poster, and overview.
    # They also carry the funnel badges.

    records = {}
    catalog_tmdb_ids = [row["tmdb_id"] for row in catalog_rows if row["tmdb_id"]]
    if catalog_tmdb_ids:
        records = {
            record.tmdb_id: record
            for record in Movie.query.filter(Movie.tmdb_id.in_(catalog_tmdb_ids))
        }
    for row in catalog_rows:
        record = records.get(row["tmdb_id"]) if row["tmdb_id"] else None
        if record is None:
            continue
        row["movie"] = record
        if record.tmdb_title:
            row["title"] = record.tmdb_title
            if record.tmdb_release_date:
                row["year"] = record.tmdb_release_date.year

    # The personal funnel. It is per-user, like at each other place. A
    # seen film never shows the might-interest badge. An owned film
    # shows the badge when it is in the stored ranking. A catalog row
    # with a refreshed record scores through the coarse scorer against
    # the profile-relative bar. A row without a record has no genres to
    # score. It stays unmarked.

    funnel_ids = movie_ids + [record.id for record in records.values()]
    sets = user_film_sets_by_id(current_user.id, funnel_ids)
    seen_ids, watchlisted_ids, refused_ids = (
        sets.logged,
        sets.watchlisted,
        sets.refused,
    )
    rec_ids = recommended_movie_ids(current_app.redis, current_user.id)
    profile = stored_profile(current_app.redis, current_user.id)
    bar = marker_bar(profile) if profile else None

    # The catalog scorer reads the genres of each record. This is 1 query
    # for all of them, not a lazy load per film (956 queries per visit
    # before 2026-08).

    genre_ids_by_movie = {}
    if profile is not None and records:
        for movie_id, genre_id in db.session.query(
            movie_genres.c.movie_id, movie_genres.c.genre_id
        ).filter(
            movie_genres.c.movie_id.in_([record.id for record in records.values()])
        ):
            genre_ids_by_movie.setdefault(movie_id, []).append(genre_id)

    for row in library_rows:
        movie_id = row["movie"].id
        row["seen"] = movie_id in seen_ids
        row["watchlisted"] = movie_id in watchlisted_ids
        row["might_interest"] = might_interest(
            seen=row["seen"],
            refused=movie_id in refused_ids,
            recommended=movie_id in rec_ids,
        )
    for row in catalog_rows:
        record = row["movie"]
        row["seen"] = record is not None and record.id in seen_ids
        row["watchlisted"] = record is not None and record.id in watchlisted_ids
        score = None
        if record is not None and profile is not None and not row["seen"]:
            genre_ids = genre_ids_by_movie.get(record.id, [])
            if genre_ids:
                score = coarse_interest_score(profile, genre_ids, row["year"])
        row["might_interest"] = might_interest(
            seen=row["seen"],
            refused=record is not None and record.id in refused_ids,
            score=score,
            bar=bar,
        )

    merged = sorted(library_rows + catalog_rows, key=criterion_sort_key)

    # The ON STREAMING filter (requested by Glenn, 2026-09-29). It keeps
    # the films that stream now on a flat-rate service: the Criterion
    # Channel or a subscribed service of the user. The Criterion Channel
    # counts on this page, also for a user who does not subscribe. The
    # filter and its count must see the whole catalog before the
    # pagination. Thus, Fitzflix reads the availability of each row with
    # a TMDB id. That is 1 MGET from the cache that the nightly refresh
    # keeps full. It never fetches inline (2026-08, a page of misses cost
    # 4 seconds behind the rate limiter). The catalog films without a
    # cache entry warm in the background for the next visit.

    provider_ids = user_provider_ids(current_user) | {CRITERION_CHANNEL_PROVIDER_ID}
    availability_by_id, deferred = batch_title_availability(
        (row["tmdb_id"] for row in merged if row["tmdb_id"]),
        fetch_limit=0,
    )
    request_warm("criterion", deferred)
    for row in merged:
        # The rows with a local file skip the leaving/newly-added
        # annotations. The copy on the shelf stays.
        row["streaming"] = bool(row["tmdb_id"]) and bool(
            streaming_matches(
                availability_by_id.get(row["tmdb_id"]),
                provider_ids,
                tmdb_id=None if row["kind"] == "library" else row["tmdb_id"],
            )
        )

    def status_match(row, status):
        """Return True if the row passes the chosen filter."""

        if status == "library":
            return row["kind"] == "library"
        if status == "settled":
            return row["kind"] == "library" and row["settled"]
        if status == "streaming":
            return row["streaming"]
        return True

    counts = {
        status: sum(1 for row in merged if status_match(row, status))
        for status in ("all", "library", "settled", "streaming")
    }
    filtered = [row for row in merged if status_match(row, filter_status)]

    last_page = max(
        (len(filtered) + CRITERION_CATALOG_PER_PAGE - 1) // CRITERION_CATALOG_PER_PAGE,
        1,
    )
    page = min(page, last_page)
    start = (page - 1) * CRITERION_CATALOG_PER_PAGE
    rows = filtered[start : start + CRITERION_CATALOG_PER_PAGE]

    return render_template(
        "library_criterion.html",
        title="Criterion Collection films",
        rows=rows,
        filter_status=filter_status,
        counts=counts,
        # The warming note matters only when the ON STREAMING filter
        # hides the films without a cache entry.
        pending=len(deferred) if filter_status == "streaming" else 0,
        page_numbers=_page_window(page, last_page),
        current_page=page,
        prev_url=(
            url_for(
                "main.criterion_collection",
                filter=filter_status,
                page=page - 1,
            )
            if page > 1
            else None
        ),
        next_url=(
            url_for(
                "main.criterion_collection",
                filter=filter_status,
                page=page + 1,
            )
            if page < last_page
            else None
        ),
    )
