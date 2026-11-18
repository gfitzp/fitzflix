"""Serve the People page and the page of 1 person."""

from datetime import date, datetime
from types import SimpleNamespace
from flask import abort, current_app, render_template, url_for, request
from flask_login import current_user, login_required
from flask_sqlalchemy.pagination import Pagination
from app import db
from app.models import (
    File,
    Movie,
    MovieCast,
    MovieCrew,
    RefQuality,
    TMDBCredit,
    TVCast,
    TVCrew,
    TVSeries,
    UserMovieReview,
)
from app.main import bp
from app.media.file_ranking import best_copy_key, ranked_movie_files
from app.cache import cached_fetch
from app.metadata.tmdb_apply import (
    PEOPLE_RANKING_SECONDS,
    tmdb_json_or,
    PEOPLE_RANKING_KEY,
)
from app.discovery.funnel import might_interest, user_film_sets_by_id
from app.discovery.recommendations import (
    JOB_TO_CLASS,
    credit_interest_markers,
    recommended_movie_ids,
    stored_profile,
)
from app.availability.streaming import (
    batch_title_availability,
    rental_matches,
    request_warm,
    streaming_matches,
    user_provider_ids,
)
from app.discovery.estimate_warm import person_movie_credits
from app.discovery.estimate_warm import person_tv_credits

# The crew jobs that count as key roles for search and filmographies.
# They are the same roles that the taste engine scores, labeled as
# nouns. Decided by Glenn: only these join the film-count ordering.
# Thus, grips and gaffers do not outrank directors.

CREW_ROLE_LABELS = {job: role.capitalize() for job, role in JOB_TO_CLASS.items()}


# A multi-role credit line reads in the conventional closing-credit
# order (directed, written, shot, edited, scored), not in the TMDB
# payload order.

CLOSING_CREDIT_ORDER = ("Director", "Writer", "Cinematographer", "Editor", "Composer")


# The TMDB person details of the person pages, cached in Redis. The
# movie and TV credits keys live with person_movie_credits and
# person_tv_credits in app.discovery.estimate_warm.

PERSON_DETAILS_KEY = "fitzflix:tmdb:person:{person_id}:details"


def _tmdb_person_details(person_id):
    """Return the name, photo, and biographical fields of the person from TMDB.

    Fitzflix caches the result for 1 day. The result is None when there
    is no API key or TMDB does not answer with a name. The filmography
    treats None as an unknown person.
    """

    if not current_app.config["TMDB_API_KEY"]:
        return None

    def fetch():
        payload = tmdb_json_or(f"/person/{person_id}", None, None) or {}
        if not payload.get("name"):
            return None, False
        details = {
            "name": payload["name"],
            "profile_path": payload.get("profile_path"),
            "biography": payload.get("biography"),
            "birthday": payload.get("birthday"),
            "deathday": payload.get("deathday"),
            "place_of_birth": payload.get("place_of_birth"),
        }
        return details, True

    return cached_fetch(PERSON_DETAILS_KEY.format(person_id=person_id), 86400, fetch)


def _tmdb_date(value):
    """Return a date from a TMDB YYYY-MM-DD string, or None if it is absent or odd."""

    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _person_bio(details):
    """Return the formatted born/died lines and the biography of a person.

    The source is a TMDB person-details dict. The filmography header
    shows the result. The age computes against the death date when
    there is one.
    """

    birthday = _tmdb_date(details.get("birthday"))
    deathday = _tmdb_date(details.get("deathday"))
    age = None
    if birthday:
        end = deathday or date.today()
        age = (
            end.year
            - birthday.year
            - ((end.month, end.day) < (birthday.month, birthday.day))
        )
    born_line = None
    if birthday:
        born_line = f"Born {birthday.strftime('%B %-d, %Y')}"
        if details.get("place_of_birth"):
            born_line += f" in {details['place_of_birth']}"
        if not deathday and age is not None:
            born_line += f" (age {age})"
    died_line = None
    if deathday:
        died_line = f"Died {deathday.strftime('%B %-d, %Y')}"
        if age is not None:
            died_line += f" (aged {age})"
    return {
        "born_line": born_line,
        "died_line": died_line,
        "biography": (details.get("biography") or "").strip(),
    }


def merge_credit_rows(credits, local_by_tmdb_id, title_key, date_key):
    """Return {tmdb id: row} with 1 row per title of a TMDB credits payload.

    credits is {"cast": [...], "crew": [...]}. The cast and crew
    credits of 1 title share 1 row. The characters are combined, and
    the key crew jobs read in the closing-credit order. title_key and
    date_key name the title and the date fields ("title" and
    "release_date" for a film, "name" and "first_air_date" for a
    series). row["local"] is the entry of local_by_tmdb_id for the
    title, or None. The rows keep the order of the credits."""

    rows = {}

    def row_for(entry):
        """Return the row of a credit entry. Make it on first sight."""

        tmdb_id = entry.get("id")
        row = rows.get(tmdb_id)
        if row is None:
            year = (entry.get(date_key) or "")[:4]
            row = rows[tmdb_id] = {
                "tmdb_id": tmdb_id,
                title_key: entry.get(title_key),
                "year": int(year) if year.isdigit() else None,
                "poster_path": entry.get("poster_path"),
                "genre_ids": entry.get("genre_ids") or [],
                "overview": entry.get("overview"),
                "episode_count": entry.get("episode_count"),
                "characters": [],
                "jobs": [],
                "local": local_by_tmdb_id.get(tmdb_id),
            }
        return row

    for entry in (credits or {}).get("cast") or []:
        if entry.get("id") is None:
            continue
        row = row_for(entry)
        if entry.get("character"):
            row["characters"].append(entry["character"])
    for entry in (credits or {}).get("crew") or []:
        if entry.get("id") is None:
            continue
        row = row_for(entry)
        label = CREW_ROLE_LABELS.get(entry.get("job"))
        if label and label not in row["jobs"]:
            row["jobs"].append(label)
    for row in rows.values():
        row["jobs"].sort(key=CLOSING_CREDIT_ORDER.index)
    return rows


def _filmography(credit, library_only=False):
    """Render the filmography page of 1 TMDB person, the credit view of movie_library.

    The page shows the full career by default. With library_only, it
    keeps only the films with an owned file and the series with owned
    episodes."""

    ranked_files = ranked_movie_files()

    # Credit ids are TMDB person ids. Thus, the filmography is not
    # limited to people with local credit rows. The user can browse
    # each person that TMDB knows from any cast list. The day-cached
    # TMDB person lookup supplies the biography for each person. A
    # local credit row supplies the name and photo when Fitzflix
    # cannot reach TMDB.

    person = TMDBCredit.query.filter_by(id=int(credit)).first()
    details = _tmdb_person_details(int(credit)) or {}
    person_name = details.get("name") or (person.name if person else None)
    person_profile_path = details.get("profile_path") or (
        person.tmdb_profile_path if person else None
    )
    if person_name is None:
        abort(404)
    bio = _person_bio(details) if details else None

    # The filmography shows the full TMDB career of the person. It
    # does not matter if a film has a local record. The local rows
    # attach the best owned file through an outer join. The rank
    # condition must be in the join, not in the WHERE clause. If not,
    # the WHERE clause would filter away the file-less review-only
    # records. The full credit list comes from TMDB, cached for 1 day.

    best_file_ids = db.session.query(ranked_files.c.id).filter(ranked_files.c.rank == 1)

    def local_credit_rows(credit_table):
        """Return the local films of the person through a credit join table.

        Each film has its best owned file outer-joined."""

        query = (
            db.session.query(File, Movie, RefQuality)
            .select_from(Movie)
            .join(credit_table, (credit_table.movie_id == Movie.id))
            .outerjoin(
                File,
                db.and_(
                    File.movie_id == Movie.id,
                    File.feature_type_id == None,
                    File.id.in_(best_file_ids),
                ),
            )
            .outerjoin(RefQuality, (RefQuality.id == File.quality_id))
            .filter(credit_table.credit_id == int(credit))
        )
        if credit_table is MovieCrew:
            query = query.filter(MovieCrew.job.in_(list(CREW_ROLE_LABELS)))
        return query.all()

    local_rows = local_credit_rows(MovieCast) + local_credit_rows(MovieCrew)

    # The best owned copy per movie. A movie can have several rank-1
    # editions. The filmography shows 1 entry per film. The best of
    # them is the first in MOVIE_BEST_ORDER (best_copy_key).

    local = {}
    for file, film, quality in local_rows:
        existing = local.get(film.id)
        if existing is None or best_copy_key(file, quality) < best_copy_key(
            existing["file"], existing["quality"]
        ):
            local[film.id] = {"movie": film, "file": file, "quality": quality}

    reviewed = {
        movie_id: bool(liked)
        for movie_id, liked in db.session.query(
            UserMovieReview.movie_id,
            db.func.max(db.case((UserMovieReview.liked == True, 1), else_=0)),
        )
        .filter(UserMovieReview.user_id == int(current_user.id))
        .filter(UserMovieReview.movie_id.in_(list(local.keys()) or [0]))
        .group_by(UserMovieReview.movie_id)
        .all()
    }

    # The full TMDB credit list of the person, cached for 1 day

    tmdb_credits = None
    if current_app.config["TMDB_API_KEY"]:

        tmdb_credits = person_movie_credits(credit)

    # Merge. There is 1 row per film. The TMDB credits come first. They
    # are deduplicated by film, and the characters are combined. Then
    # come the local credits that TMDB did not list.

    local_by_tmdb_id = {
        entry["movie"].tmdb_id: entry
        for entry in local.values()
        if entry["movie"].tmdb_id is not None
    }
    rows = merge_credit_rows(tmdb_credits, local_by_tmdb_id, "title", "release_date")
    for row in rows.values():
        local_entry = row.pop("local") or {}
        row["movie"] = local_entry.get("movie")
        row["file"] = local_entry.get("file")
        row["quality"] = local_entry.get("quality")

    matched_tmdb_ids = set(rows.keys())
    for entry in local.values():
        if entry["movie"].tmdb_id in matched_tmdb_ids:
            continue
        rows[f"local:{entry['movie'].id}"] = {
            "tmdb_id": entry["movie"].tmdb_id,
            "title": entry["movie"].tmdb_title or entry["movie"].title,
            "year": entry["movie"].year,
            "poster_path": entry["movie"].tmdb_poster_path,
            "overview": entry["movie"].tmdb_overview,
            "characters": [],
            "jobs": [],
            "movie": entry["movie"],
            "file": entry["file"],
            "quality": entry["quality"],
        }

    filmography = sorted(
        rows.values(), key=lambda row: (row["year"] is None, row["year"] or 0)
    )

    # The library filter keeps the films that have an owned file. The
    # review-only records and the TMDB-only credits drop out. The
    # filter runs before the streaming step. The owned films never
    # carry streaming badges. Thus, the filtered page makes no
    # availability lookups.

    if library_only:
        filmography = [row for row in filmography if row["file"] is not None]
    sets = user_film_sets_by_id(current_user.id, list(local.keys()))
    for row in filmography:
        row["seen"] = row["movie"] is not None and row["movie"].id in reviewed
        row["liked"] = bool(row["movie"] and reviewed.get(row["movie"].id))
        row["watchlisted"] = (
            row["movie"] is not None and row["movie"].id in sets.watchlisted
        )

    # "Might interest you" markers. The unowned films score at render
    # time. The score uses the cached credits payload and the stored
    # taste profile of the user (no TMDB calls, nothing persisted).
    # The owned unwatched films show a badge when the nightly
    # recompute ranked them in the stored recommendations. Thus, the
    # filmographies agree with the library rail and the search pages.

    profile = stored_profile(current_app.redis, current_user.id)
    rec_ids = recommended_movie_ids(current_app.redis, current_user.id)
    interesting = credit_interest_markers(profile, int(credit), filmography)
    for row in filmography:
        movie = row["movie"]
        row["might_interest"] = might_interest(
            seen=row["seen"],
            refused=movie is not None and movie.id in sets.refused,
            recommended=(row["quality"] is None and row["tmdb_id"] in interesting)
            or bool(movie and movie.id in rec_ids),
        )

    # Streaming badges on the films without a local file, filtered to
    # the services of this user. Fitzflix reads the availability from
    # the cache that the nightly refresh keeps full. It never fetches
    # inline. A career can span hundreds of films. Each
    # fetch shares the app-wide TMDB rate limiter. Thus, the page of
    # a prolific actor stalled 4 to 6 seconds behind 50 fetches. The
    # refresh cannot cover the films without a record. Those films
    # warm in the background for the next visit.

    provider_ids = user_provider_ids(current_user)
    if provider_ids:
        availability_by_id, deferred = batch_title_availability(
            (
                row["tmdb_id"]
                for row in filmography
                if row["tmdb_id"] and not row["quality"]
            ),
            fetch_limit=0,
        )
        request_warm(int(credit), deferred)
        for row in filmography:
            if row["quality"] or not row["tmdb_id"]:
                continue
            availability = availability_by_id.get(row["tmdb_id"])
            matches = streaming_matches(
                availability, provider_ids, tmdb_id=row["tmdb_id"]
            )
            rentals = rental_matches(availability, provider_ids)
            if matches:
                row["streaming"] = matches
            if rentals:
                row["rentals"] = rentals

    # Television credits. This is the TMDB TV career of the person,
    # 1 row per series, cached for 1 day like the film list. Self
    # appearances are dropped. The talk-show and awards-night rows
    # would swamp the acting credits (the key-roles-only spirit).
    # Owned series link to their pages. TV has no review flow. Thus,
    # the unowned rows render without a link (the TMDB-row rule).

    tv_rows = {}
    tv_credits = None
    if current_app.config["TMDB_API_KEY"]:

        tv_credits = person_tv_credits(int(credit))

    if tv_credits:
        credited_ids = {
            entry.get("id")
            for entry in (tv_credits.get("cast") or []) + (tv_credits.get("crew") or [])
            if entry.get("id") is not None
        }
        local_series = {
            series.tmdb_id: series
            for series in TVSeries.query.filter(
                TVSeries.tmdb_id.in_(credited_ids or [0])
            )
        }

        tv_rows = merge_credit_rows(tv_credits, local_series, "name", "first_air_date")
        for row in tv_rows.values():
            series = row["series"] = row.pop("local")
            row["owned"] = bool(series and series.files.count())

    television = sorted(
        tv_rows.values(), key=lambda row: (row["year"] is None, row["year"] or 0)
    )
    if library_only:
        television = [row for row in television if row["owned"]]

    return render_template(
        "filmography.html",
        title=person_name,
        person_name=person_name,
        profile_path=person_profile_path,
        bio=bio,
        credit=int(credit),
        library_only=library_only,
        filmography=filmography,
        television=television,
        tmdb_unavailable=tmdb_credits is None,
    )


@bp.route("/people")
@login_required
def people():
    """Browse each credited person across the films of the library.

    Cast and key crew roles both count. Decided by Glenn: only the key
    roles join the film-count ordering. Thus, day players still
    register, but grips do not outrank directors. The default shows the
    people that appear in more than 1 film, because the long tail is
    one-appearance day players. A search by name widens the list to all
    people. Uncredited-only roles never count toward the filter (the
    rule of Glenn). Each person links to their filmography page.
    """

    page = request.args.get("page", 1, type=int)
    query_text = (request.args.get("q") or "").strip()
    minimum_films = 1 if query_text else 2

    # Cast by default. The acting long tail
    # is what a browse usually wants. Crew or all people are 1 click
    # away. The film counts follow the filter. The count of a director
    # under "cast" is their acting appearances.

    role = request.args.get("role", "cast")
    if role not in ("cast", "crew", "all"):
        role = "cast"

    # The browse path (no search) pages through the cached ranking.
    # Before 2026-08, the full aggregation over the cast and crew tables
    # ran 2 times per visit. That was 1 time for the page and 1 time for
    # the count of paginate, at half a second each. A search still
    # queries live.
    # The name filter narrows it. The single-title people that it admits
    # are not in the ranking.

    if query_text:
        people_page = _ranked_people_query(role, query_text, minimum_films).paginate(
            page=page, per_page=120, error_out=False
        )
    else:
        people_page = ListPagination(
            _ranked_people(role), page=page, per_page=120, error_out=False
        )

    role_param = role if role != "cast" else None
    return render_template(
        "people.html",
        title="People",
        people=people_page.items,
        roles=_dominant_roles([person.id for person in people_page.items]),
        pages=people_page,
        query_text=query_text,
        role=role,
        role_param=role_param,
        next_url=(
            url_for(
                "main.people",
                page=people_page.next_num,
                q=query_text or None,
                role=role_param,
            )
            if people_page.has_next
            else None
        ),
        prev_url=(
            url_for(
                "main.people",
                page=people_page.prev_num,
                q=query_text or None,
                role=role_param,
            )
            if people_page.has_prev
            else None
        ),
    )


# The tie-break order for the dominant role of a person. A
# director-actor reads as Director. A bit-part-everything reads as
# Actor before the narrower crafts.

ROLE_PRECEDENCE = (
    "Director",
    "Actor",
    "Cinematographer",
    "Composer",
    "Writer",
    "Editor",
)


def _ranked_people_query(role, query_text, minimum_films):
    """Return the people with their work counts under the role filter.

    A name search can narrow the list. The people with the most works
    come first. Ties break on the surname. TMDB has no structured sort
    name. Thus, the last whitespace-separated token stands in for it.
    This is wrong for "Jr." suffixes and multi-word surnames, but
    acceptable as a tie-break."""

    pairs = _credited_film_pairs(role)
    film_count = db.func.count(db.distinct(pairs.c.movie_id)).label("film_count")
    people_query = (
        db.session.query(
            TMDBCredit.id,
            TMDBCredit.name,
            TMDBCredit.tmdb_profile_path,
            film_count,
        )
        .join(pairs, pairs.c.credit_id == TMDBCredit.id)
        .group_by(TMDBCredit.id, TMDBCredit.name, TMDBCredit.tmdb_profile_path)
    )
    if query_text:
        people_query = people_query.filter(TMDBCredit.name.ilike(f"%{query_text}%"))
    # The credit id is the unique tiebreak. 2 people with the same name
    # and count keep 1 order across the pages.

    return people_query.having(film_count >= minimum_films).order_by(
        film_count.desc(),
        db.func.substring_index(TMDBCredit.name, " ", -1).asc(),
        TMDBCredit.name.asc(),
        TMDBCredit.id.asc(),
    )


def _ranked_people(role):
    """Return each person credited on more than 1 work under the role filter.

    The rows are [id, name, profile path, count], in page order. Redis
    caches them until the next credit write (see
    invalidate_people_ranking) or for PEOPLE_RANKING_SECONDS."""

    def fetch():
        return [
            [credit_id, name, profile_path, count]
            for credit_id, name, profile_path, count in _ranked_people_query(
                role, None, 2
            )
        ], True

    return cached_fetch(
        PEOPLE_RANKING_KEY.format(role=role), PEOPLE_RANKING_SECONDS, fetch
    )


class ListPagination(Pagination):
    """The Pagination of Flask-SQLAlchemy over an in-memory ranking.

    The people template reads .items, .page, .total, and iter_pages()
    in the same way as it did from the query."""

    def __init__(self, rows, **kwargs):
        self._rows = rows
        # The default of Query.paginate, not the 100-row maximum of the base class
        kwargs.setdefault("max_per_page", None)
        super().__init__(**kwargs)

    def _query_items(self):
        """Return the rows of this page as the attribute bags of the template."""

        offset = self._query_offset
        return [
            SimpleNamespace(
                id=credit_id,
                name=name,
                tmdb_profile_path=profile_path,
                film_count=count,
            )
            for credit_id, name, profile_path, count in self._rows[
                offset : offset + self.per_page
            ]
        ]

    def _query_count(self):
        """Return the length of the whole ranking."""

        return len(self._rows)


def _owned_work_ids():
    """Return the queries of the owned movie ids and the owned series ids.

    This is the one rule of "in the library" for the people surfaces. A
    movie is owned when a main feature file of it exists. A series is
    owned when a file of it exists."""

    owned_movie_ids = db.session.query(File.movie_id).filter(
        File.movie_id != None, File.feature_type_id == None
    )
    owned_series_ids = db.session.query(File.series_id).filter(File.series_id != None)
    return owned_movie_ids, owned_series_ids


def _credited_film_pairs(role="all"):
    """Return the (credit_id, movie_id) pairs that the people surfaces count.

    The pairs are the credited cast rows, the key crew roles, or their
    union without duplicates. Movies and TV series both count. A TV
    series goes in the movie_id column as its NEGATED id. Thus, the
    distinct-count space has no collisions without a discriminator
    column. Nothing joins these ids back to a table. They are only
    counted.

    Only the owned works count. A movie counts when a main
    feature file of it exists. A series counts when a file of it
    exists. Fitzflix makes file-less records for the watchlist, the
    diary, the catalogs, and the TMDB review page. The user cannot see
    which records exist. Thus, a count of the records read as wrong
    against the "In library" filter of the filmography page.

    The search paths always count all ("all"). The /people page passes
    its cast/crew filter through. Thus, the work counts show the
    selected credit type.
    """

    owned_movie_ids, owned_series_ids = _owned_work_ids()
    cast_pairs = (
        db.session.query(
            MovieCast.credit_id.label("credit_id"),
            MovieCast.movie_id.label("movie_id"),
        )
        .filter(
            db.or_(
                MovieCast.character == None,
                db.not_(MovieCast.character.like("%(uncredited)%")),
            )
        )
        .filter(MovieCast.movie_id.in_(owned_movie_ids))
    )
    tv_cast_pairs = (
        db.session.query(
            TVCast.credit_id.label("credit_id"),
            (-TVCast.tv_id).label("movie_id"),
        )
        .filter(
            db.or_(
                TVCast.character == None,
                db.not_(TVCast.character.like("%(uncredited)%")),
            )
        )
        .filter(TVCast.tv_id.in_(owned_series_ids))
    )
    crew_pairs = (
        db.session.query(
            MovieCrew.credit_id.label("credit_id"),
            MovieCrew.movie_id.label("movie_id"),
        )
        .filter(MovieCrew.job.in_(list(CREW_ROLE_LABELS)))
        .filter(MovieCrew.movie_id.in_(owned_movie_ids))
    )
    tv_crew_pairs = (
        db.session.query(
            TVCrew.credit_id.label("credit_id"),
            (-TVCrew.tv_id).label("movie_id"),
        )
        .filter(TVCrew.job.in_(list(CREW_ROLE_LABELS)))
        .filter(TVCrew.tv_id.in_(owned_series_ids))
    )
    if role == "cast":
        return cast_pairs.union(tv_cast_pairs).subquery()
    if role == "crew":
        return crew_pairs.union(tv_crew_pairs).subquery()
    return cast_pairs.union(tv_cast_pairs, crew_pairs, tv_crew_pairs).subquery()


def _dominant_roles(credit_ids):
    """Return the dominant credited role of each person.

    That is the key role that covers the most distinct library works
    (films and series both). ROLE_PRECEDENCE breaks ties. Only the owned
    works count, with the rule of _credited_film_pairs. The role
    reads the cast and the crew credits together. The count of the page
    follows its cast or crew filter."""

    if not credit_ids:
        return {}
    owned_movie_ids, owned_series_ids = _owned_work_ids()
    counts = {}
    for credit_id, tally in (
        db.session.query(TVCast.credit_id, db.func.count(db.distinct(TVCast.tv_id)))
        .filter(TVCast.credit_id.in_(credit_ids))
        .filter(TVCast.tv_id.in_(owned_series_ids))
        .filter(
            db.or_(
                TVCast.character == None,
                db.not_(TVCast.character.like("%(uncredited)%")),
            )
        )
        .group_by(TVCast.credit_id)
    ):
        role_counts = counts.setdefault(credit_id, {})
        role_counts["Actor"] = role_counts.get("Actor", 0) + tally

    # Some roles have 2 job names, for example Screenplay and Writer. A
    # work that credits a person with both jobs counts 1 time for the
    # role. Thus, the crew credits collect the works of each
    # person and role label as a set. The ids of the series and of the
    # films overlap. Thus, each id in the set carries its kind of work.

    crew_works = {}
    for credit_id, job, tv_id in (
        db.session.query(TVCrew.credit_id, TVCrew.job, TVCrew.tv_id)
        .filter(TVCrew.credit_id.in_(credit_ids))
        .filter(TVCrew.tv_id.in_(owned_series_ids))
        .filter(TVCrew.job.in_(list(CREW_ROLE_LABELS)))
        .distinct()
    ):
        key = (credit_id, CREW_ROLE_LABELS[job])
        crew_works.setdefault(key, set()).add(("tv", tv_id))
    for credit_id, tally in (
        db.session.query(
            MovieCast.credit_id, db.func.count(db.distinct(MovieCast.movie_id))
        )
        .filter(MovieCast.credit_id.in_(credit_ids))
        .filter(MovieCast.movie_id.in_(owned_movie_ids))
        .filter(
            db.or_(
                MovieCast.character == None,
                db.not_(MovieCast.character.like("%(uncredited)%")),
            )
        )
        .group_by(MovieCast.credit_id)
    ):
        role_counts = counts.setdefault(credit_id, {})
        role_counts["Actor"] = role_counts.get("Actor", 0) + tally
    for credit_id, job, movie_id in (
        db.session.query(MovieCrew.credit_id, MovieCrew.job, MovieCrew.movie_id)
        .filter(MovieCrew.credit_id.in_(credit_ids))
        .filter(MovieCrew.movie_id.in_(owned_movie_ids))
        .filter(MovieCrew.job.in_(list(CREW_ROLE_LABELS)))
        .distinct()
    ):
        key = (credit_id, CREW_ROLE_LABELS[job])
        crew_works.setdefault(key, set()).add(("movie", movie_id))
    for (credit_id, label), works in crew_works.items():
        counts.setdefault(credit_id, {})[label] = len(works)
    return {
        credit_id: max(
            role_counts,
            key=lambda role: (role_counts[role], -ROLE_PRECEDENCE.index(role)),
        )
        for credit_id, role_counts in counts.items()
    }
