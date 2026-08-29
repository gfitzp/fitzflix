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
        """Given an arbitrary string, clean it of troublesome characters."""

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
    """Drop the /people page's cached rankings (Aug 2026): the ranked
    list of every credited person is a full aggregation over the cast
    and crew tables, so it's held in Redis and rebuilt only after a
    credit write — the TMDB apply methods call this."""

    redis = current_app.redis
    keys = [PEOPLE_RANKING_KEY.format(role=role) for role in ("cast", "crew", "all")]
    redis.delete(*keys)


def tmdb_get(url, **kwargs):
    """GET a TMDB API resource through a shared rate limiter.

    TMDB rate-limits at roughly 40-50 requests per second per IP
    (https://developer.themoviedb.org/docs/rate-limiting). A Redis counter
    keyed on the current second is shared by every worker and web process,
    so their combined request rate stays capped at
    TMDB_REQUESTS_PER_SECOND no matter how many run concurrently.
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
    """Yield the dict entries of a TMDB credits list, logging and
    skipping anything else.

    The overnight TV refresh of 2026-08-22 failed on 14 of 25 series
    because TMDB served a bare list where a cast member's role object
    belongs — for a few seconds, and clean again by noon. One malformed
    entry used to abort the whole apply with an AttributeError and no
    record of the shape; now the entry is logged with its fragment and
    skipped, so the rest of the payload still lands.
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
    """TMDB fetch/apply methods shared by the Movie and TVSeries models.

    Each refresh is split in half: *_fetch does the network work and
    returns a payload, *_apply writes it to the database — so the
    two-phase refresh tasks can run the halves on different queues.
    """

    def tmdb_movie_fetch(self, tmdb_id=None):
        """Network half of a TMDB movie refresh: search (when no id is
        given) and pull the movie details. Writes nothing to the database,
        so concurrent fetches are safe; returns the details payload for
        tmdb_movie_apply, or None with no match. Artwork isn't stored —
        the templates hotlink TMDB's image CDN."""

        tmdb_info = {}
        if not current_app.config["TMDB_API_KEY"]:
            return None
        tmdb_api_key = current_app.config["TMDB_API_KEY"]
        tmdb_api_url = current_app.config["TMDB_API_URL"]

        # Request only the appended blocks tmdb_movie_apply reads

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
        """Database half of a TMDB movie refresh: replace this movie's TMDB
        fields and associations with the fetched payload. No network calls
        here — artwork is already on disk — so it belongs on the
        single-worker sql queue, serialized against other database work."""

        if not tmdb_info:
            return self

        # Delete any existing records associated with this movie

        tmdb_collections = TMDBMovieCollection.query.all()
        for collection in tmdb_collections:
            if collection in self.collections:
                self.collections.remove(collection)

        MovieCast.query.filter_by(movie_id=self.id).delete()
        MovieCrew.query.filter_by(movie_id=self.id).delete()

        # TMDB can transiently serve a details payload whose genre list
        # is empty while the rest is intact — the Aug 7-13 2026 bulk
        # refreshes got such payloads for ~16% of requests, and the
        # unconditional wipe here then erased 943 films' genres for
        # good (#251; the Aug 22 credits glitch was the same failure
        # shape). An empty incoming list never wipes rows the record
        # already has: they're kept and the anomaly logged, since a
        # film genuinely losing its every genre or keyword on TMDB is
        # far rarer than TMDB briefly serving bad data.

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

        tmdb_production_companies = TMDBProductionCompany.query.all()
        for company in tmdb_production_companies:
            if company in self.production_companies:
                self.production_companies.remove(company)

        tmdb_production_countries = TMDBProductionCountry.query.all()
        for country in tmdb_production_countries:
            if country in self.production_countries:
                self.production_countries.remove(country)

        tmdb_spoken_languages = TMDBSpokenLanguage.query.all()
        for language in tmdb_spoken_languages:
            if language in self.spoken_languages:
                self.spoken_languages.remove(language)

        ref_tmdb_certifications = RefTMDBCertification.query.all()
        for certification in ref_tmdb_certifications:
            if certification in self.certifications:
                self.certifications.remove(certification)

        # Add fresh new data from TMDB

        if tmdb_info.get("external_ids"):
            external_ids = tmdb_info.get("external_ids")
            self.imdb_id = external_ids.get("imdb_id")

        self.tmdb_id = tmdb_info.get("id")
        self.tmdb_adult = tmdb_info.get("adult")
        self.tmdb_backdrop_path = tmdb_info.get("backdrop_path")
        self.tmdb_budget = tmdb_info.get("budget")
        self.tmdb_homepage = tmdb_info.get("homepage")
        self.tmdb_original_language = tmdb_info.get("original_language")
        self.tmdb_original_title = tmdb_info.get("original_title")
        self.tmdb_overview = tmdb_info.get("overview")
        self.tmdb_popularity = tmdb_info.get("popularity")
        self.tmdb_poster_path = tmdb_info.get("poster_path")
        canonical_year = self.year
        if tmdb_info.get("release_date"):
            self.tmdb_release_date = datetime.strptime(
                tmdb_info.get("release_date"), "%Y-%m-%d"
            )
            canonical_year = self.tmdb_release_date.year

        self.tmdb_revenue = tmdb_info.get("revenue")
        self.tmdb_runtime = tmdb_info.get("runtime")
        self.tmdb_status = tmdb_info.get("status")
        self.tmdb_tagline = tmdb_info.get("tagline")
        self.tmdb_title = tmdb_info.get("title")

        # Rename this movie to TMDB's canonical title and year, unless a
        # different movie record already holds that name: title + year is
        # unique, so renaming onto it would fail the whole commit. The two
        # records end up sharing a tmdb_id, so refreshing either movie will
        # merge them via the existing duplicate-tmdb_id handling.

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
        self.tmdb_video = tmdb_info.get("video")
        self.tmdb_vote_average = tmdb_info.get("vote_average")
        self.tmdb_vote_count = tmdb_info.get("vote_count")
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
        """Fetch from TMDB and apply to the database in one step, for
        callers outside the split refresh pipeline (e.g. review_task
        creating a movie inline)."""

        return self.tmdb_movie_apply(self.tmdb_movie_fetch(tmdb_id))

    def tmdb_movie_clear(self):
        """Detach this film from TMDB: drop the id, every fetched field,
        and every association tmdb_movie_apply creates, then mark the
        record ignored so no refresh path guesses a new id from the title.

        For films TMDB has no record of and never will — a home movie, or
        an id TMDB has since deleted. Title and year are the film's own
        library identity, not TMDB's, and stay untouched.
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
        """Network half of a TMDB TV refresh; see tmdb_movie_fetch."""

        tmdb_info = {}
        if not current_app.config["TMDB_API_KEY"]:
            return None
        tmdb_api_key = current_app.config["TMDB_API_KEY"]
        tmdb_api_url = current_app.config["TMDB_API_URL"]

        # Request only the appended blocks tmdb_tv_apply reads (networks,
        # companies, genres, and seasons arrive in the base payload).
        # aggregate_credits rather than credits: series-wide cast/crew
        # with per-role episode counts, not just the latest season's

        requested_info = "aggregate_credits,external_ids,keywords"
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

            # Episode payloads: the base payload lists the seasons;
            # fetch each one's episode block in appended batches (TMDB
            # caps append_to_response at 20). A failed batch is logged
            # and skipped — the apply side only touches seasons present
            # in the payload, so a miss leaves that season's stored
            # episodes alone instead of deleting them.

            season_numbers = [
                season.get("season_number")
                for season in tmdb_info.get("seasons", [])
                if season.get("season_number") is not None
            ]
            for start in range(0, len(season_numbers), 20):
                batch = season_numbers[start : start + 20]
                appended = ",".join(f"season/{n}" for n in batch)
                try:
                    r = tmdb_get(
                        tmdb_api_url + "/tv/" + str(tmdb_id),
                        params={
                            "api_key": tmdb_api_key,
                            "append_to_response": appended,
                        },
                    )
                    r.raise_for_status()
                except requests.exceptions.RequestException:
                    current_app.logger.warning(
                        f"{self} Season batch '{appended}' failed, skipping"
                    )
                    continue

                season_payload = r.json()
                for n in batch:
                    block = season_payload.get(f"season/{n}")
                    if block:
                        tmdb_info[f"season/{n}"] = block

        return tmdb_info or None

    def tmdb_tv_apply(self, tmdb_info):
        """Database half of a TMDB TV refresh; see tmdb_movie_apply."""

        if not tmdb_info:
            return self

        # Delete any existing records associated with this tv series.
        # Genres and keywords get the same empty-payload guard as
        # tmdb_movie_apply (#251) — TV keyword lists ride in "results"

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

        tmdb_networks = TMDBNetwork.query.all()
        for network in tmdb_networks:
            if network in self.networks:
                self.networks.remove(network)

        tmdb_production_companies = TMDBProductionCompany.query.all()
        for company in tmdb_production_companies:
            if company in self.production_companies:
                self.production_companies.remove(company)

        tmdb_seasons = TMDBSeason.query.all()
        for season in tmdb_seasons:
            if season in self.seasons:
                self.seasons.remove(season)

        # Add fresh new data from TMDB

        if tmdb_info.get("external_ids"):
            external_ids = tmdb_info.get("external_ids")
            self.imdb_id = external_ids.get("imdb_id")
            self.tvdb_id = external_ids.get("tvdb_id")

        self.tmdb_id = tmdb_info.get("id")
        self.tmdb_backdrop_path = tmdb_info.get("backdrop_path")
        if tmdb_info.get("first_air_date"):
            self.tmdb_first_air_date = datetime.strptime(
                tmdb_info.get("first_air_date"), "%Y-%m-%d"
            )

        self.tmdb_homepage = tmdb_info.get("homepage")
        self.tmdb_poster_path = tmdb_info.get("poster_path")
        self.tmdb_in_production = tmdb_info.get("in_production")
        if tmdb_info.get("last_air_date"):
            self.tmdb_last_air_date = datetime.strptime(
                tmdb_info.get("last_air_date"), "%Y-%m-%d"
            )

        self.tmdb_name = tmdb_info.get("name")
        if tmdb_info.get("status") == "Ended":
            self.tmdb_number_of_episodes = tmdb_info.get("number_of_episodes")
            self.tmdb_number_of_seasons = tmdb_info.get("number_of_seasons")

        self.tmdb_original_language = tmdb_info.get("original_language")
        self.tmdb_original_name = tmdb_info.get("original_name")
        self.tmdb_overview = tmdb_info.get("overview")
        self.tmdb_popularity = tmdb_info.get("popularity")
        self.tmdb_poster_path = tmdb_info.get("poster_path")
        self.tmdb_status = tmdb_info.get("status")
        self.tmdb_type = tmdb_info.get("type")
        self.tmdb_vote_average = tmdb_info.get("vote_average")
        self.tmdb_vote_count = tmdb_info.get("vote_count")
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

                # Fields are set on every refresh, not just at creation:
                # the create-only original left episode_count frozen at
                # whatever the season had when first seen (the TV overhaul's census
                # found announcement-time counts years stale)

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

        # Series cast/crew: replace this series' join rows from the
        # aggregate credits, mirroring the movie apply — delete-then-readd,
        # gated on the block's presence so a payload without it can't wipe
        # stored credits. The seen-sets stand in for the movie path's
        # per-row existence queries: after the bulk delete only payload
        # duplicates could collide with the unique constraints. Keys are
        # folded the way utf8mb4_general_ci compares — unaccented,
        # caseless, trailing-space-blind — because TMDB payloads really
        # do carry both 'Self - Bee farmer' and 'Self - Bee Farmer' for
        # one person, distinct to Python but a 1062 duplicate to MySQL.

        def collation_key(*parts):
            return tuple(unidecode(part or "").casefold().strip() for part in parts)

        if tmdb_info.get("aggregate_credits"):
            aggregate = tmdb_info.get("aggregate_credits")
            TVCast.query.filter_by(tv_id=self.id).delete()
            TVCrew.query.filter_by(tv_id=self.id).delete()
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

        # Episode rows: sync tv_episode slots for every season
        # block the fetch delivered. Only fetched seasons are touched —
        # a season absent from the payload keeps its stored rows, so a
        # failed season batch can never mass-delete episodes.

        for key, block in tmdb_info.items():
            if not key.startswith("season/") or not isinstance(block, dict):
                continue

            season_number = block.get("season_number")
            if season_number is None:
                continue

            existing = {
                row.episode: row
                for row in self.episodes.filter_by(season=season_number).all()
            }
            fetched_numbers = set()
            for ep in block.get("episodes") or []:
                episode_number = ep.get("episode_number")
                if episode_number is None:
                    continue

                fetched_numbers.add(episode_number)
                row = existing.get(episode_number)
                if row is None:
                    row = TVEpisode(season=season_number, episode=episode_number)
                    self.episodes.append(row)
                name = ep.get("name")
                row.tmdb_episode_id = ep.get("id")
                row.title = name[:256] if name else None
                row.overview = ep.get("overview") or None
                row.air_date = (
                    datetime.strptime(ep.get("air_date"), "%Y-%m-%d")
                    if ep.get("air_date")
                    else None
                )
                row.runtime = ep.get("runtime")
                row.tmdb_still_path = ep.get("still_path")
                row.tmdb_data_as_of = datetime.now(timezone.utc)

            # A stored slot TMDB no longer lists in this season was
            # renumbered or removed upstream — drop it rather than let
            # it mislabel

            for episode_number, row in existing.items():
                if episode_number not in fetched_numbers:
                    db.session.delete(row)

        return self

    def tmdb_tv_clear(self):
        """Detach this series from TMDB; see tmdb_movie_clear.

        Also drops the stored episode rows: without an id there is
        nothing to refresh them from, and a season list left behind from
        a deleted TMDB entry would go stale forever (#207).
        """

        TVCast.query.filter_by(tv_id=self.id).delete()
        TVCrew.query.filter_by(tv_id=self.id).delete()
        invalidate_people_ranking()

        for episode in self.episodes.all():
            db.session.delete(episode)

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

    # This user's Plex account name, so Plex watches can be attributed to
    # them personally (unmapped watchers still count toward household
    # shopping-cart priority)

    plex_username = db.Column(db.String(64), unique=True)

    # Review-export bookkeeping: when the last Letterboxd CSV export ran
    # and the highest review id it covered, so the default export can emit
    # only entries added or edited since. New rows are detected by id
    # because date_watched can be backdated past the last export

    date_reviews_exported = db.Column(db.DateTime)
    last_export_review_id = db.Column(db.Integer)

    # The Letterboxd account whose RSS feed syncs into this user's diary;
    # empty disables the poll for this user

    letterboxd_username = db.Column(db.String(64))

    # This user's Plex playback device: the Companion address (ip:port)
    # and machine id of the player their play buttons target. Per-user —
    # each household member sends films to their own screen; empty hides
    # the play buttons for this user. Set from the Profile page, which
    # probes the address and fills the machine id itself

    plex_player_address = db.Column(db.String(64))
    plex_player_id = db.Column(db.String(64))

    @property
    def plex_player_configured(self):
        """Whether this user has a playback device to send films to."""

        return bool(self.plex_player_address and self.plex_player_id)

    # This user's Infuse target: the Apple TV's Companion-protocol
    # address (ip:port) and the pyatv credentials from the one-time PIN
    # pairing on the Profile page (#192). Separate from the Plex player
    # fields — Infuse is driven over Apple's Companion protocol, not
    # Plex Companion, and a user may enable either app or both

    infuse_player_address = db.Column(db.String(64))
    infuse_player_credentials = db.Column(db.String(512))

    # Which app the plain play buttons target when BOTH are configured:
    # "plex" or "infuse" (Profile page setting). Ignored while only one
    # app is configured — that one simply wins

    default_player = db.Column(db.String(8))

    @property
    def infuse_player_configured(self):
        """Whether this user has a paired Apple TV to open Infuse on."""

        return bool(self.infuse_player_address and self.infuse_player_credentials)

    @property
    def preferred_player(self):
        """The app a plain (no-choice) play button targets: "plex" or
        "infuse" — the configured one, the chosen default when both
        are, or None with no player at all."""

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

    # Watchlist availability alerts (#156/#230): the nightly digest
    # email is strictly opt-in — it's the only per-user mail besides
    # password resets — and rentals are a further opt-in on top, since
    # a rental costs an extra fee and shouldn't read as "available"
    # unless the user asked for that

    notify_availability = db.Column(
        db.Boolean, nullable=False, default=False, server_default="0"
    )
    notify_rentals = db.Column(
        db.Boolean, nullable=False, default=False, server_default="0"
    )

    # The streaming services this user subscribes to — availability
    # displays are customized per user, never site-wide

    streaming_providers = db.relationship(
        "UserStreamingProvider",
        backref="user",
        lazy="dynamic",
        cascade="all,delete,delete-orphan",
    )

    # Films this user wants to watch — the stage before the shopping list

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
        """True when the password matches the stored hash."""

        return check_password_hash(self.password_hash, password)

    def get_reset_password_token(self, expires_in=600):
        """A signed, short-lived JWT for the password-reset email link."""

        return jwt.encode(
            {"reset_password": self.id, "exp": time() + expires_in},
            current_app.config["SECRET_KEY"],
            algorithm="HS256",
        )

    @staticmethod
    def verify_reset_password_token(token):
        """The User a reset token identifies, or None if invalid or expired."""

        try:
            id = jwt.decode(
                token, current_app.config["SECRET_KEY"], algorithms=["HS256"]
            )["reset_password"]
        except:
            return
        return db.session.get(User, id)

    def get_queue_details(self):
        """Running and queued background jobs for the queue page.

        Merges the import, transcode, and file-operation queues into one
        ordered list with queue positions attached.
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

        # The running banners hold their relative order by when each
        # FILE first began running (Glenn's original banner-ordering ask): a file's
        # work hops queues as it progresses — localization on import,
        # the library copy on file-operation — and each hop is a new
        # job with a new started_at, which used to bounce the banner to
        # the end of the list. The pipeline trail's first_run anchor
        # survives the hops; jobs without a trail sort by their own
        # start, converted to the trail's local wall clock.

        from app.pipeline import first_run

        def first_run_anchor(job):
            """The job's stable sort key among the running banners."""

            anchor = first_run(current_app.redis, job)
            if anchor:
                return anchor
            if job.started_at:
                return job.started_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            return "9999"

        def banner_worthy(job):
            """Whether a running job earns a top-of-page alert. Frame
            pool work never does (Glenn, Aug 27 2026): a per-round
            replacement banner disrupts the game and telegraphs that
            the pool just changed, and even the nightly batch's
            'Extracting a frame from X' names films about to become
            answers. They all still list on the queue page."""

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
                        "enqueued_at": job.enqueued_at,
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
                        "enqueued_at": job.enqueued_at,
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
                        "enqueued_at": job.enqueued_at,
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

        # Create list of all localizations and transcodes in queue

        details["all"] = []
        for job_id in imports_running:
            job = current_app.import_queue.fetch_job(job_id)
            if job:
                details["all"].append(
                    {
                        "id": job.id,
                        "status": job.get_status(),
                        "enqueued_at": job.enqueued_at,
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
                        "enqueued_at": job.enqueued_at,
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
                        "enqueued_at": job.enqueued_at,
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
                        "enqueued_at": job.enqueued_at,
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
                        "enqueued_at": job.enqueued_at,
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
                        "enqueued_at": job.enqueued_at,
                        "started_at": job.started_at,
                        "ended_at": job.ended_at,
                        "description": job.meta.get("description", job.description),
                    }
                )

        # Deferred retries (a file still copying in, or its title
        # locked) sit in each queue's ScheduledJobRegistry rather than
        # the queue itself, so they used to be invisible here and only
        # showed as amber chips on the File Activity page's in-flight
        # list. Since the trail chips moved onto the queue rows (Glenn,
        # Aug 2026) the queue page is the one place to see everything
        # in flight, so they list too — after the live queue, with no
        # position, since they aren't in line yet.

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
                            "enqueued_at": job.enqueued_at,
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
        """Total queued plus running jobs, for the navbar badge."""

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
    """One streaming service on a user's profile, from TMDB's
    watch-provider registry (the underlying data is JustWatch's). The
    name and logo are copied at pick time so displays survive registry
    outages."""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    provider_id = db.Column(db.Integer, nullable=False)
    name = db.Column(db.String(64))
    logo_path = db.Column(db.String(64))

    __table_args__ = (db.UniqueConstraint("user_id", "provider_id"),)

    def __repr__(self):
        return f"<UserStreamingProvider '{self.user_id}:{self.name}'>"


class UserWatchlist(db.Model):
    """A film the user wants to watch — the funnel stage before the
    shopping list.

    Adds reuse review-only Movie records, so watchlisted films are
    enriched and first-class everywhere; watching the film (a manual
    log, a Plex scrobble, or a Letterboxd import) removes the entry,
    and graduation to the shopping list then happens organically via
    the likes and ratings the shopping list already reads. Timestamps
    are local wall-clock, like the diary's.
    """

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    movie_id = db.Column(db.Integer, db.ForeignKey("movie.id"), nullable=False)
    date_added = db.Column(db.DateTime, nullable=False, default=datetime.now)

    __table_args__ = (db.UniqueConstraint("user_id", "movie_id"),)

    def __repr__(self):
        return f"<UserWatchlist '{self.user_id}:{self.movie_id}'>"


class UserMovieReview(db.Model):
    """One viewing: a diary/review row from the movie page, a Letterboxd
    import, or a Plex watch.
    """

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    movie_id = db.Column(db.Integer, db.ForeignKey("movie.id"))

    # Rating fields are nullable: a Letterboxd review or like can exist
    # without a star rating

    rating = db.Column(db.Float)
    modified_rating = db.Column(db.Float)
    whole_stars = db.Column(db.Integer)
    half_stars = db.Column(db.Integer)
    review = db.Column(db.Text)
    liked = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    date_watched = db.Column(db.DateTime)
    date_reviewed = db.Column(db.DateTime)

    # When the review text was last edited; date_reviewed keeps the
    # original review date

    date_updated = db.Column(db.DateTime)

    # True for a repeat viewing, False for a first watch, NULL for legacy
    # rows where nobody knows

    rewatch = db.Column(db.Boolean)

    # The Letterboxd feed item this row came from or was matched by
    #: the dedup/edit key, and rows carrying one never re-export
    # to Letterboxd — they are already there

    letterboxd_guid = db.Column(db.String(64), unique=True)

    # Letterboxd's spoiler checkbox, known only for feed-synced rows
    # (the CSV export has no spoiler column, so imports leave it NULL).
    # Stored as data for now; whether it affects review display is a
    # separate, undecided question

    contains_spoilers = db.Column(db.Boolean)

    def __repr__(self):
        return f"<UserMovieReview '{self.user_id}:{self.movie_id}:{self.rating}'>"


class Movie(db.Model, TMDBMixin, Utilities):
    """A film: local identity, TMDB enrichment, Criterion details, and
    shopping-cart state.
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
    tmdb_revenue = db.Column(db.BigInteger)  # BIGINT, thx Titanic (1996) $2,187,463,944
    tmdb_runtime = db.Column(db.Integer)
    tmdb_status = db.Column(db.String(32))
    tmdb_tagline = db.Column(db.String(384))
    tmdb_title = db.Column(db.String(256))
    tmdb_video = db.Column(db.Boolean)
    tmdb_vote_average = db.Column(db.Float)
    tmdb_vote_count = db.Column(db.Integer)
    tmdb_data_as_of = db.Column(db.DateTime)

    # "TMDB has no record of this film, and never will" — a home movie, or
    # an id TMDB has since deleted. Distinct from a plain NULL tmdb_id,
    # which only means "not matched yet" and invites a title search on the
    # next refresh; this flag tells every refresh path to leave the record
    # alone. Cleared by supplying an id by hand.

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
    episodes = db.relationship(
        "TVEpisode",
        backref="series",
        lazy="dynamic",
        cascade="all,delete,delete-orphan",
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


class TVEpisode(db.Model):
    """One TMDB episode of a TV series: the season/episode slot's
    title, overview, air date, runtime, and still.

    Joined from File.season/File.episode at render time; a missing row
    is normal (year-style seasons, custom-numbered specials, series TMDB
    doesn't know) and must surface as today's number-only display, never
    an error. Where File.edition is set, it outranks this title.
    """

    id = db.Column(db.Integer, primary_key=True)
    series_id = db.Column(
        db.Integer,
        db.ForeignKey("tv_series.id", ondelete="CASCADE"),
        nullable=False,
    )
    season = db.Column(db.Integer, nullable=False)
    episode = db.Column(db.Integer, nullable=False)
    tmdb_episode_id = db.Column(db.Integer)
    title = db.Column(db.String(256))
    overview = db.Column(db.Text)
    air_date = db.Column(db.DateTime)
    runtime = db.Column(db.Integer)
    tmdb_still_path = db.Column(db.String(64))
    tmdb_data_as_of = db.Column(db.DateTime)

    # The unique constraint doubles as the lookup index: its leftmost
    # prefixes cover the by-series and by-season queries, so there is no
    # separate series_id index

    __table_args__ = (db.UniqueConstraint("series_id", "season", "episode"),)

    def __repr__(self):
        return f"<TVEpisode {self.series_id} S{self.season:02d}E{self.episode:02d}>"


class File(db.Model):
    """A library media file: location, quality and ranking fields, track
    metadata relations, and AWS archive state.
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

    # The Dolby Vision flavor parsed from hdr_format: "5", "7",
    # "8.1", "8.4", … — profile 8's suffix is the cross-compatibility
    # target. Feeds the eventual P7→8.1 conversion targeting

    dolby_vision_profile = db.Column(db.String(8))
    video_bitrate_kbps = db.Column(db.Integer)
    filesize_bytes = db.Column(db.BigInteger)
    date_added = db.Column(
        db.DateTime, nullable=False, index=True, default=db.func.utc_timestamp()
    )
    date_updated = db.Column(db.DateTime, index=True)
    date_localized = db.Column(db.DateTime, index=True)
    date_transcoded = db.Column(db.DateTime, index=True)

    # When the subtitle triage marked this file's tracks as reviewed
    # ("nothing forced here"). Lives on the file rather than the track
    # rows because rescans delete and rebuild those.

    subtitle_triage_reviewed = db.Column(db.DateTime)

    # When the runtime triage (#234) accepted this file's length as
    # known-benign — a full-disc rip, a short recorded into a longer
    # broadcast slot — so it stops reappearing on the candidates list.
    # Reset on re-import: a replacement's length is new evidence.

    runtime_mismatch_reviewed = db.Column(db.DateTime)
    aws_untouched_key = db.Column(db.String(255), index=True)
    aws_untouched_filesize_bytes = db.Column(db.BigInteger)
    aws_untouched_date_uploaded = db.Column(db.DateTime)
    aws_untouched_date_deleted = db.Column(db.DateTime)

    # Set when an archive upload was lost and the S3 object is known to
    # be older than the local file. Nothing else can tell: the key still
    # exists and the date is the old upload's, so every existing check
    # reads the row as consistent. Cleared by a successful upload.

    aws_untouched_stale = db.Column(
        db.Boolean, nullable=False, server_default="0", default=False
    )
    subtrack = db.relationship(
        "FileSubtitleTrack", backref="file", lazy="select", cascade="all,delete"
    )
    audiotrack = db.relationship(
        "FileAudioTrack", backref="file", lazy="select", cascade="all,delete"
    )

    # Derived copies: the Handbrake transcodes made FROM this
    # file. Rows cascade away with their source; the physical purge is
    # the delete sites' job (see app.transcodes)

    derived_files = db.relationship(
        "DerivedFile",
        backref="source_file",
        lazy="select",
        cascade="all,delete-orphan",
        passive_deletes=True,
    )
    custom_poster = db.Column(db.String(64))

    # Keys that aren't mapped columns, but that the import pipeline includes in
    # the file_details dicts it constructs File objects from; they're kept as
    # plain attributes because find_better_files() reads them when comparing an
    # incoming file against the existing library

    IMPORT_EXTRA_KEYS = {
        "title",
        "year",
        "quality_title",
        "extension",
        "feature_type_name",
        # The TMDB id a filename's Plex id tag resolved to (#155); read by
        # finalize_localization to route the metadata fetch
        "tmdb_id",
    }

    def __repr__(self):
        return f"<File '{self.plex_title}'>"

    def __init__(self, **kwargs):
        # Route mapped columns/relationships through the real SQLAlchemy
        # constructor so instrumentation and validation apply; allow only the
        # known import-pipeline extras as plain attributes, and reject anything
        # else so a typo'd column name fails fast instead of vanishing

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
        """Delete the library copy and any transcoded sibling.

        Missing files aren't an error, and with delete_directory_tree the
        emptied folders are purged too.
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

        # Delete transcoded file and its directory if they exist

        try:
            os.remove(transcoded_file)
        except FileNotFoundError:
            pass

        try:
            os.removedirs(os.path.dirname(transcoded_file))
        except OSError:
            pass

        # Triage snapshots are only useful while the local copy exists

        from app.triage import remove_triage_snapshots

        remove_triage_snapshots(self.id)

        # Optionally delete the directory tree, even if the library file itself
        # was already gone, so deleting a record purges its empty folder too

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
        """The JSON identity used as this file's cross-task lock key:
        title/year/feature for movies, series/season/episode for TV.
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
        # Dear Future Glenn:
        #
        # This place is a message, and part of a system of messages - pay attention to it!
        #
        # Much of the logic of this method was written in a sleepless haze, so I'm not
        # sure it works 100% correctly, but you should also be hesitant to improve it
        # because it's easy to screw it up or have other unintended effects, especially
        # when you start poking around with special features, different versions
        # (e.g. Director's Cut), multi-episode tv show files, etc.
        #
        # It's also difficult because the logic is backwards from what you'd expect:
        # we're trying to find any files that are better than what we're importing,
        # and so we want to proceed only if we *don't* find any matches.
        #
        # What I WANT to do is:
        #
        # Movies:
        #
        # - If the existing file is a full screen version, always replace it with a
        #   non-full screen version, even if it means downgrading the quality
        #
        # - If the existing file is a non-full screen version, and the new file is
        #   full screen, always reject the new full screen file
        #
        # - If the existing and new files are both non-full screen versions, replace
        #   the existing file only if the new one is of same or better quality
        #
        # TV Shows:
        #
        # - If the existing file is a full screen version, always replace it with a
        #   non-full screen version, even if it means downgrading the quality and/or
        #   losing episodes from a multi-episode file
        #
        # - If the existing file is non-full screen version, and the new file is
        #   full screen, always reject the new full screen file
        #
        # - If the existing and new files are both non-full screen versions, replace
        #   the existing file only if the new one is of same or better quality, and if
        #   the new one contains as many or more episodes
        #
        # Just don't ever import full screen versions of movies or tv shows and you
        # should be ok. Not that you'd even want full screen versions in the first place.
        #
        # tl;dr: This place is not a place of honor, etc., etc.
        #
        # xoxo,
        # Past Glenn
        # 2020-08-02

        """Same-title files that outrank this one."""

        better_files = []

        source_quality = RefQuality.query.filter_by(
            quality_title=self.quality_title
        ).first()

        current_app.logger.debug(f"Import vars: {vars(self)}")

        if self.media_library == "Movies":
            # If the new file is a full screen version, better quality files that would
            # prevent this file from importing would be:
            # - the exact same movie, also full screen, in a better quality
            # - the exact same movie, NOT full screen, in the same or better quality

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

            # Otherwise, if the file is not full screen, better quality files that would
            # prevent this file from importing would be:
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
            # A TV episode's identity is series + season + episode span —
            # NEVER the edition: for TV files the edition holds the
            # filename's episode-title segment, and two releases of
            # the same episode can title it differently (Glenn's Seeds of
            # Doom case: a Blu-ray special failed to replace its DVD
            # predecessor because the discs named the extra differently).
            #
            # If the new file is a full screen version, existing files that
            # would prevent this file from importing would be:
            # - same tv episode range, also full screen, in same quality
            # - wider tv episode range, also full screen, in same or better quality
            # - same tv episode range, NOT full screen, in same quality
            # - wider tv episode range, NOT full screen, in same or better quality

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

            # Otherwise, if the file is not full screen, better quality files that would
            # prevent this file from importing would be:
            # - same tv show episode range, not full screen, in a better quality
            # - wider tv show episode range, not full screen, same quality

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
        """Same-title files this one outranks — the pruning candidates after
        an upgrade.
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
            # Edition deliberately absent: for TV files it
            # holds the filename's episode-title segment, and two releases
            # of the same episode can title it differently — the episode
            # span is the identity, so a retitled upgrade still prunes its
            # predecessor

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
    """A file derived from a library original — today the
    Handbrake transcodes under TRANSCODES_DIR, eventually the 4K→SDR
    and Dolby Vision conversions.

    Deliberately NOT a File row: File.file_path is LIBRARY_DIR-relative
    and unique, and every ranking, shopping, and import-replace query
    treats File rows as originals — putting derived copies there would
    demand a never-forget filter at every one of those sites, and a
    missed filter aims deletions at the wrong root. Their own table
    keeps them structurally invisible to all of it, while the
    source_file_id link gives the movie/file pages and the linked
    delete everything they need. file_path here is relative to
    TRANSCODES_DIR."""

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
        """The derived copy's path, marked by kind."""

        return f"<DerivedFile {self.kind} '{self.file_path}'>"


class FileAudioTrack(db.Model):
    """One audio track from a file's MediaInfo scan."""

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
    """One subtitle track from a file's MediaInfo scan."""

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
    """Lookup table of special-feature types (Trailers, Featurettes, ...)."""

    id = db.Column(db.Integer, primary_key=True)
    feature_type = db.Column(db.String(32), nullable=False, unique=True)
    files = db.relationship(
        "File", backref="feature_type", lazy="dynamic", cascade="all,delete"
    )

    def __repr__(self):
        return f"<RefFeatureType '{self.feature_type}'>"


class RefQuality(db.Model):
    """Lookup table of quality tiers ordered by preference, including the
    virtual bottom tier 'Not in library'.
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
    """Lookup table of per-country TMDB certifications (G, PG-13, ...)."""

    id = db.Column(db.Integer, primary_key=True)
    country = db.Column(db.String(8))
    certification = db.Column(db.String(32))
    meaning = db.Column(db.Text)
    order = db.Column(db.Integer)

    __table_args__ = (db.UniqueConstraint("country", "certification"),)

    def __repr__(self):
        return f"<RefCertification '{self.country} - {self.certification}'>"


class TMDBMovieCollection(db.Model, TMDBMixin):
    """A TMDB collection a movie belongs to."""

    id = db.Column(db.Integer, primary_key=True)
    tmdb_backdrop_path = db.Column(db.String(64))
    name = db.Column(db.String(128))
    tmdb_poster_path = db.Column(db.String(64))

    def __repr__(self):
        return f"<TMDBMovieCollection '{self.name}'>"


class TMDBCredit(db.Model, TMDBMixin):
    """A person from TMDB credits, shared by the cast and crew join rows."""

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
    """Join row: a credit's acting role on a movie."""

    id = db.Column(db.Integer, primary_key=True)
    movie_id = db.Column(db.Integer, db.ForeignKey("movie.id"))
    credit_id = db.Column(db.Integer, db.ForeignKey("tmdb_credit.id"))
    character = db.Column(db.String(512))
    billing_order = db.Column(db.Integer)

    __table_args__ = (db.UniqueConstraint("movie_id", "credit_id", "character"),)

    def __repr__(self):
        return f"<MovieCast '{self.movie_id}:{self.credit_id}:{self.character}'>"


class MovieCrew(db.Model):
    """Join row: a credit's crew role on a movie."""

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

    Rows are current-truth: the weekly refresh replaces a film's rows
    wholesale, so absence means Wikidata lists nothing (or the film has
    no Wikidata item) — coverage is strong for major ceremonies and
    patchy for niche festivals, so surfaces must never imply
    completeness.
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
    """Co-preference similarity between two films, from MovieLens.

    Adjusted-cosine similarity over ML-32M's 32 million ratings, with
    co-rater shrinkage — "people who loved A disproportionately loved
    B", the taste signal content features can't see. Keyed by TMDB ids
    (portable across record merges), positive similarities only, both
    directions stored so anchor-side lookups are one indexed query.
    Rebuilt only when a new MovieLens snapshot is adopted, via `flask
    recs copref` (which needs numpy/scipy installed ad hoc — they are
    deliberately not runtime dependencies).
    """

    id = db.Column(db.Integer, primary_key=True)
    tmdb_id_a = db.Column(db.Integer, index=True, nullable=False)
    tmdb_id_b = db.Column(db.Integer, nullable=False)
    similarity = db.Column(db.Float, nullable=False)

    __table_args__ = (db.UniqueConstraint("tmdb_id_a", "tmdb_id_b"),)

    def __repr__(self):
        return f"<MovieCopref '{self.tmdb_id_a}~{self.tmdb_id_b}:{self.similarity}'>"


class CatalogExclusion(db.Model):
    """A TMDB id the catalog loaders must never auto-create again.

    Wikidata's Criterion spine set occasionally lists a film that
    doesn't really exist — an unfinished work carrying a stale TMDB id
    (Eisenstein's Ivan the Terrible Part III was the first found).
    Deleting the bogus Movie record isn't enough, since the next full
    refresh would recreate it; `flask catalog exclude` deletes the
    record AND stores its id here, and both the catalog-record creation
    pass and the Criterion catalog page skip excluded ids thereafter.
    """

    id = db.Column(db.Integer, primary_key=True)
    tmdb_id = db.Column(db.Integer, unique=True, nullable=False)
    title = db.Column(db.String(256))
    date_added = db.Column(db.DateTime, default=datetime.now)

    def __repr__(self):
        return f"<CatalogExclusion '{self.tmdb_id}:{self.title}'>"


class UserMovieStatus(db.Model):
    """Per-user standing flags on films, one row per (user, film, kind).

    Kinds: "unseen" — the rating drive's "haven't seen it", permanently
    out of the drive (previously a Redis-only set, the one user-authored
    data a cache flush could lose); "not_interested" — per-user
    suppression from every recommendation surface, chiefly for unowned
    films, since the rating ladder's zero stars already covers owned
    ones with a real diary row. Not-interested feeds the taste profile
    as a mild negative; either flag clears by deleting its row.
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
    """Name That Frame standings, one row per (user, difficulty):
    the running streak and the personal best — persisted here so a
    restart, another device, or a new session can't erase a high
    score the way the original session-cookie streaks could."""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("user.id"), index=True, nullable=False
    )
    difficulty = db.Column(db.String(16), nullable=False)
    current_streak = db.Column(db.Integer, nullable=False, default=0)
    best_streak = db.Column(db.Integer, nullable=False, default=0)
    date_best = db.Column(db.DateTime)
    # Extra Difficult's running total (#202): 3/2/1 points by how
    # early in the zoom-out the guess landed
    points = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    # Win rate (Glenn's ask, Aug 27 2026): every dealt frame counts as
    # seen — skipped and abandoned rounds included — and only a
    # correct guess counts as won
    rounds_seen = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    rounds_won = db.Column(db.Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (db.UniqueConstraint("user_id", "difficulty"),)

    def __repr__(self):
        return f"<UserFrameScore '{self.user_id}:{self.difficulty}:{self.best_streak}'>"


class TVCast(db.Model):
    """Join row: a credit's acting role on a TV series, from TMDB's
    aggregate credits — one row per distinct character, with the
    series-wide billing order and how many episodes the role spans."""

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
    """Join row: a credit's crew role on a TV series, from TMDB's
    aggregate credits — one row per distinct job."""

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
    """flask-login's user loader."""

    return db.session.get(User, int(id))


def movie_file_rank():
    """Rank each movie file within its title/feature/edition group by quality."""

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
    """Rank each episode file within its series/season/episode group.

    Edition is deliberately not part of the grouping: for TV files it just
    carries the optional episode title, so two copies of the same episode
    compete whether or not their filenames included one.
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
