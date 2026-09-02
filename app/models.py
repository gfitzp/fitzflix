import json
import os

from datetime import datetime, timezone
from time import sleep, time

import jwt
import requests

from rq.registry import ScheduledJobRegistry, StartedJobRegistry
from unidecode import unidecode

from werkzeug.security import generate_password_hash, check_password_hash
from flask import current_app, render_template
from flask_login import UserMixin
from sqlalchemy.orm import joinedload

from app import db, login
from app.email import task_send_email as send_email

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


class Utilities(object):
    """Static string helpers shared by the import pipeline."""

    @staticmethod
    def sanitize_string(string):
        """Remove the characters that cause problems from a string."""

        # fmt: off
        bad_characters  = ["\\", "/", "<", ">", "?", "!", "*", ":", "|", '"',   "…", "“", "”", "‘", "’"]
        good_characters = ["+",  "+",  "",  "",  "",  "", "-", "-",  "",  "", "...",  "",  "", "'", "'"]
        # fmt: on

        for i, bad_char in enumerate(bad_characters):
            string = string.replace(bad_char, good_characters[i])

        while "  " in string:
            string = string.replace("  ", " ")

        string = string.strip().strip("-").strip(".")
        string = unidecode(string)
        return string


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
        bucket = f"fitzflix:tmdb:requests:{int(now)}"
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


class TMDBMixin(object):
    """TMDB fetch and apply methods shared by the Movie and TVSeries models.

    Each refresh has 2 halves. *_fetch does the network work and returns
    a payload. *_apply writes the payload to the database. Thus, the
    two-phase refresh tasks can run the halves on different queues.
    """

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
            except requests.exceptions.HTTPError:
                admin_user = User.query.filter(User.admin == True).first()
                send_email(
                    "Fitzflix - TMDB ID not found",
                    sender=("Fitzflix", current_app.config["SERVER_EMAIL"]),
                    recipients=[admin_user.email],
                    text_body=render_template(
                        "email/nonexistent_tmdb_id_movie.txt",
                        user=admin_user.email,
                        movie=self,
                        tmdb_id=tmdb_id,
                    ),
                    html_body=render_template(
                        "email/nonexistent_tmdb_id_movie.html",
                        user=admin_user.email,
                        movie=self,
                        tmdb_id=tmdb_id,
                    ),
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

        # Delete the existing records associated with this movie.

        tmdb_collections = TMDBMovieCollection.query.all()
        for collection in tmdb_collections:
            if collection in self.collections:
                self.collections.remove(collection)

        # Cast and crew get the same empty-payload guard as the association
        # groups below (#252). Before, the bulk delete ran before any
        # payload check. Thus, a bad payload with an empty or missing
        # credits section deleted the cast of a film permanently. This is
        # the #251 failure shape. It hits the /people rankings, the cast
        # criteria shelves, and the Name That Frame distractors.

        incoming_credits = tmdb_info.get("credits") or {}
        if (
            incoming_credits.get("cast")
            or MovieCast.query.filter_by(movie_id=self.id).count() == 0
        ):
            MovieCast.query.filter_by(movie_id=self.id).delete()
        else:
            current_app.logger.warning(
                f"{self} TMDB payload carries no cast; keeping the stored rows"
            )
        if (
            incoming_credits.get("crew")
            or MovieCrew.query.filter_by(movie_id=self.id).count() == 0
        ):
            MovieCrew.query.filter_by(movie_id=self.id).delete()
        else:
            current_app.logger.warning(
                f"{self} TMDB payload carries no crew; keeping the stored rows"
            )

        # TMDB can serve a details payload with an empty genre list for a
        # short time, while the rest is intact. The bulk refreshes of
        # 2026-08-07 to 2026-08-13 got such payloads for approximately 16%
        # of the requests. The unconditional delete here then erased the
        # genres of 943 films permanently (#251. The credits glitch of
        # 2026-08-22 had the same failure shape). An empty incoming list
        # never deletes the rows that the record already has. Fitzflix
        # keeps the rows and logs the anomaly. A film can really lose all
        # its genres or keywords on TMDB. That is much more rare than bad
        # data from TMDB for a short time.

        if tmdb_info.get("genres") or self.genres.count() == 0:
            for genre in TMDBGenre.query.all():
                if genre in self.genres:
                    self.genres.remove(genre)
        else:
            current_app.logger.warning(
                f"{self} TMDB payload carries no genres; " f"keeping the stored ones"
            )

        if (tmdb_info.get("keywords") or {}).get(
            "keywords"
        ) or self.keywords.count() == 0:
            for keyword in TMDBKeyword.query.all():
                if keyword in self.keywords:
                    self.keywords.remove(keyword)
        else:
            current_app.logger.warning(
                f"{self} TMDB payload carries no keywords; " f"keeping the stored ones"
            )

        # The remaining association groups carry the same guard (#252).
        # Collections have no guard on purpose. belongs_to_collection is
        # correctly null for most films.

        if (
            tmdb_info.get("production_companies")
            or self.production_companies.count() == 0
        ):
            for company in TMDBProductionCompany.query.all():
                if company in self.production_companies:
                    self.production_companies.remove(company)
        else:
            current_app.logger.warning(
                f"{self} TMDB payload carries no production companies; "
                f"keeping the stored ones"
            )

        if (
            tmdb_info.get("production_countries")
            or self.production_countries.count() == 0
        ):
            for country in TMDBProductionCountry.query.all():
                if country in self.production_countries:
                    self.production_countries.remove(country)
        else:
            current_app.logger.warning(
                f"{self} TMDB payload carries no production countries; "
                f"keeping the stored ones"
            )

        if tmdb_info.get("spoken_languages") or self.spoken_languages.count() == 0:
            for language in TMDBSpokenLanguage.query.all():
                if language in self.spoken_languages:
                    self.spoken_languages.remove(language)
        else:
            current_app.logger.warning(
                f"{self} TMDB payload carries no spoken languages; "
                f"keeping the stored ones"
            )

        if (tmdb_info.get("release_dates") or {}).get(
            "results"
        ) or self.certifications.count() == 0:
            for certification in RefTMDBCertification.query.all():
                if certification in self.certifications:
                    self.certifications.remove(certification)
        else:
            current_app.logger.warning(
                f"{self} TMDB payload carries no release dates; "
                f"keeping the stored certifications"
            )

        # Add the new data from TMDB.

        if tmdb_info.get("external_ids"):
            external_ids = tmdb_info.get("external_ids")
            self.imdb_id = external_ids.get("imdb_id")

        # A scalar keeps its stored value if the KEY is absent (#252). A
        # full details payload always carries every key (null where TMDB
        # has nothing). Thus, a missing key means a partial payload. Do
        # not let it set a populated column to null. A key that is present
        # with null still clears the column. That is TMDB that removes
        # real data.

        self.tmdb_id = tmdb_info.get("id", self.tmdb_id)
        self.tmdb_adult = tmdb_info.get("adult", self.tmdb_adult)
        self.tmdb_backdrop_path = tmdb_info.get(
            "backdrop_path", self.tmdb_backdrop_path
        )
        self.tmdb_budget = tmdb_info.get("budget", self.tmdb_budget)
        self.tmdb_homepage = tmdb_info.get("homepage", self.tmdb_homepage)
        self.tmdb_original_language = tmdb_info.get(
            "original_language", self.tmdb_original_language
        )
        self.tmdb_original_title = tmdb_info.get(
            "original_title", self.tmdb_original_title
        )
        self.tmdb_overview = tmdb_info.get("overview", self.tmdb_overview)
        self.tmdb_popularity = tmdb_info.get("popularity", self.tmdb_popularity)
        self.tmdb_poster_path = tmdb_info.get("poster_path", self.tmdb_poster_path)
        canonical_year = self.year
        if tmdb_info.get("release_date"):
            self.tmdb_release_date = datetime.strptime(
                tmdb_info.get("release_date"), "%Y-%m-%d"
            )
            canonical_year = self.tmdb_release_date.year

        self.tmdb_revenue = tmdb_info.get("revenue", self.tmdb_revenue)
        self.tmdb_runtime = tmdb_info.get("runtime", self.tmdb_runtime)
        self.tmdb_status = tmdb_info.get("status", self.tmdb_status)
        self.tmdb_tagline = tmdb_info.get("tagline", self.tmdb_tagline)
        self.tmdb_title = tmdb_info.get("title", self.tmdb_title)

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
            admin_user = User.query.filter(User.admin == True).first()
            send_email(
                "Fitzflix - Duplicate movie detected",
                sender=("Fitzflix", current_app.config["SERVER_EMAIL"]),
                recipients=[admin_user.email],
                text_body=render_template(
                    "email/duplicate_movie.txt",
                    user=admin_user.email,
                    movie=self,
                    duplicate=duplicate,
                    canonical_title=canonical_title,
                    canonical_year=canonical_year,
                ),
                html_body=render_template(
                    "email/duplicate_movie.html",
                    user=admin_user.email,
                    movie=self,
                    duplicate=duplicate,
                    canonical_title=canonical_title,
                    canonical_year=canonical_year,
                ),
            )

        elif canonical_title:
            self.title = canonical_title
            self.year = canonical_year
        self.tmdb_video = tmdb_info.get("video", self.tmdb_video)
        self.tmdb_vote_average = tmdb_info.get("vote_average", self.tmdb_vote_average)
        self.tmdb_vote_count = tmdb_info.get("vote_count", self.tmdb_vote_count)
        if tmdb_info.get("id"):
            self.tmdb_data_as_of = datetime.now(timezone.utc)

        release_dates = tmdb_info.get("release_dates")
        if release_dates:
            if release_dates.get("results"):
                for country_release in release_dates["results"]:
                    country = country_release.get("iso_3166_1")
                    if country:
                        dates = country_release.get("release_dates")
                        if dates:
                            certification = RefTMDBCertification.query.filter_by(
                                country=country,
                                certification=dates[0].get("certification"),
                            ).first()
                            if certification:
                                self.certifications.append(certification)

        if tmdb_info.get("belongs_to_collection"):
            collection = tmdb_info.get("belongs_to_collection")
            movie_collection = TMDBMovieCollection.query.filter_by(
                id=collection.get("id")
            ).first()
            if not movie_collection:
                movie_collection = TMDBMovieCollection(
                    id=collection.get("id"),
                    tmdb_backdrop_path=collection.get("backdrop_path"),
                    name=collection.get("name"),
                    tmdb_poster_path=collection.get("poster_path"),
                )
                db.session.add(movie_collection)

            if (
                self.collections.filter(
                    TMDBMovieCollection.id == movie_collection.id
                ).count()
                == 0
            ):
                self.collections.append(movie_collection)

        if tmdb_info.get("credits"):
            invalidate_people_ranking()
            credits = tmdb_info.get("credits")
            for person in tmdb_objects(credits.get("cast"), self, "cast"):
                p = TMDBCredit.query.filter_by(id=person.get("id")).first()
                if not p:
                    p = TMDBCredit(
                        id=person.get("id"),
                        name=person.get("name"),
                        gender=person.get("gender"),
                        tmdb_profile_path=person.get("profile_path"),
                    )
                    db.session.add(p)

                if (
                    MovieCast.query.filter_by(
                        movie_id=self.id,
                        credit_id=p.id,
                        character=person.get("character"),
                    ).count()
                    == 0
                ):
                    mc = MovieCast(
                        movie_id=self.id,
                        credit_id=p.id,
                        character=person.get("character"),
                        billing_order=person.get("order"),
                    )
                    db.session.add(mc)

            for person in tmdb_objects(credits.get("crew"), self, "crew"):
                p = TMDBCredit.query.filter_by(id=person.get("id")).first()
                if not p:
                    p = TMDBCredit(
                        id=person.get("id"),
                        name=person.get("name"),
                        gender=person.get("gender"),
                        tmdb_profile_path=person.get("profile_path"),
                    )
                    db.session.add(p)

                if (
                    MovieCrew.query.filter_by(
                        movie_id=self.id,
                        credit_id=p.id,
                        department=person.get("department"),
                        job=person.get("job"),
                    ).count()
                    == 0
                ):
                    mc = MovieCrew(
                        movie_id=self.id,
                        credit_id=p.id,
                        department=person.get("department"),
                        job=person.get("job"),
                    )
                    db.session.add(mc)

        if tmdb_info.get("genres"):
            tmdb_genres = tmdb_info.get("genres")
            for genre in tmdb_genres:
                g = TMDBGenre.query.filter_by(id=genre.get("id")).first()
                if not g:
                    g = TMDBGenre(id=genre.get("id"), name=genre.get("name"))
                    db.session.add(g)

                if self.genres.filter(TMDBGenre.id == g.id).count() == 0:
                    self.genres.append(g)

        if tmdb_info.get("keywords"):
            tmdb_keywords = tmdb_info.get("keywords")
            for keyword in tmdb_keywords.get("keywords"):
                k = TMDBKeyword.query.filter_by(id=keyword.get("id")).first()
                if not k:
                    k = TMDBKeyword(id=keyword.get("id"), name=keyword.get("name"))
                    db.session.add(k)

                if self.keywords.filter(TMDBKeyword.id == k.id).count() == 0:
                    self.keywords.append(k)

        if tmdb_info.get("production_companies"):
            tmdb_production_companies = tmdb_info.get("production_companies")
            for company in tmdb_production_companies:
                prod_company = TMDBProductionCompany.query.filter_by(
                    id=company.get("id")
                ).first()
                if not prod_company:
                    prod_company = TMDBProductionCompany(
                        id=company.get("id"),
                        name=company.get("name"),
                        country=company.get("origin_country"),
                        tmdb_logo_path=company.get("logo_path"),
                    )
                    db.session.add(prod_company)

                if (
                    self.production_companies.filter(
                        TMDBProductionCompany.id == prod_company.id
                    ).count()
                    == 0
                ):
                    self.production_companies.append(prod_company)

        if tmdb_info.get("production_countries"):
            tmdb_production_countries = tmdb_info.get("production_countries")
            for country in tmdb_production_countries:
                prod_country = TMDBProductionCountry.query.filter_by(
                    id=country.get("iso_3166_1")
                ).first()
                if not prod_country:
                    prod_country = TMDBProductionCountry(
                        id=country.get("iso_3166_1"), name=country.get("name")
                    )
                    db.session.add(prod_country)

                if (
                    self.production_countries.filter(
                        TMDBProductionCountry.id == prod_country.id
                    ).count()
                    == 0
                ):
                    self.production_countries.append(prod_country)

        if tmdb_info.get("spoken_languages"):
            tmdb_languages = tmdb_info.get("spoken_languages")
            for language in tmdb_languages:
                spoken_lang = TMDBSpokenLanguage.query.filter_by(
                    id=language.get("iso_639_1")
                ).first()
                if not spoken_lang:
                    spoken_lang = TMDBSpokenLanguage(
                        id=language.get("iso_639_1"), name=language.get("name")
                    )
                    db.session.add(spoken_lang)

                if (
                    self.spoken_languages.filter(
                        TMDBSpokenLanguage.id == spoken_lang.id
                    ).count()
                    == 0
                ):
                    self.spoken_languages.append(spoken_lang)

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

        for related in (
            self.collections,
            self.genres,
            self.keywords,
            self.production_companies,
            self.production_countries,
            self.spoken_languages,
            self.certifications,
        ):
            for row in related.all():
                related.remove(row)

        self.imdb_id = None
        self.tmdb_id = None
        self.tmdb_adult = None
        self.tmdb_backdrop_path = None
        self.tmdb_budget = None
        self.tmdb_homepage = None
        self.tmdb_original_language = None
        self.tmdb_original_title = None
        self.tmdb_overview = None
        self.tmdb_popularity = None
        self.tmdb_poster_path = None
        self.tmdb_release_date = None
        self.tmdb_revenue = None
        self.tmdb_runtime = None
        self.tmdb_status = None
        self.tmdb_tagline = None
        self.tmdb_title = None
        self.tmdb_video = None
        self.tmdb_vote_average = None
        self.tmdb_vote_count = None
        self.tmdb_data_as_of = None
        self.tmdb_ignored = True

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
            except requests.exceptions.HTTPError:
                admin_user = User.query.filter(User.admin == True).first()
                send_email(
                    "Fitzflix - TMDB ID not found",
                    sender=("Fitzflix", current_app.config["SERVER_EMAIL"]),
                    recipients=[admin_user.email],
                    text_body=render_template(
                        "email/nonexistent_tmdb_id_tv.txt",
                        user=admin_user.email,
                        tv=self,
                        tmdb_id=tmdb_id,
                    ),
                    html_body=render_template(
                        "email/nonexistent_tmdb_id_tv.html",
                        user=admin_user.email,
                        tv=self,
                        tmdb_id=tmdb_id,
                    ),
                )
                return None
            current_app.logger.debug(f"{r.url}: {r.json()}")
            tmdb_info = r.json()

        return tmdb_info or None

    def tmdb_tv_apply(self, tmdb_info):
        """Do the database half of a TMDB TV refresh. See tmdb_movie_apply."""

        if not tmdb_info:
            return self

        # Delete the existing records associated with this TV series.
        # Genres and keywords get the same empty-payload guard as
        # tmdb_movie_apply (#251). The TV keyword lists are in "results".

        if tmdb_info.get("genres") or self.genres.count() == 0:
            for genre in TMDBGenre.query.all():
                if genre in self.genres:
                    self.genres.remove(genre)
        else:
            current_app.logger.warning(
                f"{self} TMDB payload carries no genres; " f"keeping the stored ones"
            )

        if (tmdb_info.get("keywords") or {}).get(
            "results"
        ) or self.keywords.count() == 0:
            for keyword in TMDBKeyword.query.all():
                if keyword in self.keywords:
                    self.keywords.remove(keyword)
        else:
            current_app.logger.warning(
                f"{self} TMDB payload carries no keywords; " f"keeping the stored ones"
            )

        if tmdb_info.get("networks") or self.networks.count() == 0:
            for network in TMDBNetwork.query.all():
                if network in self.networks:
                    self.networks.remove(network)
        else:
            current_app.logger.warning(
                f"{self} TMDB payload carries no networks; keeping the stored ones"
            )

        if (
            tmdb_info.get("production_companies")
            or self.production_companies.count() == 0
        ):
            for company in TMDBProductionCompany.query.all():
                if company in self.production_companies:
                    self.production_companies.remove(company)
        else:
            current_app.logger.warning(
                f"{self} TMDB payload carries no production companies; "
                f"keeping the stored ones"
            )

        if tmdb_info.get("seasons") or self.seasons.count() == 0:
            for season in TMDBSeason.query.all():
                if season in self.seasons:
                    self.seasons.remove(season)
        else:
            current_app.logger.warning(
                f"{self} TMDB payload carries no seasons; keeping the stored ones"
            )

        # Add the new data from TMDB.

        if tmdb_info.get("external_ids"):
            external_ids = tmdb_info.get("external_ids")
            self.imdb_id = external_ids.get("imdb_id")
            self.tvdb_id = external_ids.get("tvdb_id")

        self.tmdb_id = tmdb_info.get("id")
        # A scalar keeps its stored value if the key is absent, the same
        # as the movie side (#252).

        self.tmdb_backdrop_path = tmdb_info.get(
            "backdrop_path", self.tmdb_backdrop_path
        )
        if tmdb_info.get("first_air_date"):
            self.tmdb_first_air_date = datetime.strptime(
                tmdb_info.get("first_air_date"), "%Y-%m-%d"
            )

        self.tmdb_homepage = tmdb_info.get("homepage", self.tmdb_homepage)
        self.tmdb_in_production = tmdb_info.get(
            "in_production", self.tmdb_in_production
        )
        if tmdb_info.get("last_air_date"):
            self.tmdb_last_air_date = datetime.strptime(
                tmdb_info.get("last_air_date"), "%Y-%m-%d"
            )

        self.tmdb_name = tmdb_info.get("name", self.tmdb_name)
        if tmdb_info.get("status") == "Ended":
            self.tmdb_number_of_episodes = tmdb_info.get("number_of_episodes")
            self.tmdb_number_of_seasons = tmdb_info.get("number_of_seasons")

        # The US content rating is in the appended content_ratings block.
        # A payload with no US entry keeps the stored rating, the same as
        # the empty-list guards above (#251).

        for country_rating in (tmdb_info.get("content_ratings") or {}).get(
            "results"
        ) or []:
            if country_rating.get("iso_3166_1") == "US" and country_rating.get(
                "rating"
            ):
                self.tmdb_content_rating = country_rating.get("rating")
                break

        self.tmdb_original_language = tmdb_info.get(
            "original_language", self.tmdb_original_language
        )
        self.tmdb_original_name = tmdb_info.get(
            "original_name", self.tmdb_original_name
        )
        self.tmdb_overview = tmdb_info.get("overview", self.tmdb_overview)
        self.tmdb_popularity = tmdb_info.get("popularity", self.tmdb_popularity)
        self.tmdb_poster_path = tmdb_info.get("poster_path", self.tmdb_poster_path)
        self.tmdb_status = tmdb_info.get("status", self.tmdb_status)
        self.tmdb_type = tmdb_info.get("type", self.tmdb_type)
        self.tmdb_vote_average = tmdb_info.get("vote_average", self.tmdb_vote_average)
        self.tmdb_vote_count = tmdb_info.get("vote_count", self.tmdb_vote_count)
        if tmdb_info.get("id"):
            self.tmdb_data_as_of = datetime.now(timezone.utc)

        if tmdb_info.get("genres"):
            tmdb_genres = tmdb_info.get("genres")
            for genre in tmdb_genres:
                g = TMDBGenre.query.filter_by(id=genre.get("id")).first()
                if not g:
                    g = TMDBGenre(id=genre.get("id"), name=genre.get("name"))
                    db.session.add(g)

                if self.genres.filter(TMDBGenre.id == g.id).count() == 0:
                    self.genres.append(g)

        if tmdb_info.get("keywords"):
            tmdb_keywords = tmdb_info.get("keywords")
            for keyword in tmdb_keywords.get("results"):
                k = TMDBKeyword.query.filter_by(id=keyword.get("id")).first()
                if not k:
                    k = TMDBKeyword(id=keyword.get("id"), name=keyword.get("name"))
                    db.session.add(k)

                if self.keywords.filter(TMDBKeyword.id == k.id).count() == 0:
                    self.keywords.append(k)

        if tmdb_info.get("networks"):
            tmdb_networks = tmdb_info.get("networks")
            for network in tmdb_networks:
                n = TMDBNetwork.query.filter_by(id=network.get("id")).first()
                if not n:
                    n = TMDBNetwork(
                        id=network.get("id"),
                        tmdb_logo_path=network.get("logo_path"),
                        name=network.get("name"),
                        origin_country=network.get("origin_country"),
                    )
                    db.session.add(n)

                if self.networks.filter(TMDBNetwork.id == n.id).count() == 0:
                    self.networks.append(n)

        if tmdb_info.get("production_companies"):
            tmdb_production_companies = tmdb_info.get("production_companies")
            for company in tmdb_production_companies:
                prod_company = TMDBProductionCompany.query.filter_by(
                    id=company.get("id")
                ).first()
                if not prod_company:
                    prod_company = TMDBProductionCompany(
                        id=company.get("id"),
                        name=company.get("name"),
                        country=company.get("origin_country"),
                        tmdb_logo_path=company.get("logo_path"),
                    )
                    db.session.add(prod_company)

                if (
                    self.production_companies.filter(
                        TMDBProductionCompany.id == prod_company.id
                    ).count()
                    == 0
                ):
                    self.production_companies.append(prod_company)

        if tmdb_info.get("seasons"):
            tmdb_seasons = tmdb_info.get("seasons")
            for season in tmdb_seasons:
                s = TMDBSeason.query.filter_by(id=season.get("id")).first()
                if not s:
                    s = TMDBSeason(id=season.get("id"))
                    db.session.add(s)

                # Set the fields on every refresh, not only at creation.
                # The original create-only code kept episode_count frozen
                # at the value the season had when first seen. The census
                # of the TV overhaul found announcement-time counts that
                # were years old.

                s.air_date = (
                    datetime.strptime(season.get("air_date"), "%Y-%m-%d")
                    if season.get("air_date")
                    else None
                )
                s.episode_count = season.get("episode_count")
                s.name = season.get("name")
                s.overview = season.get("overview")
                s.tmdb_poster_path = season.get("poster_path")
                s.season_number = season.get("season_number")

                if self.seasons.filter(TMDBSeason.id == s.id).count() == 0:
                    self.seasons.append(s)

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
                p = TMDBCredit.query.filter_by(id=person.get("id")).first()
                if not p:
                    p = TMDBCredit(
                        id=person.get("id"),
                        name=person.get("name"),
                        gender=person.get("gender"),
                        tmdb_profile_path=person.get("profile_path"),
                    )
                    db.session.add(p)

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
                p = TMDBCredit.query.filter_by(id=person.get("id")).first()
                if not p:
                    p = TMDBCredit(
                        id=person.get("id"),
                        name=person.get("name"),
                        gender=person.get("gender"),
                        tmdb_profile_path=person.get("profile_path"),
                    )
                    db.session.add(p)

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

        for related in (
            self.genres,
            self.keywords,
            self.networks,
            self.production_companies,
            self.seasons,
        ):
            for row in related.all():
                related.remove(row)

        self.imdb_id = None
        self.tvdb_id = None
        self.tmdb_id = None
        self.tmdb_backdrop_path = None
        self.tmdb_content_rating = None
        self.tmdb_first_air_date = None
        self.tmdb_homepage = None
        self.tmdb_poster_path = None
        self.tmdb_in_production = None
        self.tmdb_last_air_date = None
        self.tmdb_name = None
        self.tmdb_number_of_seasons = None
        self.tmdb_number_of_episodes = None
        self.tmdb_original_language = None
        self.tmdb_original_name = None
        self.tmdb_overview = None
        self.tmdb_popularity = None
        self.tmdb_status = None
        self.tmdb_type = None
        self.tmdb_vote_average = None
        self.tmdb_vote_count = None
        self.tmdb_data_as_of = None
        self.tmdb_ignored = True

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

    def get_reset_password_token(self, expires_in=600):
        """Return a signed, short-lived JWT for the password-reset email link."""

        return jwt.encode(
            {"reset_password": self.id, "exp": time() + expires_in},
            current_app.config["SECRET_KEY"],
            algorithm="HS256",
        )

    @staticmethod
    def verify_reset_password_token(token):
        """Return the User that a reset token identifies, or None.

        The result is None if the token is not valid or has expired."""

        try:
            id = jwt.decode(
                token, current_app.config["SECRET_KEY"], algorithms=["HS256"]
            )["reset_password"]
        except:
            return
        return db.session.get(User, id)

    def get_queue_details(self):
        """Return the running and queued background jobs for the queue page.

        This method merges the import, transcode, and file-operation
        queues into one ordered list with queue positions.
        """

        imports = StartedJobRegistry("fitzflix-import", connection=current_app.redis)
        imports_running = imports.get_job_ids()
        transcodes = StartedJobRegistry(
            "fitzflix-transcode", connection=current_app.redis
        )
        transcodes_running = transcodes.get_job_ids()
        file_operations = StartedJobRegistry(
            "fitzflix-file-operation", connection=current_app.redis
        )
        file_operations_running = file_operations.get_job_ids()

        # The running banners keep their relative order by the time that
        # each FILE first started to run (the original banner-order
        # request from Glenn). The work of a file steps between queues as
        # it progresses. Localization is on import. The library copy is on
        # file-operation. Each step is a new job with a new started_at.
        # Before, that moved the banner to the end of the list. The
        # first_run anchor of the pipeline trail survives the steps. A job
        # without a trail sorts by its own start, converted to the local
        # wall clock of the trail.

        from app.pipeline import first_enqueued, first_run

        def first_run_anchor(job):
            """Return the stable sort key of the job among the running banners."""

            anchor = first_run(current_app.redis, job)
            if anchor:
                return anchor
            if job.started_at:
                return job.started_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            return "9999"

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
            answers. All these jobs still list on the queue page."""

            return job is not None and not (job.func_name or "").startswith(
                "app.frames."
            )

        details = {}
        details["count"] = self.get_queue_count()
        details["running"] = []

        for job_id in imports_running:
            job = current_app.import_queue.fetch_job(job_id)
            if banner_worthy(job):
                details["running"].append(
                    {
                        "id": job.id,
                        "status": job.get_status(),
                        "enqueued_at": enqueue_anchor(job),
                        "started_at": job.started_at,
                        "ended_at": job.ended_at,
                        "description": job.meta.get("description", job.description),
                        "first_run": first_run_anchor(job),
                        "progress": (
                            job.meta.get("progress", -1) if job is not None else 100
                        ),
                    }
                )

        for job_id in transcodes_running:
            job = current_app.transcode_queue.fetch_job(job_id)
            if banner_worthy(job):
                details["running"].append(
                    {
                        "id": job.id,
                        "status": job.get_status(),
                        "enqueued_at": enqueue_anchor(job),
                        "started_at": job.started_at,
                        "ended_at": job.ended_at,
                        "description": job.meta.get("description", job.description),
                        "first_run": first_run_anchor(job),
                        "progress": (
                            job.meta.get("progress", -1) if job is not None else 100
                        ),
                    }
                )

        for job_id in file_operations_running:
            job = current_app.file_queue.fetch_job(job_id)
            if banner_worthy(job):
                details["running"].append(
                    {
                        "id": job.id,
                        "status": job.get_status(),
                        "enqueued_at": enqueue_anchor(job),
                        "started_at": job.started_at,
                        "ended_at": job.ended_at,
                        "description": job.meta.get("description", job.description),
                        "first_run": first_run_anchor(job),
                        "progress": (
                            job.meta.get("progress", -1) if job is not None else 100
                        ),
                    }
                )

        details["running"] = sorted(details["running"], key=lambda d: d["first_run"])

        # Make the list of all the localizations and transcodes in the queue.

        details["all"] = []
        for job_id in imports_running:
            job = current_app.import_queue.fetch_job(job_id)
            if job:
                details["all"].append(
                    {
                        "id": job.id,
                        "status": job.get_status(),
                        "enqueued_at": enqueue_anchor(job),
                        "started_at": job.started_at,
                        "ended_at": job.ended_at,
                        "description": job.meta.get("description", job.description),
                    }
                )

        for job_id in transcodes_running:
            job = current_app.transcode_queue.fetch_job(job_id)
            if job:
                details["all"].append(
                    {
                        "id": job.id,
                        "status": job.get_status(),
                        "enqueued_at": enqueue_anchor(job),
                        "started_at": job.started_at,
                        "ended_at": job.ended_at,
                        "description": job.meta.get("description", job.description),
                    }
                )

        for job_id in file_operations_running:
            job = current_app.file_queue.fetch_job(job_id)
            if job:
                details["all"].append(
                    {
                        "id": job.id,
                        "status": job.get_status(),
                        "enqueued_at": enqueue_anchor(job),
                        "started_at": job.started_at,
                        "ended_at": job.ended_at,
                        "description": job.meta.get("description", job.description),
                    }
                )

        for job_id in current_app.import_queue.job_ids:
            job = current_app.import_queue.fetch_job(job_id)
            if job:
                details["all"].append(
                    {
                        "id": job.id,
                        "status": job.get_status(),
                        "enqueued_at": enqueue_anchor(job),
                        "started_at": job.started_at,
                        "ended_at": job.ended_at,
                        "description": job.meta.get("description", job.description),
                    }
                )

        for job_id in current_app.transcode_queue.job_ids:
            job = current_app.transcode_queue.fetch_job(job_id)
            if job:
                details["all"].append(
                    {
                        "id": job.id,
                        "status": job.get_status(),
                        "enqueued_at": enqueue_anchor(job),
                        "started_at": job.started_at,
                        "ended_at": job.ended_at,
                        "description": job.meta.get("description", job.description),
                    }
                )

        for job_id in current_app.file_queue.job_ids:
            job = current_app.file_queue.fetch_job(job_id)
            if job:
                details["all"].append(
                    {
                        "id": job.id,
                        "status": job.get_status(),
                        "enqueued_at": enqueue_anchor(job),
                        "started_at": job.started_at,
                        "ended_at": job.ended_at,
                        "description": job.meta.get("description", job.description),
                    }
                )

        # A deferred retry (a file that is still copying in, or a locked
        # title) is in the ScheduledJobRegistry of each queue. It is not
        # in the queue itself. Before, these retries were not visible
        # here. They
        # showed only as amber chips on the in-flight list of the File
        # Activity page. The trail chips moved onto the queue rows (Glenn,
        # 2026-08). Now the queue page is the one place that shows all the
        # work in flight. Thus, the retries list too. They come after the
        # live queue, with no position, because they are not in line yet.

        scheduled = []
        for queue in (
            current_app.import_queue,
            current_app.transcode_queue,
            current_app.file_queue,
        ):
            registry = ScheduledJobRegistry(queue=queue)
            for job_id in registry.get_job_ids():
                job = queue.fetch_job(job_id)
                if job:
                    scheduled.append(
                        {
                            "id": job.id,
                            "status": "scheduled",
                            "enqueued_at": enqueue_anchor(job),
                            "started_at": None,
                            "ended_at": None,
                            "scheduled_for": registry.get_scheduled_time(job_id),
                            "description": job.meta.get("description", job.description),
                        }
                    )

        details["all"] = sorted(
            details["all"],
            key=lambda d: (
                d["started_at"] is None,
                d["started_at"],
                d["enqueued_at"] is None,
                d["enqueued_at"],
            ),
        )

        for i, task in enumerate(details["all"]):
            details["all"][i]["position"] = i + 1

        scheduled.sort(
            key=lambda d: (d["scheduled_for"] is None, d["scheduled_for"] or "")
        )
        for task in scheduled:
            task["position"] = None
        details["all"].extend(scheduled)

        return details

    def get_queue_count(self):
        """Return the total of queued and running jobs for the navbar badge."""

        imports = StartedJobRegistry("fitzflix-import", connection=current_app.redis)
        imports_running = imports.get_job_ids()
        transcodes = StartedJobRegistry(
            "fitzflix-transcode", connection=current_app.redis
        )
        transcodes_running = transcodes.get_job_ids()
        file_operations = StartedJobRegistry(
            "fitzflix-file-operation", connection=current_app.redis
        )
        file_operations_running = file_operations.get_job_ids()
        jobs_in_queue = (
            len(imports_running)
            + len(transcodes_running)
            + len(file_operations_running)
            + len(current_app.import_queue.job_ids)
            + len(current_app.transcode_queue.job_ids)
            + len(current_app.file_queue.job_ids)
        )
        return jobs_in_queue


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

    # The time of the last edit of the review text. date_reviewed keeps
    # the original review date.

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


class Movie(db.Model, TMDBMixin, Utilities):
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

    collections = db.relationship(
        "TMDBMovieCollection",
        secondary=movie_collections,
        backref=db.backref("movies", lazy="dynamic"),
        lazy="dynamic",
        cascade="all,delete",
    )
    genres = db.relationship(
        "TMDBGenre",
        secondary=movie_genres,
        backref=db.backref("movies", lazy="dynamic"),
        lazy="dynamic",
        cascade="all,delete",
    )
    keywords = db.relationship(
        "TMDBKeyword",
        secondary=movie_keywords,
        backref=db.backref("movies", lazy="dynamic"),
        lazy="dynamic",
        cascade="all,delete",
    )
    production_companies = db.relationship(
        "TMDBProductionCompany",
        secondary=movie_production_companies,
        backref=db.backref("movies", lazy="dynamic"),
        lazy="dynamic",
        cascade="all,delete",
    )
    production_countries = db.relationship(
        "TMDBProductionCountry",
        secondary=movie_production_countries,
        backref=db.backref("movies", lazy="dynamic"),
        lazy="dynamic",
        cascade="all,delete",
    )
    spoken_languages = db.relationship(
        "TMDBSpokenLanguage",
        secondary=movie_spoken_languages,
        backref=db.backref("movies", lazy="dynamic"),
        lazy="dynamic",
        cascade="all,delete",
    )
    certifications = db.relationship(
        "RefTMDBCertification",
        secondary=movie_certifications,
        backref=db.backref("movies", lazy="dynamic"),
        lazy="dynamic",
        cascade="all,delete",
    )

    __table_args__ = (db.UniqueConstraint("title", "year"),)

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
    tmdb_poster_path = db.Column(db.String(64))
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
        cascade="all,delete",
    )
    keywords = db.relationship(
        "TMDBKeyword",
        secondary=tv_keywords,
        backref=db.backref("tv_series", lazy="dynamic"),
        lazy="dynamic",
        cascade="all,delete",
    )
    networks = db.relationship(
        "TMDBNetwork",
        secondary=tv_networks,
        backref=db.backref("tv_series", lazy="dynamic"),
        lazy="dynamic",
        cascade="all,delete",
    )
    production_companies = db.relationship(
        "TMDBProductionCompany",
        secondary=tv_production_companies,
        backref=db.backref("tv_series", lazy="dynamic"),
        lazy="dynamic",
        cascade="all,delete",
    )
    seasons = db.relationship(
        "TMDBSeason",
        secondary=tv_seasons,
        backref=db.backref("tv_series", lazy="dynamic"),
        lazy="dynamic",
        cascade="all,delete",
    )

    def __repr__(self):
        return f"<TVSeries '{self.title}'>"


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
    video_bitrate_kbps = db.Column(db.Integer)
    filesize_bytes = db.Column(db.BigInteger)
    date_added = db.Column(
        db.DateTime, nullable=False, index=True, default=db.func.utc_timestamp()
    )
    date_updated = db.Column(db.DateTime, index=True)
    date_localized = db.Column(db.DateTime, index=True)
    date_transcoded = db.Column(db.DateTime, index=True)

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
    aws_untouched_key = db.Column(db.String(255), index=True)
    aws_untouched_filesize_bytes = db.Column(db.BigInteger)
    aws_untouched_date_uploaded = db.Column(db.DateTime)
    aws_untouched_date_deleted = db.Column(db.DateTime)

    # Set when an archive upload was lost and the S3 object is known to be
    # older than the local file. No other field can tell. The key still
    # exists and the date is from the old upload. Thus, every existing
    # check reads the row as consistent. A successful upload clears it.

    aws_untouched_stale = db.Column(
        db.Boolean, nullable=False, server_default="0", default=False
    )
    subtrack = db.relationship(
        "FileSubtitleTrack", backref="file", lazy="select", cascade="all,delete"
    )
    audiotrack = db.relationship(
        "FileAudioTrack", backref="file", lazy="select", cascade="all,delete"
    )

    # The derived copies. These are the Handbrake transcodes made FROM
    # this file. The rows cascade away with their source. The physical
    # delete is the job of the delete sites (see app.transcodes).

    derived_files = db.relationship(
        "DerivedFile",
        backref="source_file",
        lazy="select",
        cascade="all,delete-orphan",
        passive_deletes=True,
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
    }

    def __repr__(self):
        return f"<File '{self.plex_title}'>"

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
        """Delete the library copy and the transcoded sibling, if any.

        A missing file is not an error. With delete_directory_tree, this
        method also deletes the folders that became empty.
        """

        file_to_delete = os.path.join(current_app.config["LIBRARY_DIR"], self.file_path)
        transcoded_file = os.path.join(
            current_app.config["TRANSCODES_DIR"],
            self.dirname,
            f"{self.plex_title}.{current_app.config['HANDBRAKE_EXTENSION']}",
        )
        try:
            os.remove(file_to_delete)

        except FileNotFoundError:
            pass

        else:
            current_app.logger.info(f"Deleted local file '{file_to_delete}'")

        # Delete the transcoded file and its directory if they exist.

        try:
            os.remove(transcoded_file)
        except FileNotFoundError:
            pass

        try:
            os.removedirs(os.path.dirname(transcoded_file))
        except OSError:
            pass

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
            file_identifier = {
                "title": file.movie.title,
                "year": file.movie.year,
                "feature_type": (
                    file.feature_type.feature_type if file.feature_type else None
                ),
                "plex_title": file.plex_title,
                "edition": file.edition,
            }

        elif self.media_library == "TV Shows":
            file = (
                File.query.join(TVSeries, (TVSeries.id == File.series_id))
                .filter(File.id == self.id)
                .first()
            )
            file_identifier = {
                "title": file.tv_series.title,
                "season": file.season,
                "episode": file.episode,
            }

        file_identifier = json.dumps(file_identifier)
        return file_identifier

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
                            db.and_(
                                db.func.concat(File.plex_title, " - Full Screen")
                                == self.plex_title,
                                db.or_(
                                    db.func.concat("Full Screen") == self.edition,
                                    db.func.concat(File.edition, " - Full Screen")
                                    == self.edition,
                                ).self_group(),
                                File.fullscreen == False,
                                # RefQuality.preference >= source_quality.preference
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

        return better_files

    def find_worse_files(self):
        """Return the same-title files that this file outranks.

        These are the candidates for removal after an upgrade.
        """

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
            # removes its predecessor.

            worse_files = (
                File.query.join(RefQuality, (RefQuality.id == File.quality_id))
                .options(joinedload(File.quality, innerjoin=True))
                .filter(
                    File.series_id == self.series_id,
                    File.season == self.season,
                    File.episode == self.episode,
                    db.or_(
                        File.last_episode < self.last_episode,
                        db.and_(
                            File.last_episode == self.last_episode,
                            RefQuality.preference <= self.quality.preference,
                        ).self_group(),
                    ),
                    db.or_(File.fullscreen == self.fullscreen, File.fullscreen == True),
                    File.id != self.id,
                )
                .all()
            )

        return worse_files


class DerivedFile(db.Model):
    """A file derived from a library original.

    Today these are the Handbrake transcodes under TRANSCODES_DIR. In
    the future they include the 4K to SDR and the Dolby Vision
    conversions.

    This is NOT a File row. That is intentional. File.file_path is
    relative to LIBRARY_DIR and unique. Every ranking, shopping, and
    import-replace query treats a File row as an original. Derived
    copies in that table would need a filter at every one of those
    sites, with no exception. A missed filter aims a delete at the
    wrong root. Their own table keeps the derived copies invisible to
    all of that. The source_file_id link gives the movie and file pages
    and the linked delete all that they need. file_path here is
    relative to TRANSCODES_DIR."""

    id = db.Column(db.Integer, primary_key=True)
    source_file_id = db.Column(
        db.Integer,
        db.ForeignKey("file.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind = db.Column(db.String(32), nullable=False, default="handbrake")
    file_path = db.Column(db.String(512), nullable=False, unique=True, index=True)
    basename = db.Column(db.String(255), nullable=False)
    filesize_bytes = db.Column(db.BigInteger)
    date_created = db.Column(
        db.DateTime, nullable=False, default=db.func.utc_timestamp()
    )

    def __repr__(self):
        """Return the path of the derived copy, marked by kind."""

        return f"<DerivedFile {self.kind} '{self.file_path}'>"


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
    acted_in = db.relationship(
        "MovieCast", backref="starring", lazy="dynamic", cascade="all,delete"
    )
    crewed_on = db.relationship(
        "MovieCrew", backref="crewed", lazy="dynamic", cascade="all,delete"
    )
    tv_acted_in = db.relationship(
        "TVCast", backref="starring", lazy="dynamic", cascade="all,delete"
    )
    tv_crewed_on = db.relationship(
        "TVCrew", backref="crewed", lazy="dynamic", cascade="all,delete"
    )

    def __repr__(self):
        return f"<TMDBCredit '{self.name}'>"


class MovieCast(db.Model):
    """A join row for the acting role of a credit on a movie."""

    id = db.Column(db.Integer, primary_key=True)
    movie_id = db.Column(db.Integer, db.ForeignKey("movie.id"))
    credit_id = db.Column(db.Integer, db.ForeignKey("tmdb_credit.id"))
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
    """Load a user for flask-login."""

    return db.session.get(User, int(id))


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
    """Rank each movie file in its title/feature/edition group by quality."""

    return (
        db.func.row_number()
        .over(
            partition_by=(
                Movie.id,
                File.feature_type_id,
                File.plex_title,
                File.edition,
            ),
            order_by=(File.fullscreen.asc(), RefQuality.preference.desc()),
        )
        .label("rank")
    )


def tv_file_rank():
    """Rank each episode file in its series/season/episode group.

    The edition is not part of the group. That is intentional. For a TV
    file, it only carries the optional episode title. Thus, 2 copies of
    the same episode compete, with or without a title in their
    filenames.
    """

    return (
        db.func.row_number()
        .over(
            partition_by=(TVSeries.id, File.season, File.episode),
            order_by=(
                File.fullscreen.asc(),
                RefQuality.preference.desc(),
                File.last_episode.desc(),
            ),
        )
        .label("rank")
    )
