"""The TMDB fetch and apply half of the Movie and TVSeries models.

TMDBMixin holds the fetch and apply methods that Movie and TVSeries
share. tmdb_get is the one rate-limited GET of the TMDB API. The other
names are the pieces of the apply tables. app.models imports this
module before it defines the models. Thus, this module imports the
models only inside the functions that use them. app.models re-exports
every public name of this module."""

import traceback

from datetime import datetime, timezone
from time import sleep, time

import requests

from flask import current_app

from app import db
from app.email import notify_admin
from app.title_keys import title_key


def _clear_links(links):
    """Remove every link of a dynamic relationship of 1 record.

    This goes through the links of the record only. The old clear-outs
    loaded every row of the table and asked about each row with 1
    query. That was approximately 23,700 queries for each film (#295)."""

    for linked in links.all():
        links.remove(linked)


PEOPLE_RANKING_KEY = "fitzflix:people:ranked:{role}"

# The cache life of the /people ranking. This is a backstop. The
# credit writers invalidate the cache directly.

PEOPLE_RANKING_SECONDS = 7 * 86400


def invalidate_people_ranking():
    """Delete the cached rankings of the /people page (2026-08).

    The ranked list of every credited person is a full aggregation over
    the cast and crew tables. Thus, Redis holds the list, and Fitzflix
    builds it again only after a credit write. The TMDB apply methods
    call this function."""

    redis = current_app.redis
    keys = [PEOPLE_RANKING_KEY.format(role=role) for role in ("cast", "crew", "all")]
    redis.delete(*keys)


# The counter of the TMDB requests of 1 second, shared by every process.

TMDB_RATE_KEY = "fitzflix:tmdb:requests:{second}"


def tmdb_get(url, **kwargs):
    """GET a TMDB API resource through a shared rate limiter.

    TMDB limits the rate to approximately 40 to 50 requests per second
    per IP. Refer to https://developer.themoviedb.org/docs/rate-limiting
    for the rule. Every worker and web process shares a Redis counter
    keyed on the current
    second. Thus, their combined request rate stays at or below
    TMDB_REQUESTS_PER_SECOND, whatever the number of processes.
    """

    limit = current_app.config["TMDB_REQUESTS_PER_SECOND"]
    while True:
        now = time()
        bucket = TMDB_RATE_KEY.format(second=int(now))
        pipe = current_app.redis.pipeline()
        pipe.incr(bucket)
        pipe.expire(bucket, 3)
        if pipe.execute()[0] <= limit:
            break
        sleep(max(int(now) + 1 - now, 0.05))
    kwargs.setdefault("timeout", 30)
    return requests.get(url, **kwargs)


def tmdb_json(path, params=None, timeout=10):
    """GET 1 TMDB API path through tmdb_get and return the JSON body.

    path starts with a slash, for example "/movie/603". This function
    adds the URL of the API and the API key. An HTTP error raises
    requests.HTTPError. Thus, a caller can tell a 404 from an outage."""

    r = tmdb_get(
        current_app.config["TMDB_API_URL"] + path,
        params={"api_key": current_app.config["TMDB_API_KEY"], **(params or {})},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


def tmdb_json_or(path, params, default, timeout=10):
    """Return the JSON body of 1 TMDB GET, or default on any failure.

    The failure goes to the log as a warning with its traceback. Use
    tmdb_json for a caller that must tell a 404 from an outage."""

    try:
        return tmdb_json(path, params, timeout=timeout)
    except Exception:
        current_app.logger.warning(traceback.format_exc())
        return default


def tmdb_objects(entries, owner, what):
    """Yield the dict entries of a TMDB credits list.

    This function logs and skips all other entries. The overnight TV
    refresh of 2026-08-22 failed on 14 of 25 series. TMDB served a bare
    list where the role object of a cast member belongs. That lasted
    some seconds, and the data was correct again by noon. Before, one
    malformed entry stopped the full apply with an AttributeError and
    no record of the shape. Now Fitzflix logs the entry with its
    fragment and skips it. Thus, the rest of the payload still arrives.
    """

    for entry in entries or []:
        if isinstance(entry, dict):
            yield entry
        else:
            current_app.logger.warning(
                f"{owner} TMDB {what} entry is not an object, skipping: "
                f"{repr(entry)[:300]}"
            )


def _tmdb_date(value):
    """Return a TMDB date string as a datetime, or None for a blank value."""

    return datetime.strptime(value, "%Y-%m-%d") if value else None


def canonical_movie_name(tmdb_info, year):
    """Return the (title, year) that tmdb_movie_apply gives a movie for a payload (#603).

    The title is the TMDB title, shortened to the column of Movie.title.
    The year is the year of the release date of the payload, or year
    when the payload has no release date. The title is None when the
    payload has no title. tmdb_movie_apply can still keep the old name
    when another record holds the new one."""

    from app.models import Movie

    title = tmdb_info.get("title")
    title_length = Movie.__table__.c.title.type.length
    if title and len(title) > title_length:
        title = title[:title_length].rstrip()
    if tmdb_info.get("release_date"):
        year = _tmdb_date(tmdb_info["release_date"]).year
    return title, year


def pick_rating(entries, field, preferred=None):
    """Return the first non-blank value of field in a list of TMDB entries.

    The entries that preferred accepts come first, in their order. The
    movie certifications prefer the theatrical release (type 3). The
    premiere (type 1) usually has a blank certification (#303). The TV
    content ratings use the US entry."""

    entries = [entry for entry in entries or [] if isinstance(entry, dict)]
    if preferred:
        entries = sorted(entries, key=lambda entry: not preferred(entry))
    return next((entry.get(field) for entry in entries if entry.get(field)), None)


class TMDBAssociation:
    """1 association group of a TMDB apply: the rows that a record links to.

    rel is the dynamic relationship of the record. model is the name of
    the linked model class. path is the key path of the payload list. A
    payload object at the path counts as a list of 1. key is the payload
    field that holds the id of a row. fields maps each column of a new
    row to a payload field, or to a function of the payload entry. With
    refresh_fields, an existing row gets the fields again on each apply.
    prune_link is a link-table column. After the apply, each row that no
    link names goes (#305). no_guard lets an empty payload clear the
    links. Collections use it, because most films belong to no
    collection. resolve replaces the find-or-create of the rows. label
    names the group in the guard warning, and kept names what stays."""

    def __init__(
        self,
        rel,
        model,
        path,
        key="id",
        fields=None,
        label=None,
        kept="ones",
        refresh_fields=False,
        prune_link=None,
        no_guard=False,
        resolve=None,
    ):
        self.rel = rel
        self.model = model
        self.path = path
        self.key = key
        self.fields = fields or {"name": "name"}
        self.label = label or rel.replace("_", " ")
        self.kept = kept
        self.refresh_fields = refresh_fields
        self.prune_link = prune_link
        self.no_guard = no_guard
        self.resolve = resolve

    def entries(self, tmdb_info):
        """Return the payload list of the group. A missing list is empty."""

        value = tmdb_info
        for part in self.path:
            value = (value or {}).get(part) if isinstance(value, dict) else None
        # belongs_to_collection is one object, not a list. An empty object
        # at a list path is a glitch of the #251 class. It must read as
        # "nothing", or the guard below would let it clear the links.
        if isinstance(value, dict):
            return [value] if value else []
        return list(value or [])

    def values(self, entry):
        """Return the column values of a row from its payload entry."""

        return {
            column: (field(entry) if callable(field) else entry.get(field))
            for column, field in self.fields.items()
        }


# The 6 marks of the MPA rating system. The certification chip of a
# film shows only these (Glenn, 2026-09-17). The live TMDB paths (the
# review page and the card of a bare TMDB id) filter through this set.
# Thus, an NR or an Unrated film shows no chip there.

MPAA_RATINGS = frozenset({"G", "PG", "PG-13", "R", "NC-17", "X"})

# The TMDB statuses of a TV series whose run is over. Its season and
# episode counts are final (#482).

FINISHED_TV_STATUSES = ("Ended", "Canceled")


def release_certification(country_release):
    """Return the rating of 1 country entry of the TMDB release dates, or None.

    The theatrical release (type 3) comes first (pick_rating, #303).
    This is the 1 rule of the cataloger and of the live TMDB pages."""

    return pick_rating(
        country_release.get("release_dates"),
        "certification",
        preferred=lambda entry: entry.get("type") == 3,
    )


def us_certification_from(details):
    """Return the US rating of a TMDB movie payload, or None (#365).

    The theatrical release comes first, as in release_certification,
    the rule that the cataloger stores. The result is the first value
    in MPAA_RATINGS. A country entry with no MPAA value gives None."""

    for country_release in (details.get("release_dates") or {}).get("results") or []:
        if isinstance(country_release, dict) and (
            country_release.get("iso_3166_1") == "US"
        ):
            # An unrated theatrical run ("NR") must not hide the MPAA
            # mark of a later entry. Thus, the first MPAA value wins,
            # with the theatrical entries first.
            entries = [
                entry
                for entry in country_release.get("release_dates") or []
                if isinstance(entry, dict)
            ]
            entries.sort(key=lambda entry: entry.get("type") != 3)
            return next(
                (
                    entry.get("certification")
                    for entry in entries
                    if entry.get("certification") in MPAA_RATINGS
                ),
                None,
            )
    return None


def _movie_certifications(record, entries):
    """Return the certification rows of the release dates of a movie payload.

    Each country gives at most 1 rating (release_certification). A
    rating with no row in the table is not shown. The weekly sync adds
    the new ones."""

    from app.models import RefTMDBCertification

    rows = []
    for country_release in entries:
        country = country_release.get("iso_3166_1")
        if not country:
            continue
        rating = release_certification(country_release)
        if rating:
            certification = RefTMDBCertification.query.filter_by(
                country=country, certification=rating
            ).first()
            if certification:
                rows.append(certification)
    return rows


def _tmdb_credit(person):
    """Return the TMDBCredit row of a payload person. Make it if it is new."""

    from app.models import TMDBCredit

    credit = TMDBCredit.query.filter_by(id=person.get("id")).first()
    if not credit:
        credit = TMDBCredit(
            id=person.get("id"),
            name=person.get("name"),
            gender=person.get("gender"),
            tmdb_profile_path=person.get("profile_path"),
        )
        db.session.add(credit)
    return credit


class TMDBUnavailable(Exception):
    """TMDB answered a details request with an error that is not a 404 (#342)."""


def _raise_unless_not_found(record, tmdb_id, error):
    """Raise TMDBUnavailable for a TMDB error that is not a 404 (#342).

    Only a 404 says that the id does not exist. A 429 or a 5xx is an
    outage. It must not send the "TMDB ID not found" email for each record
    of a nightly refresh. The refresh stops and changes nothing."""

    status = error.response.status_code if error.response is not None else None
    if status != 404:
        current_app.logger.warning(
            f"{record} TMDB answered {status} for id {tmdb_id}. Nothing changed"
        )
        raise TMDBUnavailable(f"TMDB answered {status} for id {tmdb_id}") from error


class TMDBMixin(object):
    """TMDB fetch and apply methods shared by the Movie and TVSeries models.

    Each refresh has 2 halves. *_fetch does the network work and returns
    a payload. *_apply writes the payload to the database. Thus, the
    two-phase refresh tasks can run the halves on different queues.
    """

    # TMDB strings that are links or image paths. A shortened link is a
    # broken link. Thus, a value that is too long for its column is not
    # stored. Other text is shortened to fit.

    TMDB_LINK_COLUMNS = ("tmdb_homepage", "tmdb_backdrop_path", "tmdb_poster_path")

    def _fit_tmdb_strings(self):
        """Make each TMDB string value fit its VARCHAR column (#328).

        MariaDB runs with STRICT_TRANS_TABLES. A value that is too long
        stops the commit of the full refresh with error 1406. Thus, one
        long homepage link removed all the TMDB updates of the record.
        SQLite does not check the length, so the tests cannot see it."""

        for column in self.__table__.columns:
            name = column.key
            length = getattr(column.type, "length", None)
            if not length or not (name.startswith("tmdb_") or name == "imdb_id"):
                continue
            value = getattr(self, name, None)
            if not isinstance(value, str) or len(value) <= length:
                continue
            if name in self.TMDB_LINK_COLUMNS:
                current_app.logger.warning(
                    f"{self} TMDB {name} has {len(value)} characters, more "
                    f"than {length}. It is not stored: {value!r}"
                )
                setattr(self, name, None)
            else:
                current_app.logger.warning(
                    f"{self} TMDB {name} has {len(value)} characters, more "
                    f"than {length}. It is shortened"
                )
                setattr(self, name, value[:length])

    def _apply_association(self, spec, tmdb_info):
        """Replace the links of 1 association group from the payload.

        TMDB can serve a payload with an empty list for a short time,
        while the rest is intact. The bulk refreshes of 2026-08-07 to
        2026-08-13 got empty genre lists for approximately 16% of the
        requests. An unconditional delete then erased the genres of 943
        films (#251). Thus, an empty incoming list never deletes the
        links that the record has. Fitzflix keeps them and logs the
        anomaly. A spec with no_guard skips this check. Each clear-out
        goes through the links of this record only (#295)."""

        # A bare value in the list is skipped and logged, as for the
        # credits (#554). Before, 1 bare entry raised, and the whole
        # apply was lost until the next run.
        entries = list(tmdb_objects(spec.entries(tmdb_info), self, spec.label))
        links = getattr(self, spec.rel)
        if entries or spec.no_guard or links.count() == 0:
            _clear_links(links)
        else:
            current_app.logger.warning(
                f"{self} TMDB payload carries no {spec.label}; "
                f"keeping the stored {spec.kept}"
            )
            return
        if not entries:
            return

        from app import models

        model = getattr(models, spec.model)
        if spec.resolve:
            rows = spec.resolve(self, entries)
        else:
            rows = []
            for entry in entries:
                row = model.query.filter_by(id=entry.get(spec.key)).first()
                if not row:
                    row = model(id=entry.get(spec.key), **spec.values(entry))
                    db.session.add(row)
                elif spec.refresh_fields:
                    for column, value in spec.values(entry).items():
                        setattr(row, column, value)
                rows.append(row)
        for row in rows:
            if links.filter(model.id == row.id).count() == 0:
                links.append(row)

        if spec.prune_link is not None:
            db.session.flush()
            linked = db.session.query(spec.prune_link).filter(
                spec.prune_link.isnot(None)
            )
            model.query.filter(~model.id.in_(linked)).delete(synchronize_session=False)

    def _apply_scalars(self, tmdb_info):
        """Copy the external ids, the scalars and the dates of the payload.

        A scalar keeps its stored value if the KEY is absent (#252). A
        full details payload always carries every key, with null where
        TMDB has nothing. Thus, a missing key means a partial payload. It
        must not set a populated column to null. A key that is present
        with null still clears the column. That is TMDB that removes real
        data. A date changes only when the payload has a value."""

        external_ids = tmdb_info.get("external_ids")
        if external_ids:
            for column, field in self.EXTERNAL_ID_FIELDS:
                setattr(self, column, external_ids.get(field))
        for column, field in self.SCALAR_FIELDS:
            setattr(self, column, tmdb_info.get(field, getattr(self, column)))
        for column, field in self.DATE_FIELDS:
            if tmdb_info.get(field):
                setattr(self, column, _tmdb_date(tmdb_info.get(field)))

    def _clear_tmdb(self):
        """Remove every TMDB field and link that the apply tables name.

        The caller removes the credit rows. The record is then marked as
        ignored."""

        for spec in self.TMDB_ASSOCIATIONS:
            _clear_links(getattr(self, spec.rel))
        for column, _ in (
            self.EXTERNAL_ID_FIELDS + self.SCALAR_FIELDS + self.DATE_FIELDS
        ):
            setattr(self, column, None)
        for column in self.CLEARED_FIELDS:
            setattr(self, column, None)
        self.tmdb_ignored = True

    def tmdb_movie_fetch(self, tmdb_id=None):
        """Do the network half of a TMDB movie refresh.

        This method searches (if no id is given) and gets the movie
        details. It writes nothing to the database. Thus, concurrent
        fetches are safe. It returns the details payload for
        tmdb_movie_apply, or None if there is no match. Fitzflix does not
        store artwork. The templates link directly to the TMDB image
        CDN."""

        tmdb_info = {}
        if not current_app.config["TMDB_API_KEY"]:
            return None

        # Request only the appended blocks that tmdb_movie_apply reads.

        requested_info = "credits,external_ids,keywords,release_dates"
        # A bulk refresh fetches hundreds of movies a night. The apply
        # writes the 1 INFO line of each movie (#590, #605).
        current_app.logger.debug(f"{self} Getting TMDB data")
        if tmdb_id == None:
            search = tmdb_json(
                "/search/movie",
                {"query": self.title, "primary_release_year": self.year},
                timeout=30,
            )
            current_app.logger.debug(f"/search/movie: {search}")
            if len(search.get("results")) > 0:
                first_result = search.get("results")[0]
                tmdb_id = first_result.get("id")

        if tmdb_id:
            try:
                tmdb_info = tmdb_json(
                    f"/movie/{tmdb_id}",
                    {"append_to_response": requested_info},
                    timeout=30,
                )
            except requests.exceptions.HTTPError as e:
                _raise_unless_not_found(self, tmdb_id, e)
                notify_admin(
                    "TMDB ID not found",
                    "nonexistent_tmdb_id_movie",
                    movie=self,
                    tmdb_id=tmdb_id,
                )
                return None
            current_app.logger.debug(f"/movie/{tmdb_id}: {tmdb_info}")

        return tmdb_info or None

    def tmdb_movie_apply(self, tmdb_info):
        """Do the database half of a TMDB movie refresh.

        This method replaces the TMDB fields and associations of this
        movie with the fetched payload. It makes no network calls. The
        artwork is already on disk. Thus, it belongs on the single-worker
        sql queue, serialized with the other database work."""

        from app.models import Movie, MovieCast, MovieCrew

        if not tmdb_info:
            return self

        self._apply_scalars(tmdb_info)
        canonical_title, canonical_year = canonical_movie_name(tmdb_info, self.year)
        self._fit_tmdb_strings()

        # Rename this movie to the canonical TMDB title and year, unless a
        # different movie record already holds that name. Title + year is
        # unique. Thus, a rename onto that name would fail the full commit.
        # The 2 records then share a tmdb_id. Thus, a refresh of either
        # movie merges them through the existing duplicate-tmdb_id
        # handling.

        # Movie.title is narrower than tmdb_title. The title goes into
        # the library names, which have a byte limit. Thus, the record
        # title is shortened to its column, as _fit_tmdb_strings does for
        # the TMDB columns (#383). canonical_movie_name shortens it. The
        # title locks of a refresh use the same rule (#603).

        title_length = Movie.__table__.c.title.type.length
        if len(tmdb_info.get("title") or "") > title_length:
            current_app.logger.warning(
                f"{self} TMDB title has {len(tmdb_info['title'])} characters, "
                f"more than {title_length}. The record title is shortened"
            )

        duplicate = Movie.query.filter(
            Movie.title == canonical_title, Movie.year == canonical_year
        ).first()

        if duplicate is not None and duplicate is not self:
            current_app.logger.warning(
                f"{self} not renamed to '{canonical_title} ({canonical_year})': "
                f"{duplicate} already has that name; refresh either movie "
                f"with TMDB id {tmdb_info.get('id')} to merge them"
            )
            notify_admin(
                "Duplicate movie detected",
                "duplicate_movie",
                movie=self,
                duplicate=duplicate,
                canonical_title=canonical_title,
                canonical_year=canonical_year,
            )

        elif canonical_title:
            self.title = canonical_title
            self.year = canonical_year
        if tmdb_info.get("id"):
            self.tmdb_data_as_of = datetime.now(timezone.utc)
            # The record page shows "last updated" from this column (#465).
            self.date_updated = self.tmdb_data_as_of

        for spec in self.TMDB_ASSOCIATIONS:
            self._apply_association(spec, tmdb_info)

        # Cast and crew get the same empty-payload guard as the association
        # groups (#252). Before, the bulk delete ran before any payload
        # check. Thus, a bad payload with an empty or missing credits
        # section deleted the cast of a film permanently. This is the #251
        # failure shape. It hits the /people rankings, the cast criteria
        # shelves, and the Name That Frame distractors.

        incoming_credits = tmdb_info.get("credits") or {}
        for model, section in ((MovieCast, "cast"), (MovieCrew, "crew")):
            rows = model.query.filter_by(movie_id=self.id)
            if incoming_credits.get(section) or rows.count() == 0:
                rows.delete()
            else:
                current_app.logger.warning(
                    f"{self} TMDB payload carries no {section}; keeping the "
                    f"stored rows"
                )

        if tmdb_info.get("credits"):
            invalidate_people_ranking()
            credits = tmdb_info.get("credits")
            for person in tmdb_objects(credits.get("cast"), self, "cast"):
                credit = _tmdb_credit(person)
                if (
                    MovieCast.query.filter_by(
                        movie_id=self.id,
                        credit_id=credit.id,
                        character=person.get("character"),
                    ).count()
                    == 0
                ):
                    db.session.add(
                        MovieCast(
                            movie_id=self.id,
                            credit_id=credit.id,
                            character=person.get("character"),
                            billing_order=person.get("order"),
                        )
                    )

            for person in tmdb_objects(credits.get("crew"), self, "crew"):
                credit = _tmdb_credit(person)
                if (
                    MovieCrew.query.filter_by(
                        movie_id=self.id,
                        credit_id=credit.id,
                        department=person.get("department"),
                        job=person.get("job"),
                    ).count()
                    == 0
                ):
                    db.session.add(
                        MovieCrew(
                            movie_id=self.id,
                            credit_id=credit.id,
                            department=person.get("department"),
                            job=person.get("job"),
                        )
                    )

        return self

    def tmdb_movie_query(self, tmdb_id=None):
        """Fetch from TMDB and apply to the database in 1 step.

        This is for the callers outside the split refresh pipeline (for
        example, review_task when it creates a movie inline)."""

        return self.tmdb_movie_apply(self.tmdb_movie_fetch(tmdb_id))

    def tmdb_movie_clear(self):
        """Detach this film from TMDB.

        This method deletes the id, every fetched field, and every
        association that tmdb_movie_apply creates. Then it marks the
        record as ignored. Thus, no refresh path guesses a new id from
        the title.

        This is for the films that TMDB has no record of and never will.
        Examples are a home movie, or an id that TMDB deleted. The title
        and the year are the library identity of the film, not the TMDB
        identity. They stay unchanged.
        """

        from app.models import MovieCast, MovieCrew

        MovieCast.query.filter_by(movie_id=self.id).delete()
        MovieCrew.query.filter_by(movie_id=self.id).delete()
        invalidate_people_ranking()
        self._clear_tmdb()
        return self

    def tmdb_tv_fetch(self, tmdb_id=None):
        """Do the network half of a TMDB TV refresh. See tmdb_movie_fetch."""

        tmdb_info = {}
        if not current_app.config["TMDB_API_KEY"]:
            return None

        # Request only the appended blocks that tmdb_tv_apply reads.
        # Networks, companies, genres, and seasons arrive in the base
        # payload. Use aggregate_credits, not credits. It gives the
        # series-wide cast and crew with episode counts per role, not only
        # the cast and crew of the latest season.

        requested_info = "aggregate_credits,external_ids,keywords,content_ratings"
        current_app.logger.info(f"{self} Getting TMDB data")
        if tmdb_id == None:
            search = tmdb_json("/search/tv", {"query": self.title}, timeout=30)
            current_app.logger.debug(f"/search/tv: {search}")
            if len(search.get("results")) > 0:
                first_result = search.get("results")[0]
                tmdb_id = first_result.get("id")

        if tmdb_id:
            try:
                tmdb_info = tmdb_json(
                    f"/tv/{tmdb_id}",
                    {"append_to_response": requested_info},
                    timeout=30,
                )
            except requests.exceptions.HTTPError as e:
                _raise_unless_not_found(self, tmdb_id, e)
                notify_admin(
                    "TMDB ID not found",
                    "nonexistent_tmdb_id_tv",
                    tv=self,
                    tmdb_id=tmdb_id,
                )
                return None
            current_app.logger.debug(f"/tv/{tmdb_id}: {tmdb_info}")

        return tmdb_info or None

    def tmdb_tv_apply(self, tmdb_info):
        """Do the database half of a TMDB TV refresh. See tmdb_movie_apply."""

        from app.models import TVCast, TVCrew

        if not tmdb_info:
            return self

        self._apply_scalars(tmdb_info)
        # A finished run has final counts. TMDB marks it Ended or Canceled
        # (#482). A running series keeps no counts. A series that runs
        # again loses its old final counts (#554). A payload with no
        # status (a glitch) changes nothing.

        status = tmdb_info.get("status")
        if status in FINISHED_TV_STATUSES:
            self.tmdb_number_of_episodes = tmdb_info.get("number_of_episodes")
            self.tmdb_number_of_seasons = tmdb_info.get("number_of_seasons")
        elif status:
            self.tmdb_number_of_episodes = None
            self.tmdb_number_of_seasons = None

        # The US content rating is in the appended content_ratings block.
        # A payload with no US entry keeps the stored rating, the same as
        # the empty-list guards of the associations (#251).

        rating = pick_rating(
            [
                entry
                for entry in (tmdb_info.get("content_ratings") or {}).get("results")
                or []
                if isinstance(entry, dict) and entry.get("iso_3166_1") == "US"
            ],
            "rating",
        )
        if rating:
            self.tmdb_content_rating = rating
        self._fit_tmdb_strings()
        if tmdb_info.get("id"):
            self.tmdb_data_as_of = datetime.now(timezone.utc)
            # The record page shows "last updated" from this column (#465).
            self.date_updated = self.tmdb_data_as_of

        for spec in self.TMDB_ASSOCIATIONS:
            self._apply_association(spec, tmdb_info)

        # Series cast and crew. Replace the join rows of this series from
        # the aggregate credits, the same as the movie apply. Delete, then
        # add again. The presence of the block gates this. Thus, a payload
        # without the block cannot delete the stored credits. The seen
        # sets replace the per-row existence queries of the movie path.
        # After the bulk delete, only duplicates in the payload can
        # collide with the unique constraints. The keys are folded the way
        # utf8mb4_general_ci compares. That is without accents, without
        # case, and without trailing spaces. TMDB payloads really carry
        # both 'Self - Bee farmer' and 'Self - Bee Farmer' for one person.
        # Python sees 2 values. MySQL sees a 1062 duplicate.

        def collation_key(*parts):
            return tuple(title_key(part) for part in parts)

        if tmdb_info.get("aggregate_credits"):
            aggregate = tmdb_info.get("aggregate_credits")

            # This is the same guard as the movie side (#252). A section
            # that is present with an empty cast or crew list keeps the
            # stored rows.

            if (
                aggregate.get("cast")
                or TVCast.query.filter_by(tv_id=self.id).count() == 0
            ):
                TVCast.query.filter_by(tv_id=self.id).delete()
            else:
                current_app.logger.warning(
                    f"{self} TMDB payload carries no cast; keeping the stored rows"
                )
            if (
                aggregate.get("crew")
                or TVCrew.query.filter_by(tv_id=self.id).count() == 0
            ):
                TVCrew.query.filter_by(tv_id=self.id).delete()
            else:
                current_app.logger.warning(
                    f"{self} TMDB payload carries no crew; keeping the stored rows"
                )
            invalidate_people_ranking()

            seen_roles = set()
            for person in tmdb_objects(aggregate.get("cast"), self, "cast"):
                p = _tmdb_credit(person)

                for role in tmdb_objects(
                    person.get("roles"), self, f"cast {person.get('id')} role"
                ):
                    key = (p.id,) + collation_key(role.get("character"))
                    if key in seen_roles:
                        continue
                    seen_roles.add(key)
                    db.session.add(
                        TVCast(
                            tv_id=self.id,
                            credit_id=p.id,
                            character=role.get("character"),
                            billing_order=person.get("order"),
                            episode_count=role.get("episode_count"),
                        )
                    )

            seen_jobs = set()
            for person in tmdb_objects(aggregate.get("crew"), self, "crew"):
                p = _tmdb_credit(person)

                for job in tmdb_objects(
                    person.get("jobs"), self, f"crew {person.get('id')} job"
                ):
                    key = (p.id,) + collation_key(
                        person.get("department"), job.get("job")
                    )
                    if key in seen_jobs:
                        continue
                    seen_jobs.add(key)
                    db.session.add(
                        TVCrew(
                            tv_id=self.id,
                            credit_id=p.id,
                            department=person.get("department"),
                            job=job.get("job"),
                            episode_count=job.get("episode_count"),
                        )
                    )

        return self

    def tmdb_tv_clear(self):
        """Detach this series from TMDB. See tmdb_movie_clear."""

        from app.models import TVCast, TVCrew

        TVCast.query.filter_by(tv_id=self.id).delete()
        TVCrew.query.filter_by(tv_id=self.id).delete()
        invalidate_people_ranking()
        self._clear_tmdb()
        return self
