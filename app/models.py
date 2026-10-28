import hashlib
import hmac
import json
import os
import re

from datetime import datetime
from time import time

import jwt

from werkzeug.security import generate_password_hash, check_password_hash
from flask import current_app
from flask_login import UserMixin

from app import db, login

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


# The 2 library names. They are the directory names under LIBRARY_DIR
# and the values of File.media_library

MOVIES = "Movies"
TV_SHOWS = "TV Shows"

# The TMDB fetch and apply half of the models lives in app.tmdb_apply.
# The models below use these 4 names. The other names of that module
# stay importable from app.models through __getattr__ at the end.

from app.tmdb_apply import (  # noqa: E402
    TMDBAssociation,
    TMDBMixin,
    _movie_certifications,
    _tmdb_date,
)


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

    def get_queue_details(self):
        """Return the running and queued background jobs for the queue page."""

        from app.queue_page import queue_details
        from app.redis_guard import REDIS_DOWN_ERRORS, warn_redis_down

        # Each page renders this. A Redis outage gives an empty result,
        # not a failed page (#476).

        try:
            return queue_details()
        except REDIS_DOWN_ERRORS as error:
            warn_redis_down("The queue badge", error)
            return {}

    def get_queue_count(self):
        """Return the total of queued and running jobs for the navbar badge."""

        from app.queue_page import queue_count
        from app.redis_guard import REDIS_DOWN_ERRORS, warn_redis_down

        try:
            return queue_count()
        except REDIS_DOWN_ERRORS as error:
            warn_redis_down("The queue badge", error)
            return 0


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

        return f"{self.tmdb_title or self.title} ({self.display_year})"

    @property
    def display_year(self):
        """Return the year that the site shows for the film (display_year_of)."""

        return display_year_of(self.year, self.tmdb_title, self.tmdb_release_date)

    @property
    def us_certification(self):
        """Return the stored US rating of the film, or None (#365).

        The cataloger stores 1 rating per country (release_certification).
        The movie page and the poster card of a record show this value."""

        return next(
            (
                certification.certification
                for certification in self.certifications
                if certification.country == "US" and certification.certification
            ),
            None,
        )

    def __repr__(self):
        return f"<Movie '{self.title} ({self.year})'>"


def clip_movie_title(title):
    """Return a TMDB title shortened to fit Movie.title (#383, #462).

    MariaDB runs with STRICT_TRANS_TABLES. A longer title stops the
    INSERT with error 1406, and the whole batch of the caller fails. A
    later refresh shortens the title in the same way. Thus, it finds the
    same row."""

    length = Movie.__table__.c.title.type.length
    if title and len(title) > length:
        return title[:length].rstrip()
    return title


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

    @property
    def display_year(self):
        """Return the year of the first TMDB air date of the series, or None."""

        return self.tmdb_first_air_date.year if self.tmdb_first_air_date else None

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

    if media_library == MOVIES:
        identity = {
            "title": title,
            "year": year,
            "feature_type": feature_type,
            "plex_title": plex_title,
            "edition": edition,
        }
    elif media_library == TV_SHOWS:
        identity = {"title": title, "season": season, "episode": episode}
    else:
        raise ValueError(f"unknown media library {media_library!r}")
    return json.dumps(identity)


# The keys of the parsed details of an import that are not File columns.
# The title and the year belong to the Movie or the TVSeries row, the
# quality title to the RefQuality row. tmdb_id routes the metadata fetch
# (#155). replace_existing is the "Import" choice of a held conversion
# (#292). Any other key outside the columns is a mistake.

IMPORT_DETAIL_KEYS = frozenset(
    {
        "title",
        "year",
        "quality_title",
        "extension",
        "feature_type_name",
        "tmdb_id",
        "replace_existing",
    }
)


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

    # The key that the archive moves to after a rename (#369). The move
    # job copies the object, verifies the copy, and only then writes the
    # key to aws_key. Thus, aws_key names a real object during the whole
    # move. A target in transcoded/ is the move of aws_transcoded_key.
    # None when no move is pending.

    aws_key_target = db.Column(db.String(255), index=True)
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

    @classmethod
    def from_details(cls, details):
        """Return a new File with the column values of the parsed details of an import.

        The details also hold keys that are not File columns. The title
        and the year belong to the Movie or the TVSeries row, the quality
        title to the RefQuality row. The import flags stay in the dict.
        The import rules of app.file_ranking read the dict, not a File."""

        mapped = set(db.inspect(cls).attrs.keys())
        unexpected = set(details) - mapped - IMPORT_DETAIL_KEYS
        if unexpected:
            # A misspelled column key must not catalog a file with a
            # blank column. That is how a conversion would pass as a rip.
            raise TypeError(
                f"File.from_details() got unexpected key(s): {sorted(unexpected)}"
            )
        return cls(**{key: value for key, value in details.items() if key in mapped})

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

        if self.media_library == MOVIES:
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

    @property
    def quality_label(self):
        """Return the quality of the track as text, or None if it is unknown.

        A lossless track with a known depth and rate reads "24-bit 48 kHz".
        Another track with a known bitrate reads "640 kbps"."""

        if (
            self.compression_mode == "Lossless"
            and self.bit_depth
            and self.sampling_rate_khz
        ):
            return f"{self.bit_depth}-bit {self.sampling_rate_khz} kHz"
        if self.bitrate_kbps:
            return f"{self.bitrate_kbps} kbps"
        return None

    @property
    def label(self):
        """Return the label of the track in a choice list of the file page."""

        label = f"{self.language_name}: {self.codec} {self.channels}"
        quality = self.quality_label
        return f"{label} ({quality})" if quality else label

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

    @property
    def label(self):
        """Return the label of the track in a choice list of the file page."""

        return f"{self.elements}-element {self.language_name}"

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


def display_year_of(year, tmdb_title, tmdb_release_date):
    """Return the year that the site shows for a film.

    This is the TMDB release year if the TMDB title rules. In all other
    cases, it is the local year. Movie.display_year and the column rows
    of the frame game use this rule."""

    return tmdb_release_date.year if tmdb_title and tmdb_release_date else year


def sort_title(text):
    """Return a title without its leading "The", "A", or "An", for a Python sort.

    This is the rule of title_sort for a title in Python. The article
    test ignores the case."""

    return re.sub(r"^(The|A|An)\s+", "", text or "", flags=re.IGNORECASE)


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


# The re-exports of app.models (#361). The quality ranking lives in
# app.file_ranking and the TMDB fetch and apply in app.tmdb_apply. The
# old imports from app.models stay correct. The import is lazy, because
# those modules import the models of this module.

_REEXPORTS = {
    "app.file_ranking": (
        "MOVIE_BEST_ORDER",
        "TV_BEST_ORDER",
        "movie_file_rank",
        "tv_file_rank",
        "ranked_movie_files",
        "ranked_tv_files",
    ),
    "app.tmdb_apply": (
        "PEOPLE_RANKING_KEY",
        "TMDB_RATE_KEY",
        "TMDBUnavailable",
        "invalidate_people_ranking",
        "pick_rating",
        "tmdb_get",
        "tmdb_json",
        "tmdb_json_or",
        "tmdb_objects",
    ),
}
_REEXPORT_HOMES = {name: home for home, names in _REEXPORTS.items() for name in names}


def __getattr__(name):
    """Return a re-exported name from its home module."""

    if name in _REEXPORT_HOMES:
        from importlib import import_module

        return getattr(import_module(_REEXPORT_HOMES[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
