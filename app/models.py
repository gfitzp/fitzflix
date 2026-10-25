import hashlib
import hmac
import json
import os

from datetime import datetime, timezone
from time import sleep, time

import jwt
import requests

from rq.registry import ScheduledJobRegistry, StartedJobRegistry
from unidecode import unidecode

from werkzeug.security import generate_password_hash, check_password_hash
from flask import current_app
from flask_login import UserMixin
from sqlalchemy.orm import joinedload

from app import db, login
from app.email import notify_admin

movie_collections = db.Table(
    "movie_collections",
    db.Column("movie_id", db.Integer, db.ForeignKey("movie.id")),
    db.Column("collection_id", db.Integer, db.ForeignKey("tmdb_movie_collection.id")),
)


movie_genres = db.Table(
    "movie_genres",
    db.Column("movie_id", db.Integer, db.ForeignKey("movie.id")),
    db.Column("genre_id", db.Integer, db.ForeignKey("tmdb_genre.id")),
)


movie_keywords = db.Table(
    "movie_keywords",
    db.Column("movie_id", db.Integer, db.ForeignKey("movie.id")),
    db.Column("keyword_id", db.Integer, db.ForeignKey("tmdb_keyword.id")),
)


movie_production_companies = db.Table(
    "movie_production_companies",
    db.Column("movie_id", db.Integer, db.ForeignKey("movie.id")),
    db.Column("company_id", db.Integer, db.ForeignKey("tmdb_production_company.id")),
)


movie_production_countries = db.Table(
    "movie_production_countries",
    db.Column("movie_id", db.Integer, db.ForeignKey("movie.id")),
    db.Column("country_id", db.String(2), db.ForeignKey("tmdb_production_country.id")),
)


movie_spoken_languages = db.Table(
    "movie_spoken_languages",
    db.Column("movie_id", db.Integer, db.ForeignKey("movie.id")),
    db.Column("country_id", db.String(2), db.ForeignKey("tmdb_spoken_language.id")),
)


movie_certifications = db.Table(
    "movie_certifications",
    db.Column("movie_id", db.Integer, db.ForeignKey("movie.id")),
    db.Column(
        "certification_id", db.Integer, db.ForeignKey("ref_tmdb_certification.id")
    ),
)


tv_genres = db.Table(
    "tv_genres",
    db.Column("tv_id", db.Integer, db.ForeignKey("tv_series.id")),
    db.Column("genre_id", db.Integer, db.ForeignKey("tmdb_genre.id")),
)


tv_keywords = db.Table(
    "tv_keywords",
    db.Column("tv_id", db.Integer, db.ForeignKey("tv_series.id")),
    db.Column("keyword_id", db.Integer, db.ForeignKey("tmdb_keyword.id")),
)


tv_networks = db.Table(
    "tv_networks",
    db.Column("tv_id", db.Integer, db.ForeignKey("tv_series.id")),
    db.Column("network_id", db.Integer, db.ForeignKey("tmdb_network.id")),
)


tv_production_companies = db.Table(
    "tv_production_companies",
    db.Column("tv_id", db.Integer, db.ForeignKey("tv_series.id")),
    db.Column("company_id", db.Integer, db.ForeignKey("tmdb_production_company.id")),
)


tv_seasons = db.Table(
    "tv_seasons",
    db.Column("tv_id", db.Integer, db.ForeignKey("tv_series.id")),
    db.Column("season_id", db.Integer, db.ForeignKey("tmdb_season.id")),
)


def _clear_links(links):
    """Remove every link of a dynamic relationship of 1 record.

    This goes through the links of the record only. The old clear-outs
    loaded every row of the table and asked about each row with 1
    query. That was approximately 23,700 queries for each film (#295)."""

    for linked in links.all():
        links.remove(linked)


PEOPLE_RANKING_KEY = "fitzflix:people:ranked:{role}"


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


def _movie_certifications(record, entries):
    """Return the certification rows of the release dates of a movie payload.

    Each country gives at most 1 rating (pick_rating). A rating with no
    row in the table is not shown. The weekly sync adds the new ones."""

    rows = []
    for country_release in entries:
        country = country_release.get("iso_3166_1")
        if not country:
            continue
        rating = pick_rating(
            country_release.get("release_dates"),
            "certification",
            preferred=lambda entry: entry.get("type") == 3,
        )
        if rating:
            certification = RefTMDBCertification.query.filter_by(
                country=country, certification=rating
            ).first()
            if certification:
                rows.append(certification)
    return rows


def _tmdb_credit(person):
    """Return the TMDBCredit row of a payload person. Make it if it is new."""

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

        entries = spec.entries(tmdb_info)
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

        model = globals()[spec.model]
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
        tmdb_api_key = current_app.config["TMDB_API_KEY"]
        tmdb_api_url = current_app.config["TMDB_API_URL"]

        # Request only the appended blocks that tmdb_movie_apply reads.

        requested_info = "credits,external_ids,keywords,release_dates"
        current_app.logger.info(f"{self} Getting TMDB data")
        if tmdb_id == None:
            r = tmdb_get(
                tmdb_api_url + "/search/movie",
                params={
                    "api_key": tmdb_api_key,
                    "query": self.title,
                    "primary_release_year": self.year,
                },
            )
            r.raise_for_status()
            current_app.logger.debug(f"{r.url}: {r.json()}")
            if len(r.json().get("results")) > 0:
                first_result = r.json().get("results")[0]
                tmdb_id = first_result.get("id")

        if tmdb_id:
            try:
                r = tmdb_get(
                    tmdb_api_url + "/movie/" + str(tmdb_id),
                    params={
                        "api_key": tmdb_api_key,
                        "append_to_response": requested_info,
                    },
                )
                r.raise_for_status()
            except requests.exceptions.HTTPError as e:
                _raise_unless_not_found(self, tmdb_id, e)
                notify_admin(
                    "TMDB ID not found",
                    "nonexistent_tmdb_id_movie",
                    movie=self,
                    tmdb_id=tmdb_id,
                )
                return None
            current_app.logger.debug(f"{r.url}: {r.json()}")
            tmdb_info = r.json()

        return tmdb_info or None

    def tmdb_movie_apply(self, tmdb_info):
        """Do the database half of a TMDB movie refresh.

        This method replaces the TMDB fields and associations of this
        movie with the fetched payload. It makes no network calls. The
        artwork is already on disk. Thus, it belongs on the single-worker
        sql queue, serialized with the other database work."""

        if not tmdb_info:
            return self

        self._apply_scalars(tmdb_info)
        canonical_year = self.year
        if self.tmdb_release_date and tmdb_info.get("release_date"):
            canonical_year = self.tmdb_release_date.year
        self._fit_tmdb_strings()

        # Rename this movie to the canonical TMDB title and year, unless a
        # different movie record already holds that name. Title + year is
        # unique. Thus, a rename onto that name would fail the full commit.
        # The 2 records then share a tmdb_id. Thus, a refresh of either
        # movie merges them through the existing duplicate-tmdb_id
        # handling.

        canonical_title = tmdb_info.get("title")

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
        tmdb_api_key = current_app.config["TMDB_API_KEY"]
        tmdb_api_url = current_app.config["TMDB_API_URL"]

        # Request only the appended blocks that tmdb_tv_apply reads.
        # Networks, companies, genres, and seasons arrive in the base
        # payload. Use aggregate_credits, not credits. It gives the
        # series-wide cast and crew with episode counts per role, not only
        # the cast and crew of the latest season.

        requested_info = "aggregate_credits,external_ids,keywords,content_ratings"
        current_app.logger.info(f"{self} Getting TMDB data")
        if tmdb_id == None:
            r = tmdb_get(
                tmdb_api_url + "/search/tv",
                params={
                    "api_key": tmdb_api_key,
                    "query": self.title,
                },
            )
            r.raise_for_status()
            current_app.logger.debug(f"{r.url}: {r.json()}")
            if len(r.json().get("results")) > 0:
                first_result = r.json().get("results")[0]
                tmdb_id = first_result.get("id")

        if tmdb_id:
            try:
                r = tmdb_get(
                    tmdb_api_url + "/tv/" + str(tmdb_id),
                    params={
                        "api_key": tmdb_api_key,
                        "append_to_response": requested_info,
                    },
                )
                r.raise_for_status()
            except requests.exceptions.HTTPError as e:
                _raise_unless_not_found(self, tmdb_id, e)
                notify_admin(
                    "TMDB ID not found",
                    "nonexistent_tmdb_id_tv",
                    tv=self,
                    tmdb_id=tmdb_id,
                )
                return None
            current_app.logger.debug(f"{r.url}: {r.json()}")
            tmdb_info = r.json()

        return tmdb_info or None

    def tmdb_tv_apply(self, tmdb_info):
        """Do the database half of a TMDB TV refresh. See tmdb_movie_apply."""

        if not tmdb_info:
            return self

        self._apply_scalars(tmdb_info)
        if tmdb_info.get("status") == "Ended":
            self.tmdb_number_of_episodes = tmdb_info.get("number_of_episodes")
            self.tmdb_number_of_seasons = tmdb_info.get("number_of_seasons")

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
            return tuple(unidecode(part or "").casefold().strip() for part in parts)

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

        TVCast.query.filter_by(tv_id=self.id).delete()
        TVCrew.query.filter_by(tv_id=self.id).delete()
        invalidate_people_ranking()
        self._clear_tmdb()
        return self


class User(UserMixin, db.Model):
    """An account: credentials, admin flag, API key, and Plex mapping."""

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), index=True, unique=True)
    password_hash = db.Column(db.String(256))
    admin = db.Column(db.Boolean, default=False)
    api_key = db.Column(db.String(32))

    # The Plex account name of this user. Thus, Fitzflix can attribute a
    # Plex watch to this user. An unmapped watcher still counts toward the
    # household shopping-cart priority.

    plex_username = db.Column(db.String(64), unique=True)

    # Bookkeeping for the review export. This is the time of the last
    # Letterboxd CSV export and the highest review id that it covered.
    # Thus, the default export can emit only the entries added or edited
    # after that. Fitzflix detects new rows by id because date_watched can
    # be a date before the last export.

    date_reviews_exported = db.Column(db.DateTime)
    last_export_review_id = db.Column(db.Integer)

    # The Letterboxd account whose RSS feed syncs into the diary of this
    # user. An empty value disables the poll for this user.

    letterboxd_username = db.Column(db.String(64))

    # The Plex playback device of this user. This is the Companion address
    # (ip:port) and the machine id of the player that their play buttons
    # target. It is per user. Each household member sends films to their
    # own screen. An empty value hides the play buttons for this user. The
    # Profile page sets it. The page probes the address and fills the
    # machine id itself.

    plex_player_address = db.Column(db.String(64))
    plex_player_id = db.Column(db.String(64))

    @property
    def plex_player_configured(self):
        """Return True if this user has a playback device for films."""

        return bool(self.plex_player_address and self.plex_player_id)

    # The Infuse target of this user. This is the Companion-protocol
    # address of the Apple TV (ip:port) and the pyatv credentials from the
    # one-time PIN pairing on the Profile page (#192). These fields are
    # separate from the Plex player fields. Fitzflix drives Infuse over
    # the Apple Companion protocol, not Plex Companion. A user can enable
    # one app or both apps.

    infuse_player_address = db.Column(db.String(64))
    infuse_player_credentials = db.Column(db.String(512))

    # The app that the plain play buttons target when BOTH apps are
    # configured. The value is "plex" or "infuse" (a Profile page
    # setting). Fitzflix ignores it while only one app is configured.
    # Then that app wins.

    default_player = db.Column(db.String(8))

    @property
    def infuse_player_configured(self):
        """Return True if this user has a paired Apple TV that can open Infuse."""

        return bool(self.infuse_player_address and self.infuse_player_credentials)

    @property
    def preferred_player(self):
        """Return the app that a plain (no-choice) play button targets.

        The value is "plex" or "infuse". It is the configured app. If
        both apps are configured, it is the default that the user chose.
        It is None if no player is configured."""

        players = [
            player
            for player, configured in (
                ("plex", self.plex_player_configured),
                ("infuse", self.infuse_player_configured),
            )
            if configured
        ]
        if not players:
            return None
        if len(players) == 1:
            return players[0]
        return self.default_player if self.default_player in players else "plex"

    # Watchlist availability alerts (#156/#230). The nightly digest email
    # is strictly opt-in. It is the only per-user mail other than the
    # password resets. Rentals are a second opt-in on top. A rental costs
    # an extra fee. Thus, it must not read as "available" unless the user
    # asked for that.

    notify_availability = db.Column(
        db.Boolean, nullable=False, default=False, server_default="0"
    )
    notify_rentals = db.Column(
        db.Boolean, nullable=False, default=False, server_default="0"
    )

    # The streaming services that this user subscribes to. The
    # availability displays are per user, never site-wide.

    streaming_providers = db.relationship(
        "UserStreamingProvider",
        backref="user",
        lazy="dynamic",
        cascade="all,delete,delete-orphan",
    )

    # The films that this user wants to watch. This is the stage before
    # the shopping list.

    watchlist = db.relationship(
        "UserWatchlist",
        backref="user",
        lazy="dynamic",
        cascade="all,delete,delete-orphan",
    )

    def __repr__(self):
        return f"<User '{self.email}'>"

    def set_password(self, password):
        """Store a salted hash of the password."""

        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        """Return True if the password matches the stored hash."""

        return check_password_hash(self.password_hash, password)

    def password_fingerprint(self):
        """Return a keyed fingerprint of the password hash.

        A new password changes the fingerprint. The reset token and the
        login session carry it. Thus, a password change makes each older
        token and session not valid."""

        return hmac.new(
            current_app.config["SECRET_KEY"].encode(),
            (self.password_hash or "").encode(),
            hashlib.sha256,
        ).hexdigest()[:32]

    def get_id(self):
        """Return the session id of flask-login: the user id and the password fingerprint."""

        return f"{self.id}:{self.password_fingerprint()}"

    def get_reset_password_token(self, expires_in=600):
        """Return a signed, short-lived JWT for the password-reset email link.

        The token holds the password fingerprint. Thus, it works 1 time."""

        return jwt.encode(
            {
                "reset_password": self.id,
                "password": self.password_fingerprint(),
                "exp": time() + expires_in,
            },
            current_app.config["SECRET_KEY"],
            algorithm="HS256",
        )

    @staticmethod
    def verify_reset_password_token(token):
        """Return the User that a reset token identifies, or None.

        The result is None if the token is not valid, has expired, or was
        used. A used token holds the fingerprint of the old password."""

        try:
            claims = jwt.decode(
                token, current_app.config["SECRET_KEY"], algorithms=["HS256"]
            )
            user = db.session.get(User, int(claims["reset_password"]))
        except Exception:
            return None
        if user is None or not hmac.compare_digest(
            str(claims.get("password") or ""), user.password_fingerprint()
        ):
            return None
        return user

    def _listed_queues(self):
        """Return the queues that the queue page lists, each with a job filter.

        The filter is None for a queue whose jobs all list. The SQL queue
        also runs the cataloging of each import. Its other work (the SQL
        refreshes) is not file work. Thus, only its pipeline stages list.
        Before, a file that was in cataloging had no job row. Its trail
        showed as a "no job in queue" row at the end of the table."""

        from app.pipeline import STAGES

        return (
            (current_app.handoff_queue, None),
            (current_app.import_queue, None),
            (current_app.transcode_queue, None),
            (current_app.file_queue, None),
            (current_app.sql_queue, STAGES),
        )

    def _listed_jobs(self):
        """Return the running jobs and the queued jobs that the queue page lists.

        Each list holds the rq jobs in the order of the queues and then
        of each queue."""

        running = []
        queued = []
        for queue, only in self._listed_queues():
            for target, job_ids in (
                (running, StartedJobRegistry(queue=queue).get_job_ids()),
                (queued, queue.job_ids),
            ):
                for job_id in job_ids:
                    job = queue.fetch_job(job_id)
                    if job is not None and (only is None or job.func_name in only):
                        target.append(job)
        return running, queued

    def get_queue_details(self):
        """Return the running and queued background jobs for the queue page.

        This method merges the pipeline queues into one ordered list with
        queue positions. The running banners at the top of each page and
        the running rows of the queue table have the same order.
        """

        # The running banners and the running rows keep their order by
        # the time that each FILE first started to run. This is the
        # original banner-order request from Glenn. The first file to
        # start is at the top. A newer file comes below it. The work of a
        # file steps between queues as it progresses. Each step is a new
        # job with a new started_at. Before, the table sorted by that
        # time. Thus, a file moved down the table at each step, and the
        # table and the banners did not agree. The first_run anchor of
        # the pipeline trail survives the steps. A job without a trail
        # uses its own start.

        from app.pipeline import first_enqueued, first_run, front_times

        def file_started(job):
            """Return the time that the FILE of the job first started to run.

            The anchor of the trail is local wall clock. The value is
            aware UTC, the same as the own started_at of rq. flask-moment
            reads each time as UTC."""

            anchor = first_run(current_app.redis, job)
            if anchor:
                try:
                    local = datetime.strptime(anchor, "%Y-%m-%d %H:%M:%S")
                    return local.astimezone().astimezone(timezone.utc)
                except ValueError:
                    pass
            return job.started_at

        def enqueue_anchor(job):
            """Return the time that the FILE of the job first entered the pipeline.

            The Enqueued column stays the same as the work steps between
            queues. Each step is a new job with a new enqueued_at. A job
            outside the pipeline keeps its own enqueue time."""

            return first_enqueued(current_app.redis, job) or job.enqueued_at

        def banner_worthy(job):
            """Return True if a running job gets an alert at the top of the page.

            Frame pool work never gets one (Glenn, 2026-08-27). A
            replacement banner per round disrupts the game. It also shows
            that the pool changed. Even the 'Extracting a frame from X'
            banner of the nightly batch names films that will become
            answers. All these jobs still list on the queue page.

            The SQL worker never gets one either (Glenn, 2026-10-14). Its
            cataloging jobs list as rows only. Thus, the banners stay at
            1 for each of the 5 pipeline workers."""

            if job.origin == current_app.sql_queue.name:
                return False
            return not (job.func_name or "").startswith("app.frames.")

        def row(job, started_at):
            """Return the queue-page fields of 1 job."""

            return {
                "id": job.id,
                "status": job.get_status(),
                "enqueued_at": enqueue_anchor(job),
                "started_at": started_at,
                "ended_at": job.ended_at,
                "description": job.meta.get("description", job.description),
                # A check or finish job of a multi-job run (the Atmos twin)
                # shows the chips of the first job of the run. A booked
                # check names its wait ("checks again"), not "retries".
                "trail_job": job.meta.get("trail_job"),
                "scheduled_word": job.meta.get("scheduled_word"),
            }

        def time_key(value):
            """Return a sort key that puts a missing time last."""

            return (value is None, value or datetime.min.replace(tzinfo=timezone.utc))

        running_jobs, queued_jobs = self._listed_jobs()

        running = []
        for job in running_jobs:
            started_at = file_started(job)
            item = row(job, started_at)
            item["first_run"] = (
                started_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
                if started_at
                else "9999"
            )
            item["progress"] = job.meta.get("progress", -1)
            running.append((job, item))

        # The sort is stable. Thus, 2 files with the same start keep the
        # same order in the banners and in the table.

        running.sort(key=lambda pair: time_key(pair[1]["started_at"]))

        details = {}
        details["count"] = len(running_jobs) + len(queued_jobs)
        details["running"] = [item for job, item in running if banner_worthy(job)]

        # A job that an admin moved to the front of its queue (#236) lists
        # before the other waiting jobs. The newest move is at the top,
        # because each move pushes the job onto the head of its queue.

        # The job at the head of its queue gets no front button. A move
        # does not change which job of that queue a worker takes next.

        fronted = front_times(current_app.redis, [job.id for job in queued_jobs])
        heads = set()
        for queue, only in self._listed_queues():
            head = current_app.redis.lindex(queue.key, 0)
            if head is not None:
                heads.add(head.decode() if isinstance(head, bytes) else head)
        queued = []
        for job in queued_jobs:
            item = row(job, None)
            item["fronted"] = job.id in fronted
            item["at_head"] = job.id in heads
            queued.append(item)
        queued.sort(
            key=lambda item: (
                (0, -fronted[item["id"]])
                if item["fronted"]
                else (1, time_key(item["enqueued_at"]))
            )
        )

        details["all"] = [
            {
                key: value
                for key, value in item.items()
                if key not in ("first_run", "progress")
            }
            for job, item in running
        ] + queued
        for i, task in enumerate(details["all"]):
            task["position"] = i + 1

        # A deferred retry (a file that is still copying in, or a locked
        # title) is in the ScheduledJobRegistry of each queue. It is not
        # in the queue itself. Before, these retries were not visible
        # here. They showed only as amber chips on the in-flight list of
        # the File Activity page. The trail chips moved onto the queue
        # rows (Glenn, 2026-08). Now the queue page is the one place that
        # shows all the work in flight. Thus, the retries list too. They
        # come after the live queue, with no position, because they are
        # not in line yet.

        scheduled = []
        for queue, only in self._listed_queues():
            registry = ScheduledJobRegistry(queue=queue)
            for job_id in registry.get_job_ids():
                job = queue.fetch_job(job_id)
                if job is not None and (only is None or job.func_name in only):
                    item = row(job, None)
                    item["status"] = "scheduled"
                    item["ended_at"] = None
                    item["scheduled_for"] = registry.get_scheduled_time(job_id)
                    item["position"] = None
                    scheduled.append(item)

        scheduled.sort(
            key=lambda d: (d["scheduled_for"] is None, d["scheduled_for"] or "")
        )
        details["all"].extend(scheduled)

        return details

    def get_queue_count(self):
        """Return the total of queued and running jobs for the navbar badge.

        The badge counts the same jobs that the queue page lists."""

        running, queued = self._listed_jobs()
        return len(running) + len(queued)


class UserStreamingProvider(db.Model):
    """One streaming service on the profile of a user.

    The service comes from the watch-provider registry of TMDB (the
    source data is from JustWatch). Fitzflix copies the name and the
    logo when the user picks the service. Thus, the displays survive a
    registry outage."""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    provider_id = db.Column(db.Integer, nullable=False)
    name = db.Column(db.String(64))
    logo_path = db.Column(db.String(64))

    __table_args__ = (db.UniqueConstraint("user_id", "provider_id"),)

    def __repr__(self):
        return f"<UserStreamingProvider '{self.user_id}:{self.name}'>"


class UserWatchlist(db.Model):
    """A film that the user wants to watch.

    This is the funnel stage before the shopping list. An add uses the
    review-only Movie records again. Thus, a watchlisted film is
    enriched and first-class on every surface. A watch of the film (a
    manual log, a Plex scrobble, or a Letterboxd import) removes the
    entry. The film then moves to the shopping list naturally through
    the likes and ratings that the shopping list already reads. The
    timestamps are local wall-clock, the same as the diary.
    """

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    movie_id = db.Column(db.Integer, db.ForeignKey("movie.id"), nullable=False)
    date_added = db.Column(db.DateTime, nullable=False, default=datetime.now)

    __table_args__ = (db.UniqueConstraint("user_id", "movie_id"),)

    def __repr__(self):
        return f"<UserWatchlist '{self.user_id}:{self.movie_id}'>"


class UserMovieReview(db.Model):
    """One viewing.

    This is a diary or review row from the movie page, a Letterboxd
    import, or a Plex watch.
    """

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    movie_id = db.Column(db.Integer, db.ForeignKey("movie.id"))

    # The rating fields are nullable. A Letterboxd review or like can
    # exist without a star rating.

    rating = db.Column(db.Float)
    modified_rating = db.Column(db.Float)
    whole_stars = db.Column(db.Integer)
    half_stars = db.Column(db.Integer)
    review = db.Column(db.Text)
    liked = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    date_watched = db.Column(db.DateTime)
    date_reviewed = db.Column(db.DateTime)

    # The time of the last edit of the row: the stars, the text, or the
    # watch date. date_reviewed keeps the original review date. The
    # incremental Letterboxd export selects the edited rows by this time.

    date_updated = db.Column(db.DateTime)

    # True for a repeat viewing. False for a first watch. NULL for a
    # legacy row where nobody knows.

    rewatch = db.Column(db.Boolean)

    # The Letterboxd feed item that this row came from or matched. This
    # is the key for duplicate removal and edits. A row with a guid never
    # exports to Letterboxd again. It is already there.

    letterboxd_guid = db.Column(db.String(64), unique=True)

    # The spoiler checkbox of Letterboxd. It is known only for rows synced
    # from the feed (the CSV export has no spoiler column. Thus, an import
    # leaves it NULL). For now, Fitzflix stores it as data. If it changes
    # the review display is a separate, open question.

    contains_spoilers = db.Column(db.Boolean)

    def __repr__(self):
        return f"<UserMovieReview '{self.user_id}:{self.movie_id}:{self.rating}'>"


class Movie(db.Model, TMDBMixin):
    """A film.

    The row holds the local identity, the TMDB enrichment, the Criterion
    details, and the shopping-cart state.
    """

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(220), nullable=False, index=True)
    year = db.Column(db.Integer, nullable=False, index=True)
    date_created = db.Column(
        db.DateTime, nullable=False, index=True, default=db.func.utc_timestamp()
    )
    date_updated = db.Column(db.DateTime, index=True)

    imdb_id = db.Column(db.String(16))

    tmdb_id = db.Column(db.Integer)
    tmdb_adult = db.Column(db.Boolean)
    tmdb_backdrop_path = db.Column(db.String(64))
    tmdb_budget = db.Column(db.Integer)
    tmdb_homepage = db.Column(db.String(128))
    tmdb_original_language = db.Column(db.String(16))
    tmdb_original_title = db.Column(db.String(256))
    tmdb_overview = db.Column(db.Text)
    tmdb_popularity = db.Column(db.Float)
    tmdb_poster_path = db.Column(db.String(64))
    tmdb_release_date = db.Column(db.DateTime)
    tmdb_revenue = db.Column(db.BigInteger)  # BIGINT. Titanic (1996): $2,187,463,944
    tmdb_runtime = db.Column(db.Integer)
    tmdb_status = db.Column(db.String(32))
    tmdb_tagline = db.Column(db.String(384))
    tmdb_title = db.Column(db.String(256))
    tmdb_video = db.Column(db.Boolean)
    tmdb_vote_average = db.Column(db.Float)
    tmdb_vote_count = db.Column(db.Integer)
    tmdb_data_as_of = db.Column(db.DateTime)

    # "TMDB has no record of this film, and never will". Examples are a
    # home movie, or an id that TMDB deleted. This is different from a
    # plain NULL tmdb_id. A NULL only means "not matched yet" and starts a
    # title search on the next refresh. This flag tells every refresh path
    # not to touch the record. An id supplied by hand clears the flag.

    tmdb_ignored = db.Column(
        db.Boolean, nullable=False, default=False, server_default="0"
    )

    criterion_spine_number = db.Column(db.Integer)
    criterion_film_id = db.Column(db.String(255))
    criterion_set_title = db.Column(db.String(512))
    criterion_in_print = db.Column(db.Boolean)
    criterion_disc_owned = db.Column(db.Boolean)
    criterion_quality_id = db.Column(db.Integer, db.ForeignKey("ref_quality.id"))

    shopping_list_exclude = db.Column(db.Boolean)
    shopping_cart_add_date = db.Column(db.DateTime)
    shopping_cart_priority = db.Column(db.Integer)

    custom_poster = db.Column(db.String(64))

    files = db.relationship(
        "File", backref="movie", lazy="dynamic", cascade="all,delete,delete-orphan"
    )
    ratings = db.relationship(
        "UserMovieReview",
        backref="movie",
        lazy="dynamic",
        cascade="all,delete,delete-orphan",
    )
    awards = db.relationship(
        "MovieAward",
        backref="movie",
        lazy="dynamic",
        cascade="all,delete,delete-orphan",
    )
    watchlist_entries = db.relationship(
        "UserWatchlist",
        backref="movie",
        lazy="dynamic",
        cascade="all,delete,delete-orphan",
    )
    statuses = db.relationship(
        "UserMovieStatus",
        backref="movie",
        lazy="dynamic",
        cascade="all,delete,delete-orphan",
    )
    cast = db.relationship(
        "MovieCast", backref="movie", lazy="dynamic", cascade="all,delete,delete-orphan"
    )
    crew = db.relationship(
        "MovieCrew", backref="movie", lazy="dynamic", cascade="all,delete,delete-orphan"
    )

    # The TMDB rows of these relations are shared by many films and
    # series. A delete of a movie removes only its link rows. A delete
    # cascade removed the genre, the certification, and so on from the
    # whole library (Oct 24 2026 review).

    collections = db.relationship(
        "TMDBMovieCollection",
        secondary=movie_collections,
        backref=db.backref("movies", lazy="dynamic"),
        lazy="dynamic",
    )
    genres = db.relationship(
        "TMDBGenre",
        secondary=movie_genres,
        backref=db.backref("movies", lazy="dynamic"),
        lazy="dynamic",
    )
    keywords = db.relationship(
        "TMDBKeyword",
        secondary=movie_keywords,
        backref=db.backref("movies", lazy="dynamic"),
        lazy="dynamic",
    )
    production_companies = db.relationship(
        "TMDBProductionCompany",
        secondary=movie_production_companies,
        backref=db.backref("movies", lazy="dynamic"),
        lazy="dynamic",
    )
    production_countries = db.relationship(
        "TMDBProductionCountry",
        secondary=movie_production_countries,
        backref=db.backref("movies", lazy="dynamic"),
        lazy="dynamic",
    )
    spoken_languages = db.relationship(
        "TMDBSpokenLanguage",
        secondary=movie_spoken_languages,
        backref=db.backref("movies", lazy="dynamic"),
        lazy="dynamic",
    )
    certifications = db.relationship(
        "RefTMDBCertification",
        secondary=movie_certifications,
        backref=db.backref("movies", lazy="dynamic"),
        lazy="dynamic",
    )

    __table_args__ = (db.UniqueConstraint("title", "year"),)

    # The apply tables of the TMDB mixin. tmdb_movie_clear clears the
    # same columns and links.

    EXTERNAL_ID_FIELDS = (("imdb_id", "imdb_id"),)
    SCALAR_FIELDS = (
        ("tmdb_id", "id"),
        ("tmdb_adult", "adult"),
        ("tmdb_backdrop_path", "backdrop_path"),
        ("tmdb_budget", "budget"),
        ("tmdb_homepage", "homepage"),
        ("tmdb_original_language", "original_language"),
        ("tmdb_original_title", "original_title"),
        ("tmdb_overview", "overview"),
        ("tmdb_popularity", "popularity"),
        ("tmdb_poster_path", "poster_path"),
        ("tmdb_revenue", "revenue"),
        ("tmdb_runtime", "runtime"),
        ("tmdb_status", "status"),
        ("tmdb_tagline", "tagline"),
        ("tmdb_title", "title"),
        ("tmdb_video", "video"),
        ("tmdb_vote_average", "vote_average"),
        ("tmdb_vote_count", "vote_count"),
    )
    DATE_FIELDS = (("tmdb_release_date", "release_date"),)
    CLEARED_FIELDS = ("tmdb_data_as_of",)
    TMDB_ASSOCIATIONS = (
        TMDBAssociation(
            "collections",
            "TMDBMovieCollection",
            ("belongs_to_collection",),
            fields={
                "tmdb_backdrop_path": "backdrop_path",
                "name": "name",
                "tmdb_poster_path": "poster_path",
            },
            no_guard=True,
        ),
        TMDBAssociation("genres", "TMDBGenre", ("genres",)),
        TMDBAssociation("keywords", "TMDBKeyword", ("keywords", "keywords")),
        TMDBAssociation(
            "production_companies",
            "TMDBProductionCompany",
            ("production_companies",),
            fields={
                "name": "name",
                "country": "origin_country",
                "tmdb_logo_path": "logo_path",
            },
        ),
        TMDBAssociation(
            "production_countries",
            "TMDBProductionCountry",
            ("production_countries",),
            key="iso_3166_1",
        ),
        TMDBAssociation(
            "spoken_languages",
            "TMDBSpokenLanguage",
            ("spoken_languages",),
            key="iso_639_1",
        ),
        TMDBAssociation(
            "certifications",
            "RefTMDBCertification",
            ("release_dates", "results"),
            label="release dates",
            kept="certifications",
            resolve=_movie_certifications,
        ),
    )

    @property
    def display_title(self):
        """Return the site-wide display form: the TMDB title and year if known.

        The year comes from the TMDB release date only when the TMDB
        title is known. Without a TMDB title, the local title and year
        show."""

        title = self.tmdb_title or self.title
        year = (
            self.tmdb_release_date.strftime("%Y")
            if self.tmdb_title and self.tmdb_release_date
            else self.year
        )
        return f"{title} ({year})"

    def __repr__(self):
        return f"<Movie '{self.title} ({self.year})'>"


class TVSeries(db.Model, TMDBMixin):
    """A TV series and its TMDB enrichment."""

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(220), nullable=False, unique=True, index=True)
    date_created = db.Column(
        db.DateTime, nullable=False, index=True, default=db.func.utc_timestamp()
    )
    date_updated = db.Column(db.DateTime, index=True)

    imdb_id = db.Column(db.String(16))

    tmdb_id = db.Column(db.Integer)
    tmdb_backdrop_path = db.Column(db.String(64))
    # The US content rating (TV-PG, TV-MA, ...). This is the TV version
    # of the certifications table on the movie side. It holds one country
    # only because no surface shows the other countries.
    tmdb_content_rating = db.Column(db.String(32))
    tmdb_first_air_date = db.Column(db.DateTime)
    tmdb_homepage = db.Column(db.String(128))
    tmdb_poster_path = db.Column(db.String(64))
    tmdb_in_production = db.Column(db.Boolean)
    tmdb_last_air_date = db.Column(db.DateTime)
    tmdb_name = db.Column(db.String(256))
    tmdb_number_of_seasons = db.Column(db.Integer)
    tmdb_number_of_episodes = db.Column(db.Integer)
    tmdb_original_language = db.Column(db.String(16))
    tmdb_original_name = db.Column(db.String(256))
    tmdb_overview = db.Column(db.Text)
    tmdb_popularity = db.Column(db.Float)
    tmdb_status = db.Column(db.String(32))
    tmdb_type = db.Column(db.String(32))
    tmdb_vote_average = db.Column(db.Float)
    tmdb_vote_count = db.Column(db.Integer)
    tmdb_data_as_of = db.Column(db.DateTime)

    # See Movie.tmdb_ignored

    tmdb_ignored = db.Column(
        db.Boolean, nullable=False, default=False, server_default="0"
    )

    tvdb_id = db.Column(db.Integer)

    files = db.relationship(
        "File", backref="tv_series", lazy="dynamic", cascade="all,delete,delete-orphan"
    )
    cast = db.relationship(
        "TVCast",
        backref="tv_series",
        lazy="dynamic",
        cascade="all,delete,delete-orphan",
    )
    crew = db.relationship(
        "TVCrew",
        backref="tv_series",
        lazy="dynamic",
        cascade="all,delete,delete-orphan",
    )
    genres = db.relationship(
        "TMDBGenre",
        secondary=tv_genres,
        backref=db.backref("tv_series", lazy="dynamic"),
        lazy="dynamic",
    )
    keywords = db.relationship(
        "TMDBKeyword",
        secondary=tv_keywords,
        backref=db.backref("tv_series", lazy="dynamic"),
        lazy="dynamic",
    )
    networks = db.relationship(
        "TMDBNetwork",
        secondary=tv_networks,
        backref=db.backref("tv_series", lazy="dynamic"),
        lazy="dynamic",
    )
    production_companies = db.relationship(
        "TMDBProductionCompany",
        secondary=tv_production_companies,
        backref=db.backref("tv_series", lazy="dynamic"),
        lazy="dynamic",
    )
    # No delete cascade: 2 series records with 1 TMDB id share their
    # season rows. The cascade deleted the shared rows (#305), as it did
    # the shared genres before 423b475. tmdb_tv_apply removes the season
    # rows that no series links to.

    seasons = db.relationship(
        "TMDBSeason",
        secondary=tv_seasons,
        backref=db.backref("tv_series", lazy="dynamic"),
        lazy="dynamic",
    )

    # The apply tables of the TMDB mixin. The episode counts change only
    # for an ended series, and the content rating has its own pick.

    EXTERNAL_ID_FIELDS = (("imdb_id", "imdb_id"), ("tvdb_id", "tvdb_id"))
    SCALAR_FIELDS = (
        ("tmdb_id", "id"),
        ("tmdb_backdrop_path", "backdrop_path"),
        ("tmdb_homepage", "homepage"),
        ("tmdb_in_production", "in_production"),
        ("tmdb_name", "name"),
        ("tmdb_original_language", "original_language"),
        ("tmdb_original_name", "original_name"),
        ("tmdb_overview", "overview"),
        ("tmdb_popularity", "popularity"),
        ("tmdb_poster_path", "poster_path"),
        ("tmdb_status", "status"),
        ("tmdb_type", "type"),
        ("tmdb_vote_average", "vote_average"),
        ("tmdb_vote_count", "vote_count"),
    )
    DATE_FIELDS = (
        ("tmdb_first_air_date", "first_air_date"),
        ("tmdb_last_air_date", "last_air_date"),
    )
    CLEARED_FIELDS = (
        "tmdb_content_rating",
        "tmdb_number_of_seasons",
        "tmdb_number_of_episodes",
        "tmdb_data_as_of",
    )
    TMDB_ASSOCIATIONS = (
        TMDBAssociation("genres", "TMDBGenre", ("genres",)),
        TMDBAssociation("keywords", "TMDBKeyword", ("keywords", "results")),
        TMDBAssociation(
            "networks",
            "TMDBNetwork",
            ("networks",),
            fields={
                "tmdb_logo_path": "logo_path",
                "name": "name",
                "origin_country": "origin_country",
            },
        ),
        TMDBAssociation(
            "production_companies",
            "TMDBProductionCompany",
            ("production_companies",),
            fields={
                "name": "name",
                "country": "origin_country",
                "tmdb_logo_path": "logo_path",
            },
        ),
        # The season fields change on every refresh, not only at the
        # creation. The create-only code kept episode_count frozen at the
        # value of the first sight. A season that TMDB renumbered, or the
        # season of a deleted series, has no link after the apply. It
        # goes (#305).
        TMDBAssociation(
            "seasons",
            "TMDBSeason",
            ("seasons",),
            fields={
                "air_date": lambda season: _tmdb_date(season.get("air_date")),
                "episode_count": "episode_count",
                "name": "name",
                "overview": "overview",
                "tmdb_poster_path": "poster_path",
                "season_number": "season_number",
            },
            refresh_fields=True,
            prune_link=tv_seasons.c.season_id,
        ),
    )

    @property
    def display_title(self):
        """Return the site-wide display form: the TMDB name if known."""

        return self.tmdb_name or self.title

    def __repr__(self):
        return f"<TVSeries '{self.title}'>"


def title_lock_key(
    media_library,
    title,
    year=None,
    feature_type=None,
    plex_title=None,
    season=None,
    episode=None,
    edition=None,
):
    """Return the cross-task lock key of a title as a JSON string.

    Fitzflix must not process 2 versions of one video at the same time.
    The import task builds this key from the parsed filename. The later
    tasks build it from the database rows (File.file_identifier). Both
    go through this one function, so the 2 keys are the same string. A
    movie key is title, year, feature type, Plex title and edition. A TV
    key is series title, season and episode."""

    if media_library == "Movies":
        identity = {
            "title": title,
            "year": year,
            "feature_type": feature_type,
            "plex_title": plex_title,
            "edition": edition,
        }
    elif media_library == "TV Shows":
        identity = {"title": title, "season": season, "episode": episode}
    else:
        raise ValueError(f"unknown media library {media_library!r}")
    return json.dumps(identity)


class File(db.Model):
    """A library media file.

    The row holds the location, the quality and ranking fields, the
    track metadata relations, and the AWS archive state.
    """

    id = db.Column(db.Integer, primary_key=True)
    untouched_basename = db.Column(db.String(255))
    media_library = db.Column(db.String(16), nullable=False, index=True)
    file_path = db.Column(db.String(512), nullable=False, unique=True, index=True)
    dirname = db.Column(db.String(255), nullable=False)
    basename = db.Column(db.String(255), nullable=False)
    plex_title = db.Column(db.String(230), nullable=False, index=True)
    movie_id = db.Column(db.Integer, db.ForeignKey("movie.id"))
    feature_type_id = db.Column(db.Integer, db.ForeignKey("ref_feature_type.id"))
    series_id = db.Column(db.Integer, db.ForeignKey("tv_series.id"))
    season = db.Column(db.Integer)
    episode = db.Column(db.Integer)
    last_episode = db.Column(db.Integer)
    edition = db.Column(db.String(219), index=True)
    quality_id = db.Column(db.Integer, db.ForeignKey("ref_quality.id"))
    fullscreen = db.Column(db.Boolean, nullable=False, index=True, default=False)
    crop = db.Column(db.String(19))
    container = db.Column(db.String(64))
    format = db.Column(db.String(64))
    codec = db.Column(db.String(64))
    hdr_format = db.Column(db.String(255))

    # The Dolby Vision variant parsed from hdr_format: "5", "7", "8.1",
    # "8.4", and so on. The suffix of profile 8 is the cross-compatibility
    # target. This feeds the future P7 to 8.1 conversion targeting.

    dolby_vision_profile = db.Column(db.String(8))

    # The color tags of the video track, from MediaInfo. An SDR file can
    # have BT.2020 colors. hdr_format does not show that (#292). None
    # when the file has no tag.

    color_primaries = db.Column(db.String(64))
    transfer_characteristics = db.Column(db.String(64))

    # Set when this file is a 1080p SDR conversion (#292). It is the
    # quality of the original, for example "Bluray-2160p". The import
    # archived that original untouched in S3. Thus, the archive is NOT
    # a copy of this file. converted_from_format names the color of the
    # original, for example "Dolby Vision Profile 7".

    converted_from_quality = db.Column(db.String(32))
    converted_from_format = db.Column(db.String(255))

    # The second archive of a conversion: its current 1080p library file,
    # in transcoded/ (#299). aws_key keeps the original. A restore of a
    # missing conversion uses this copy first, straight into the library
    # path.

    aws_transcoded_key = db.Column(db.String(255), index=True)
    aws_transcoded_filesize_bytes = db.Column(db.BigInteger)
    aws_transcoded_date_uploaded = db.Column(db.DateTime)
    video_bitrate_kbps = db.Column(db.Integer)
    filesize_bytes = db.Column(db.BigInteger)
    date_added = db.Column(
        db.DateTime, nullable=False, index=True, default=db.func.utc_timestamp()
    )
    date_updated = db.Column(db.DateTime, index=True)
    date_localized = db.Column(db.DateTime, index=True)

    # The time that the subtitle triage marked the tracks of this file as
    # reviewed ("nothing forced here"). It is on the file, not on the
    # track rows, because a rescan deletes and builds those rows again.

    subtitle_triage_reviewed = db.Column(db.DateTime)

    # The time that the runtime triage (#234) accepted the length of this
    # file as known and harmless. Examples are a full-disc rip, or a short
    # recorded into a longer broadcast slot. Then the file no longer
    # appears on the candidates list. A new import resets it. The length
    # of a replacement is new evidence.

    runtime_mismatch_reviewed = db.Column(db.DateTime)

    # The time that the lossy-audio triage (#212) accepted the track
    # layout of this file as it is. The lossless sibling is a commentary
    # or different content. Then the file no longer appears on the
    # worklist. A new import resets it, the same as the other verdicts.

    lossy_audio_reviewed = db.Column(db.DateTime)

    # The archive of the file (#299). The key is in untouched/ (the rip,
    # as it arrived) until the first change of the library file. Then it
    # is in modified/ (the current library file). A conversion keeps its
    # original here and its library file in aws_transcoded_key.

    aws_key = db.Column(db.String(255), index=True)
    aws_filesize_bytes = db.Column(db.BigInteger)
    aws_date_uploaded = db.Column(db.DateTime)
    aws_date_deleted = db.Column(db.DateTime)

    # Set when an archive upload was lost and the S3 object is known to be
    # older than the local file. No other field can tell. The key still
    # exists and the date is from the old upload. Thus, every existing
    # check reads the row as consistent. A successful upload clears it.

    aws_stale = db.Column(db.Boolean, nullable=False, server_default="0", default=False)
    subtrack = db.relationship(
        "FileSubtitleTrack", backref="file", lazy="select", cascade="all,delete"
    )
    audiotrack = db.relationship(
        "FileAudioTrack", backref="file", lazy="select", cascade="all,delete"
    )

    custom_poster = db.Column(db.String(64))

    # These keys are not mapped columns. The import pipeline includes them
    # in the file_details dicts that it makes File objects from. Fitzflix
    # keeps them as plain attributes because find_better_files() reads
    # them when it compares an incoming file with the existing library.

    IMPORT_EXTRA_KEYS = {
        "title",
        "year",
        "quality_title",
        "extension",
        "feature_type_name",
        # The TMDB id that the Plex id tag of a filename resolved to
        # (#155). finalize_localization reads it to route the metadata
        # fetch.
        "tmdb_id",
        # An admin chose "Import" for a held conversion (#292). The
        # conversion then replaces the real rip of the title.
        "replace_existing",
    }

    def __repr__(self):
        return f"<File '{self.plex_title}'>"

    @property
    def needs_sdr_conversion(self):
        """Return True if this file has HDR or BT.2020 colors (#292)."""

        from app.sdr_conversion import needs_conversion

        return needs_conversion(
            self.hdr_format, self.color_primaries, self.transfer_characteristics
        )

    @property
    def restore_key(self):
        """Return the S3 key that restores the library file of this record (#299).

        A conversion with an uploaded transcoded/ copy restores that copy.
        Each other record restores its archive: untouched/ (the rip) or
        modified/ (the library file after a change)."""

        if (
            self.converted_from_quality
            and self.aws_transcoded_key
            and self.aws_transcoded_date_uploaded
        ):
            return self.aws_transcoded_key
        return self.aws_key

    @property
    def archive_is_library_copy(self):
        """Return True if the S3 archive of this file can follow its library copy.

        Fitzflix replaces the archive with the library file after a remux
        or an Atmos twin. The archive of a conversion is the original
        (#292). The library file must never replace it."""

        return not self.converted_from_quality

    def __init__(self, **kwargs):
        # Route the mapped columns and relationships through the real
        # SQLAlchemy constructor. Thus, instrumentation and validation
        # apply. Allow only the known import-pipeline extras as plain
        # attributes. Reject all other keys. Thus, a column name with a
        # typo fails fast and does not disappear.

        mapped = set(db.inspect(File).attrs.keys())
        unexpected = set(kwargs) - mapped - self.IMPORT_EXTRA_KEYS
        if unexpected:
            raise TypeError(
                f"File() got unexpected keyword argument(s): {sorted(unexpected)}"
            )

        super().__init__(**{k: v for k, v in kwargs.items() if k in mapped})

        for key in set(kwargs) & self.IMPORT_EXTRA_KEYS:
            setattr(self, key, kwargs[key])

    def delete_local_file(self, delete_directory_tree=False):
        """Delete the library copy.

        A missing file is not an error. With delete_directory_tree, this
        method also deletes the folders that became empty.
        """

        file_to_delete = os.path.join(current_app.config["LIBRARY_DIR"], self.file_path)
        try:
            os.remove(file_to_delete)

        except FileNotFoundError:
            pass

        else:
            current_app.logger.info(f"Deleted local file '{file_to_delete}'")

        # The triage snapshots are useful only while the local copy exists.

        from app.triage import remove_triage_snapshots

        remove_triage_snapshots(self.id)

        # As an option, delete the directory tree, even if the library file
        # itself was already gone. Thus, a record delete also removes its
        # empty folder.

        if delete_directory_tree:
            try:
                os.removedirs(os.path.dirname(file_to_delete))

            except OSError:
                pass

            else:
                current_app.logger.info(
                    f"Deleted the directory tree '{os.path.dirname(file_to_delete)}'"
                )

        return self

    def file_identifier(self):
        """Return the JSON identity that is the cross-task lock key of this file.

        The key is title/year/feature for movies and series/season/episode
        for TV.
        """

        if self.media_library == "Movies":
            file = (
                File.query.join(Movie, (Movie.id == File.movie_id))
                .outerjoin(RefFeatureType, (RefFeatureType.id == File.feature_type_id))
                .filter(File.id == self.id)
                .first()
            )
            return title_lock_key(
                "Movies",
                file.movie.title,
                year=file.movie.year,
                feature_type=(
                    file.feature_type.feature_type if file.feature_type else None
                ),
                plex_title=file.plex_title,
                edition=file.edition,
            )

        file = (
            File.query.join(TVSeries, (TVSeries.id == File.series_id))
            .filter(File.id == self.id)
            .first()
        )
        return title_lock_key(
            "TV Shows",
            file.tv_series.title,
            season=file.season,
            episode=file.episode,
        )

    def find_better_files(self):
        # A message to the future Glenn (written by Glenn, 2020-08-02):
        #
        # This place is a message, and part of a system of messages. Give
        # it your attention.
        #
        # Glenn wrote much of the logic of this method without sleep. Thus,
        # it is not certain that it works 100% correctly. But be careful
        # with improvements. It is easy to break this method or to cause
        # unintended effects. This is especially true for special features,
        # different versions (for example, the Director's Cut), and
        # multi-episode TV show files.
        #
        # The method is also difficult because the logic is the reverse of
        # what you expect. It looks for the files that are better than the
        # file that Fitzflix imports. Thus, the import continues only if
        # the method finds NO matches.
        #
        # The intended rules are:
        #
        # Movies:
        #
        # - If the existing file is a full screen version, always replace
        #   it with a non-full screen version, even if the quality is lower.
        #
        # - If the existing file is a non-full screen version, and the new
        #   file is full screen, always reject the new full screen file.
        #
        # - If the existing and the new files are both non-full screen
        #   versions, compare the quality. Replace the existing file only
        #   if the new file has the same or better quality.
        #
        # TV Shows:
        #
        # - If the existing file is a full screen version, always replace
        #   it with a non-full screen version. Do this even if the quality
        #   is lower, or if a multi-episode file loses episodes.
        #
        # - If the existing file is a non-full screen version, and the new
        #   file is full screen, always reject the new full screen file.
        #
        # - If the existing and the new files are both non-full screen
        #   versions, compare the quality and the episode count. Replace
        #   the existing file only if the new file has the same or better
        #   quality. The new file must also contain the same number of
        #   episodes or more.
        #
        # Never import full screen versions of movies or TV shows. Then you
        # should be safe. You do not want full screen versions anyway.
        #
        # In short: this place is not a place of honor.

        """Return the same-title files that outrank this file."""

        better_files = []

        source_quality = RefQuality.query.filter_by(
            quality_title=self.quality_title
        ).first()

        current_app.logger.debug(f"Import vars: {vars(self)}")

        if self.media_library == "Movies":
            # If the new file is a full screen version, these better files
            # prevent the import of this file:
            # - the exact same movie, also full screen, in a better quality
            # - the exact same movie, NOT full screen, in the same or better
            #   quality

            if self.fullscreen == True:
                better_files = (
                    File.query.join(Movie, (Movie.id == File.movie_id))
                    .join(RefQuality, (RefQuality.id == File.quality_id))
                    .filter(
                        File.media_library == self.media_library,
                        File.dirname == self.dirname,
                        Movie.title == self.title,
                        Movie.year == self.year,
                        db.or_(
                            db.and_(
                                File.plex_title == self.plex_title,
                                File.edition == self.edition,
                                File.fullscreen == True,
                                RefQuality.preference > source_quality.preference,
                            ).self_group(),
                            # The parser gives a full screen file the plex
                            # title and the edition of its widescreen copy.
                            # Only the flag differs. Any widescreen copy
                            # blocks it.
                            db.and_(
                                File.plex_title == self.plex_title,
                                File.edition == self.edition,
                                db.or_(
                                    File.fullscreen == False,
                                    File.fullscreen.is_(None),
                                ).self_group(),
                            ).self_group(),
                        ),
                    )
                    .all()
                )

            # Otherwise, if the file is not full screen, these better files
            # prevent the import of this file:
            # - the exact same movie, not full screen, in a better quality

            else:
                better_files = (
                    File.query.join(Movie, (Movie.id == File.movie_id))
                    .join(RefQuality, (RefQuality.id == File.quality_id))
                    .filter(
                        File.media_library == self.media_library,
                        File.dirname == self.dirname,
                        Movie.title == self.title,
                        Movie.year == self.year,
                        File.plex_title == self.plex_title,
                        File.edition == self.edition,
                        File.fullscreen == False,
                        RefQuality.preference > source_quality.preference,
                    )
                    .all()
                )

        elif self.media_library == "TV Shows":
            # The identity of a TV episode is series + season + episode
            # span. It is NEVER the edition. For a TV file, the edition
            # holds the episode-title segment of the filename. Two releases
            # of the same episode can give it different titles (the Seeds
            # of Doom case from Glenn: a Blu-ray special did not replace its
            # DVD predecessor because the discs gave the extra different
            # names).
            #
            # If the new file is a full screen version, these existing
            # files prevent the import of this file:
            # - same TV episode range, also full screen, in the same quality
            # - wider TV episode range, also full screen, in the same or
            #   better quality
            # - same TV episode range, NOT full screen, in the same quality
            # - wider TV episode range, NOT full screen, in the same or
            #   better quality

            if self.fullscreen == True:
                better_files = (
                    File.query.join(TVSeries, (TVSeries.id == File.series_id))
                    .join(RefQuality, (RefQuality.id == File.quality_id))
                    .filter(
                        File.media_library == self.media_library,
                        File.dirname == self.dirname,
                        TVSeries.title == self.title,
                        File.season == self.season,
                        File.episode == self.episode,
                        db.or_(
                            db.and_(
                                File.last_episode == self.last_episode,
                                File.fullscreen == True,
                                RefQuality.preference == source_quality.preference,
                            ).self_group(),
                            db.and_(
                                File.last_episode > self.last_episode,
                                File.fullscreen == True,
                                RefQuality.preference >= source_quality.preference,
                            ).self_group(),
                            db.and_(
                                File.last_episode == self.last_episode,
                                File.fullscreen == False,
                                RefQuality.preference == source_quality.preference,
                            ).self_group(),
                            db.and_(
                                File.last_episode > self.last_episode,
                                File.fullscreen == False,
                                RefQuality.preference >= source_quality.preference,
                            ).self_group(),
                        ).self_group(),
                    )
                    .all()
                )

            # Otherwise, if the file is not full screen, these better files
            # prevent the import of this file:
            # - same TV show episode range, not full screen, in a better
            #   quality
            # - wider TV show episode range, not full screen, in the same
            #   quality

            else:
                better_files = (
                    File.query.join(TVSeries, (TVSeries.id == File.series_id))
                    .join(RefQuality, (RefQuality.id == File.quality_id))
                    .filter(
                        File.media_library == self.media_library,
                        File.dirname == self.dirname,
                        TVSeries.title == self.title,
                        File.season == self.season,
                        File.episode == self.episode,
                        db.or_(
                            db.and_(
                                File.last_episode == self.last_episode,
                                RefQuality.preference > source_quality.preference,
                            ).self_group(),
                            db.and_(
                                File.last_episode > self.last_episode,
                                RefQuality.preference == source_quality.preference,
                            ).self_group(),
                        ).self_group(),
                        File.fullscreen == False,
                    )
                    .all()
                )

        # A conversion to 1080p SDR (#292) has 2 more rules. An admin who
        # chose "Import" for a held conversion replaces the existing file,
        # whatever its quality. A file with HDR or BT.2020 colors never
        # blocks a conversion. The conversion replaces it.

        if self.converted_from_quality:
            if getattr(self, "replace_existing", False):
                return []
            better_files = [f for f in better_files if not f.needs_sdr_conversion]

        return better_files

    def same_identity_files(self):
        """Return the other files of this title, edition, or episode span, in any quality.

        A TV file includes the files with a narrower episode span. These
        are the files that find_worse_files can remove, before its
        quality rule."""

        query = (
            File.query.join(RefQuality, (RefQuality.id == File.quality_id))
            .options(joinedload(File.quality, innerjoin=True))
            .filter(
                db.or_(File.fullscreen == self.fullscreen, File.fullscreen == True),
                File.id != self.id,
            )
        )
        if self.media_library == "Movies":
            query = query.filter(
                File.movie_id == self.movie_id,
                File.feature_type_id == self.feature_type_id,
                File.plex_title == self.plex_title,
                File.edition == self.edition,
            )
        elif self.media_library == "TV Shows":
            query = query.filter(
                File.series_id == self.series_id,
                File.season == self.season,
                File.episode == self.episode,
                File.last_episode <= self.last_episode,
            )
        else:
            return []
        return query.all()

    def find_worse_files(self):
        """Return the same-title files that this file outranks.

        These are the candidates for removal after an upgrade. A
        conversion to 1080p SDR (#292) also outranks the files with HDR
        or BT.2020 colors, in any quality. After an admin chose "Import"
        for a held conversion, it outranks every file of its title.
        """

        worse_files = self._worse_by_quality()
        if self.converted_from_quality:
            replace_all = getattr(self, "replace_existing", False)

            # A real rip of the same quality outranks a conversion. Only
            # the "Import" choice of an admin lets the conversion replace it.

            if not replace_all:
                worse_files = [
                    f
                    for f in worse_files
                    if f.converted_from_quality
                    or f.needs_sdr_conversion
                    or f.quality.preference < self.quality.preference
                    or (
                        self.media_library == "TV Shows"
                        and f.last_episode < self.last_episode
                    )
                ]
            worse_files += [
                f
                for f in self.same_identity_files()
                if f not in worse_files and (replace_all or f.needs_sdr_conversion)
            ]
        return worse_files

    def _worse_by_quality(self):
        """Return the same-title files of the same or a lower quality."""

        worse_files = []

        if self.media_library == "Movies":
            worse_files = (
                File.query.join(RefQuality, (RefQuality.id == File.quality_id))
                .options(joinedload(File.quality, innerjoin=True))
                .filter(
                    File.movie_id == self.movie_id,
                    File.feature_type_id == self.feature_type_id,
                    File.plex_title == self.plex_title,
                    File.edition == self.edition,
                    RefQuality.preference <= self.quality.preference,
                    db.or_(File.fullscreen == self.fullscreen, File.fullscreen == True),
                    File.id != self.id,
                )
                .all()
            )

        elif self.media_library == "TV Shows":
            # The edition is absent on purpose. For a TV file, it holds the
            # episode-title segment of the filename. Two releases of the
            # same episode can give it different titles. The episode span
            # is the identity. Thus, an upgrade with a new title still
            # removes its predecessor. A wider span replaces a narrower
            # file only at the same or a better quality, the rule of
            # find_better_files and tv_file_rank. A DVD "S13E01-E06" must
            # not delete a Bluray "S13E01".

            worse_files = (
                File.query.join(RefQuality, (RefQuality.id == File.quality_id))
                .options(joinedload(File.quality, innerjoin=True))
                .filter(
                    File.series_id == self.series_id,
                    File.season == self.season,
                    File.episode == self.episode,
                    File.last_episode <= self.last_episode,
                    RefQuality.preference <= self.quality.preference,
                    db.or_(File.fullscreen == self.fullscreen, File.fullscreen == True),
                    File.id != self.id,
                )
                .all()
            )

        return worse_files


class ArchivedOriginal(db.Model):
    """An original in S3 with no library file of its own (#292).

    Fitzflix makes one in 2 cases. An admin chose "Archive only" for a
    held conversion. Or a real rip replaced a conversion, and the
    original of the conversion stays in S3. The S3 sync counts these
    keys as claimed. Thus, the sync does not prune them. A restore of
    one imports the original again, through the conversion flow."""

    id = db.Column(db.Integer, primary_key=True)
    media_library = db.Column(db.String(32), nullable=False)
    movie_id = db.Column(
        db.Integer, db.ForeignKey("movie.id", ondelete="SET NULL"), index=True
    )
    series_id = db.Column(
        db.Integer, db.ForeignKey("tv_series.id", ondelete="SET NULL"), index=True
    )
    season = db.Column(db.Integer)
    episode = db.Column(db.Integer)
    plex_title = db.Column(db.String(255))
    edition = db.Column(db.String(219))
    quality_title = db.Column(db.String(32), nullable=False)
    hdr_format = db.Column(db.String(255))
    untouched_basename = db.Column(db.String(255), nullable=False)
    aws_key = db.Column(db.String(255), nullable=False, unique=True)
    aws_filesize_bytes = db.Column(db.BigInteger)
    aws_date_uploaded = db.Column(db.DateTime)
    reason = db.Column(db.String(64), nullable=False)
    date_added = db.Column(db.DateTime, nullable=False, default=db.func.utc_timestamp())
    movie = db.relationship("Movie")
    tv_series = db.relationship("TVSeries")

    def __repr__(self):
        return f"<ArchivedOriginal '{self.aws_key}'>"


class HeldImport(db.Model):
    """A conversion candidate that waits for an admin decision (#292).

    The library already has a real rip of the title. The original file
    waits in HELD_DIR. The admin chooses "Import", "Archive only", or
    "Discard" on the Held imports page. file_details is the JSON of the
    parsed filename, with the conversion fields."""

    id = db.Column(db.Integer, primary_key=True)
    held_path = db.Column(db.String(512), nullable=False, unique=True)
    untouched_basename = db.Column(db.String(255), nullable=False)
    file_details = db.Column(db.Text, nullable=False)
    source_format = db.Column(db.String(255))
    filesize_bytes = db.Column(db.BigInteger)
    existing_file_id = db.Column(
        db.Integer, db.ForeignKey("file.id", ondelete="SET NULL")
    )

    # None while the item waits. "import" or "archive" after the admin
    # chose. The import reads it when the file comes back.

    decision = db.Column(db.String(16))
    date_held = db.Column(db.DateTime, nullable=False, default=db.func.utc_timestamp())
    date_decided = db.Column(db.DateTime)
    existing_file = db.relationship("File")

    def details(self):
        """Return the parsed file details as a dict."""

        return json.loads(self.file_details)

    def __repr__(self):
        return f"<HeldImport '{self.untouched_basename}'>"


class FileAudioTrack(db.Model):
    """One audio track from the MediaInfo scan of a file."""

    id = db.Column(db.Integer, primary_key=True)
    file_id = db.Column(db.Integer, db.ForeignKey("file.id"))
    track = db.Column(db.Integer, nullable=False)
    language = db.Column(db.String(3), nullable=False)
    format = db.Column(db.String(64))
    channels = db.Column(db.String(64))
    default = db.Column(db.Boolean)
    streamorder = db.Column(db.Integer)
    codec = db.Column(db.String(64))
    bitrate = db.Column(db.Integer)
    bitrate_kbps = db.Column(db.Integer)
    bit_depth = db.Column(db.Integer)
    sampling_rate = db.Column(db.Integer)
    sampling_rate_khz = db.Column(db.Integer)
    language_name = db.Column(db.String(64), nullable=False)
    compression_mode = db.Column(db.String(64))

    __table_args__ = (db.UniqueConstraint("file_id", "track"),)

    def __repr__(self):
        return f"<FileAudioTrack '{self.file_id}:{self.track}:{self.language}'>"


class FileSubtitleTrack(db.Model):
    """One subtitle track from the MediaInfo scan of a file."""

    id = db.Column(db.Integer, primary_key=True)
    file_id = db.Column(db.Integer, db.ForeignKey("file.id"))
    track = db.Column(db.Integer, nullable=False)
    language = db.Column(db.String(3), nullable=False)
    format = db.Column(db.String(64))
    elements = db.Column(db.Integer, nullable=False)
    default = db.Column(db.Boolean)
    forced = db.Column(db.Boolean)
    streamorder = db.Column(db.Integer)
    language_name = db.Column(db.String(64), nullable=False)

    __table_args__ = (db.UniqueConstraint("file_id", "track"),)

    def __repr__(self):
        return f"<FileSubtitleTrack '{self.file_id}:{self.track}:{self.language}:{self.forced}'>"


class RefFeatureType(db.Model):
    """The lookup table of special-feature types (Trailers, Featurettes, ...)."""

    id = db.Column(db.Integer, primary_key=True)
    feature_type = db.Column(db.String(32), nullable=False, unique=True)
    files = db.relationship(
        "File", backref="feature_type", lazy="dynamic", cascade="all,delete"
    )

    def __repr__(self):
        return f"<RefFeatureType '{self.feature_type}'>"


class RefQuality(db.Model):
    """The lookup table of quality tiers, ordered by preference.

    It includes the virtual bottom tier 'Not in library'.
    """

    id = db.Column(db.Integer, primary_key=True)
    quality_title = db.Column(db.String(32), nullable=False, unique=True)
    preference = db.Column(db.Integer, nullable=False)
    physical_media = db.Column(db.Boolean, nullable=False, default=False)
    date_updated = db.Column(db.DateTime)
    files = db.relationship(
        "File", backref="quality", lazy="dynamic", cascade="all,delete"
    )
    movies = db.relationship(
        "Movie", backref="format", lazy="dynamic", cascade="all,delete"
    )

    def __repr__(self):
        return f"<RefQuality '{self.quality_title}'>"


class RefTMDBCertification(db.Model, TMDBMixin):
    """The lookup table of TMDB certifications per country (G, PG-13, ...)."""

    id = db.Column(db.Integer, primary_key=True)
    country = db.Column(db.String(8))
    certification = db.Column(db.String(32))
    meaning = db.Column(db.Text)
    order = db.Column(db.Integer)

    __table_args__ = (db.UniqueConstraint("country", "certification"),)

    def __repr__(self):
        return f"<RefCertification '{self.country} - {self.certification}'>"


class TMDBMovieCollection(db.Model, TMDBMixin):
    """A TMDB collection that a movie belongs to."""

    id = db.Column(db.Integer, primary_key=True)
    tmdb_backdrop_path = db.Column(db.String(64))
    name = db.Column(db.String(128))
    tmdb_poster_path = db.Column(db.String(64))

    def __repr__(self):
        return f"<TMDBMovieCollection '{self.name}'>"


class TMDBCredit(db.Model, TMDBMixin):
    """A person from the TMDB credits, shared by the cast and crew join rows."""

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128))
    gender = db.Column(db.Integer)
    tmdb_profile_path = db.Column(db.String(64))

    # The join rows point at the credit (MovieCast.starring and so on).
    # The credit has no relationship back to them. Thus, a delete of a
    # credit does not delete the cast and crew rows. The foreign keys
    # refuse it. Nothing in the app deletes a credit.

    def __repr__(self):
        return f"<TMDBCredit '{self.name}'>"


class MovieCast(db.Model):
    """A join row for the acting role of a credit on a movie."""

    id = db.Column(db.Integer, primary_key=True)
    movie_id = db.Column(db.Integer, db.ForeignKey("movie.id"))
    credit_id = db.Column(db.Integer, db.ForeignKey("tmdb_credit.id"))
    starring = db.relationship("TMDBCredit")
    character = db.Column(db.String(512))
    billing_order = db.Column(db.Integer)

    __table_args__ = (db.UniqueConstraint("movie_id", "credit_id", "character"),)

    def __repr__(self):
        return f"<MovieCast '{self.movie_id}:{self.credit_id}:{self.character}'>"


class MovieCrew(db.Model):
    """A join row for the crew role of a credit on a movie."""

    id = db.Column(db.Integer, primary_key=True)
    movie_id = db.Column(db.Integer, db.ForeignKey("movie.id"))
    credit_id = db.Column(db.Integer, db.ForeignKey("tmdb_credit.id"))
    crewed = db.relationship("TMDBCredit")
    department = db.Column(db.String(128))
    job = db.Column(db.String(128))

    __table_args__ = (
        db.UniqueConstraint("movie_id", "credit_id", "department", "job"),
    )

    def __repr__(self):
        return f"<MovieCrew '{self.movie_id}:{self.credit_id}:{self.job}'>"


class MovieAward(db.Model):
    """An award win or nomination for a film, read from Wikidata.

    The rows are the current truth. The weekly refresh replaces all the
    rows of a film. Thus, an absent row means that Wikidata lists
    nothing (or the film has no Wikidata item). The coverage is good for
    major ceremonies and incomplete for niche festivals. Thus, a surface
    must never show the data as complete.
    """

    id = db.Column(db.Integer, primary_key=True)
    movie_id = db.Column(
        db.Integer, db.ForeignKey("movie.id"), index=True, nullable=False
    )
    award_id = db.Column(db.String(32))
    award_name = db.Column(db.String(512))
    win = db.Column(db.Boolean, nullable=False, default=False)
    year = db.Column(db.Integer)

    __table_args__ = (db.UniqueConstraint("movie_id", "award_id", "win", "year"),)

    def __repr__(self):
        return (
            f"<MovieAward '{self.movie_id}:{self.award_name}:"
            f"{'win' if self.win else 'nomination'}'>"
        )


class MovieCopref(db.Model):
    """The co-preference similarity between 2 films, from MovieLens.

    This is the adjusted-cosine similarity over the 32 million ratings
    of ML-32M, with co-rater shrinkage. It means "people who loved A
    loved B more than expected". That is the taste signal that content
    features cannot see. The key is the TMDB ids (they survive record
    merges). Only positive similarities are stored. Both directions are
    stored. Thus, an anchor-side lookup is one indexed query. Fitzflix
    builds the table again only when it adopts a new MovieLens snapshot,
    through `flask recs copref`. That command needs numpy and scipy
    installed ad hoc. They are not runtime dependencies on purpose.
    """

    id = db.Column(db.Integer, primary_key=True)
    tmdb_id_a = db.Column(db.Integer, index=True, nullable=False)
    tmdb_id_b = db.Column(db.Integer, nullable=False)
    similarity = db.Column(db.Float, nullable=False)

    __table_args__ = (db.UniqueConstraint("tmdb_id_a", "tmdb_id_b"),)

    def __repr__(self):
        return f"<MovieCopref '{self.tmdb_id_a}~{self.tmdb_id_b}:{self.similarity}'>"


class CatalogExclusion(db.Model):
    """A TMDB id that the catalog loaders must never create again.

    The Criterion spine set on Wikidata sometimes lists a film that does
    not really exist. An example is an unfinished work with a stale TMDB
    id (Ivan the Terrible Part III by Eisenstein was the first found).
    A delete of the incorrect Movie record is not sufficient. The next
    full refresh would create it again. `flask catalog exclude` deletes
    the record AND stores its id here. After that, the catalog-record
    creation pass and the Criterion catalog page skip the excluded ids.
    """

    id = db.Column(db.Integer, primary_key=True)
    tmdb_id = db.Column(db.Integer, unique=True, nullable=False)
    title = db.Column(db.String(256))
    date_added = db.Column(db.DateTime, default=datetime.now)

    def __repr__(self):
        return f"<CatalogExclusion '{self.tmdb_id}:{self.title}'>"


class AppSetting(db.Model):
    """A value that an admin set on the Settings page, one row per setting.

    The value is text. app.settings parses it with the type of the
    setting. The table is in MySQL, not in Redis. Thus, the values are in
    the nightly backup, and a Redis flush does not remove them. A setting
    that .env sets ignores its row, because .env wins."""

    name = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.Text, nullable=False)
    updated_by = db.Column(db.String(120))
    date_updated = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    def __repr__(self):
        return f"<AppSetting '{self.name}'>"


class UserMovieStatus(db.Model):
    """The standing flags of a user on films, one row per (user, film, kind).

    The kinds are "unseen" and "not_interested". "unseen" is the "have
    not seen it" answer of the rating drive. The film is permanently out
    of the drive. Before, this was a Redis-only set. It was the one
    user-authored data that a cache flush could lose. "not_interested"
    removes the film from every recommendation surface for this user.
    It is mainly for unowned films. The zero stars of the rating ladder
    already cover the owned films with a real diary row. Not-interested
    feeds the taste profile as a mild negative. A delete of the row
    clears the flag.
    """

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("user.id"), index=True, nullable=False
    )
    movie_id = db.Column(
        db.Integer, db.ForeignKey("movie.id"), index=True, nullable=False
    )
    kind = db.Column(db.String(32), nullable=False)
    date_added = db.Column(db.DateTime, default=datetime.now)

    __table_args__ = (db.UniqueConstraint("user_id", "movie_id", "kind"),)

    def __repr__(self):
        return f"<UserMovieStatus '{self.user_id}:{self.movie_id}:{self.kind}'>"


class UserFrameScore(db.Model):
    """The Name That Frame standings, one row per (user, difficulty).

    The row holds the running streak and the personal best. Fitzflix
    stores them here. Thus, a restart, a different device, or a new
    session cannot erase a high score. The original session-cookie
    streaks could lose it."""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("user.id"), index=True, nullable=False
    )
    difficulty = db.Column(db.String(16), nullable=False)
    current_streak = db.Column(db.Integer, nullable=False, default=0)
    best_streak = db.Column(db.Integer, nullable=False, default=0)
    date_best = db.Column(db.DateTime)
    # The running total of Extra Difficult (#202): 3, 2, or 1 points by
    # how early in the zoom-out the guess arrived.
    points = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    # The win rate (requested by Glenn, 2026-08-27). Every dealt frame
    # counts as seen. That includes skipped and abandoned rounds. Only a
    # correct guess counts as won.
    rounds_seen = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    rounds_won = db.Column(db.Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (db.UniqueConstraint("user_id", "difficulty"),)

    def __repr__(self):
        return f"<UserFrameScore '{self.user_id}:{self.difficulty}:{self.best_streak}'>"


class TVCast(db.Model):
    """A join row for the acting role of a credit on a TV series.

    The data comes from the TMDB aggregate credits. There is one row per
    distinct character, with the series-wide billing order and the
    number of episodes of the role."""

    id = db.Column(db.Integer, primary_key=True)
    tv_id = db.Column(db.Integer, db.ForeignKey("tv_series.id"))
    credit_id = db.Column(db.Integer, db.ForeignKey("tmdb_credit.id"))
    starring = db.relationship("TMDBCredit")
    character = db.Column(db.String(512))
    billing_order = db.Column(db.Integer)
    episode_count = db.Column(db.Integer)

    __table_args__ = (db.UniqueConstraint("tv_id", "credit_id", "character"),)

    def __repr__(self):
        return f"<TVCast '{self.tv_id}:{self.credit_id}:{self.character}'>"


class TVCrew(db.Model):
    """A join row for the crew role of a credit on a TV series.

    The data comes from the TMDB aggregate credits. There is one row per
    distinct job."""

    id = db.Column(db.Integer, primary_key=True)
    tv_id = db.Column(db.Integer, db.ForeignKey("tv_series.id"))
    credit_id = db.Column(db.Integer, db.ForeignKey("tmdb_credit.id"))
    crewed = db.relationship("TMDBCredit")
    department = db.Column(db.String(128))
    job = db.Column(db.String(128))
    episode_count = db.Column(db.Integer)

    __table_args__ = (db.UniqueConstraint("tv_id", "credit_id", "department", "job"),)

    def __repr__(self):
        return f"<TVCrew '{self.tv_id}:{self.credit_id}:{self.job}'>"


class TMDBGenre(db.Model, TMDBMixin):
    """A TMDB genre associated with a library title."""

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(32))

    def __repr__(self):
        return f"<TMDBGenre '{self.name}'>"


class TMDBKeyword(db.Model, TMDBMixin):
    """A TMDB keyword associated with a library title."""

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128))

    def __repr__(self):
        return f"<TMDBKeyword '{self.name}'>"


class TMDBNetwork(db.Model, TMDBMixin):
    """A TMDB network associated with a series."""

    id = db.Column(db.Integer, primary_key=True)
    tmdb_logo_path = db.Column(db.String(64))
    name = db.Column(db.String(128))
    origin_country = db.Column(db.String(16))

    def __repr__(self):
        return f"<TMDBNetwork '{self.name}'>"


class TMDBProductionCompany(db.Model, TMDBMixin):
    """A TMDB production company associated with a library title."""

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128))
    country = db.Column(db.String(16))
    tmdb_logo_path = db.Column(db.String(64))

    def __repr__(self):
        return f"<TMDBProductionCompany '{self.name}'>"


class TMDBProductionCountry(db.Model, TMDBMixin):
    """A TMDB production country associated with a library title."""

    id = db.Column(db.String(2), primary_key=True)
    name = db.Column(db.String(128))

    def __repr__(self):
        return f"<TMDBProductionCountry '{self.name}'>"


class TMDBSpokenLanguage(db.Model, TMDBMixin):
    """A TMDB spoken language associated with a library title."""

    id = db.Column(db.String(2), primary_key=True)
    name = db.Column(db.String(128))

    def __repr__(self):
        return f"<TMDBSpokenLanguage '{self.name}'>"


class TMDBSeason(db.Model, TMDBMixin):
    """A TMDB season summary associated with a series."""

    id = db.Column(db.Integer, primary_key=True)
    air_date = db.Column(db.DateTime)
    episode_count = db.Column(db.Integer)
    name = db.Column(db.String(128))
    overview = db.Column(db.Text)
    tmdb_poster_path = db.Column(db.String(64))
    season_number = db.Column(db.Integer)

    def __repr__(self):
        return f"<TMDBSeason '{self.id}'>"


@login.user_loader
def load_user(id):
    """Load a user for flask-login.

    The session id holds the password fingerprint. A session from before
    a password change does not load. A session id with no fingerprint
    (from before the fingerprint) does not load either."""

    user_id, _, fingerprint = str(id).partition(":")
    try:
        user = db.session.get(User, int(user_id))
    except ValueError:
        return None
    if user is None or not hmac.compare_digest(
        fingerprint, user.password_fingerprint()
    ):
        return None
    return user


dvr_channel_movies = db.Table(
    "dvr_channel_movies",
    db.Column("channel_id", db.Integer, db.ForeignKey("dvr_channel.id")),
    db.Column("movie_id", db.Integer, db.ForeignKey("movie.id")),
)


dvr_channel_series = db.Table(
    "dvr_channel_series",
    db.Column("channel_id", db.Integer, db.ForeignKey("dvr_channel.id")),
    db.Column("series_id", db.Integer, db.ForeignKey("tv_series.id")),
)


class DVRChannel(db.Model):
    """A virtual DVR channel that an admin defines (#182).

    The members of a channel are its explicit picks (the movie and
    series relationships) plus all titles that match its rule columns.
    Genres and keywords match each library when the related include
    flag is set. network_country applies to series. criterion and
    leaving limit the rule-matched film pool to the films that stream
    on the Criterion Channel, or that leave it. title_pins pull titles
    in past every other filter. The comma-separated rule columns follow
    the free-text convention, not JSON.

    The slug is frozen at creation. It is the tvg-id and the stream URL
    of the channel. Plex maps by the slug. Thus, a rename of a channel
    must never move its stream.
    """

    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.Integer, nullable=False, unique=True)
    name = db.Column(db.String(64), nullable=False, unique=True)
    slug = db.Column(db.String(64), nullable=False, unique=True)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    include_movies = db.Column(db.Boolean, nullable=False, default=False)
    include_tv = db.Column(db.Boolean, nullable=False, default=False)
    genres = db.Column(db.Text)
    keywords = db.Column(db.Text)
    network_country = db.Column(db.String(16))
    title_pins = db.Column(db.Text)
    criterion_only = db.Column(db.Boolean, nullable=False, default=False)
    leaving_only = db.Column(db.Boolean, nullable=False, default=False)
    date_created = db.Column(
        db.DateTime, nullable=False, default=db.func.utc_timestamp()
    )

    movies = db.relationship(
        "Movie",
        secondary=dvr_channel_movies,
        backref=db.backref("dvr_channels", lazy="dynamic"),
        lazy="dynamic",
    )
    series = db.relationship(
        "TVSeries",
        secondary=dvr_channel_series,
        backref=db.backref("dvr_channels", lazy="dynamic"),
        lazy="dynamic",
    )

    def rule_list(self, column):
        """Return one comma-separated rule column as a clean list.

        The terms are lowercase."""

        return [
            term.strip().lower()
            for term in (getattr(self, column) or "").split(",")
            if term.strip()
        ]

    def __repr__(self):
        return f"<DVRChannel {self.number} '{self.name}'>"


def movie_file_rank():
    """Rank each movie file in its title/feature/edition group by quality.

    At the same quality, a real rip ranks above a 1080p SDR conversion
    (#292). A tie goes to the lowest file id. Thus, the winner is always
    the same. The ranked_files reporting view uses the same rule (#273)."""

    return (
        db.func.row_number()
        .over(
            partition_by=(
                Movie.id,
                File.feature_type_id,
                File.plex_title,
                File.edition,
            ),
            order_by=(
                File.fullscreen.asc(),
                RefQuality.preference.desc(),
                File.converted_from_quality.isnot(None).asc(),
                File.id.asc(),
            ),
        )
        .label("rank")
    )


def tv_file_rank():
    """Rank each episode file in its series/season/episode group.

    The edition is not part of the group. That is intentional. For a TV
    file, it only carries the optional episode title. Thus, 2 copies of
    the same episode compete, with or without a title in their
    filenames. At the same quality and span, a real rip ranks above a
    1080p SDR conversion (#292). A tie goes to the lowest file id. The
    ranked_files reporting view uses the same rule (#273).
    """

    return (
        db.func.row_number()
        .over(
            partition_by=(TVSeries.id, File.season, File.episode),
            order_by=(
                File.fullscreen.asc(),
                RefQuality.preference.desc(),
                File.last_episode.desc(),
                File.converted_from_quality.isnot(None).asc(),
                File.id.asc(),
            ),
        )
        .label("rank")
    )


def ranked_movie_files():
    """Return a subquery of (id, rank) with the movie_file_rank of each movie file.

    rank 1 is the best copy of its title, feature, and edition group."""

    return (
        db.session.query(File.id, movie_file_rank())
        .join(Movie, (Movie.id == File.movie_id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .subquery()
    )


def ranked_tv_files():
    """Return a subquery of (id, rank) with the tv_file_rank of each episode file.

    rank 1 is the best copy of its series, season, and episode."""

    return (
        db.session.query(File.id, tv_file_rank())
        .join(TVSeries, (TVSeries.id == File.series_id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .subquery()
    )


def title_sort(model, tmdb=True):
    """Return the sort expression of the title of a film or a series.

    A leading "The", "A", or "An" does not count. With tmdb, the TMDB
    title (tmdb_title of a Movie, tmdb_name of a TVSeries) is used when
    it is set. Without tmdb, only the local title is used."""

    title = model.title
    if tmdb:
        tmdb_title = model.tmdb_title if model is Movie else model.tmdb_name
        title = db.case((tmdb_title != None, tmdb_title), else_=model.title)
    return db.func.regexp_replace(title, "^(The|A|An) ", "")
