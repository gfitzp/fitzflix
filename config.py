import os

from dotenv import load_dotenv


basedir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(basedir, ".env"))


class Config(object):

    # fmt: off

    # Time constants
    ONE_SECOND      = 1
    ONE_MINUTE      = ONE_SECOND * 60
    FIVE_MINUTES    = ONE_MINUTE * 5
    TEN_MINUTES     = ONE_MINUTE * 10
    ONE_HOUR        = ONE_MINUTE * 60
    TWO_HOURS       = ONE_HOUR * 2
    SIX_HOURS       = ONE_HOUR * 6
    ONE_DAY         = ONE_HOUR * 24
    TWO_DAYS        = ONE_DAY * 2


    # Environmental variables

    PREFERRED_URL_SCHEME                = os.environ.get("PREFERRED_URL_SCHEME") or "http"
    SERVER_NAME                         = os.environ.get("SERVER_NAME") or None
    APPLICATION_ROOT                    = os.environ.get("APPLICATION_ROOT") or "/"

    # Fitzflix core configuration

    NATIVE_LANGUAGE                     = os.environ.get("ISO_639_2_NATIVE_LANGUAGE") or "eng"
    PREVENT_ACCOUNT_CREATION            = os.environ.get("PREVENT_ACCOUNT_CREATION") is not None
    REDIS_URL                           = os.environ.get("REDIS_URL") or "redis://"
    SECRET_KEY                          = os.environ.get("SECRET_KEY") or "fitzflix-secret"
    SQLALCHEMY_DATABASE_URI             = os.environ.get("SQLALCHEMY_DATABASE_URI") or "sqlite:///" + os.path.join(basedir, "app.db")
    SQLALCHEMY_ENGINE_OPTIONS           = {"pool_pre_ping": True, "pool_recycle": 300}
    SQLALCHEMY_TRACK_MODIFICATIONS      = os.environ.get("SQLALCHEMY_TRACK_MODIFICATIONS") is not None

    # Fitzflix directories
    MEDIA_LOCATION                      = os.environ.get("MEDIA_LOCATION") or os.path.join(basedir, "..", "videos")
    IMPORT_DIR                          = os.environ.get("IMPORT_DIR") or os.path.join(MEDIA_LOCATION, "import")
    LIBRARY_DIR                         = os.environ.get("LIBRARY_DIR") or os.path.join(MEDIA_LOCATION, "library")
    REJECTS_DIR                         = os.environ.get("REJECTS_DIR") or os.path.join(MEDIA_LOCATION, "rejects")
    TRANSCODES_DIR                      = os.environ.get("TRANSCODED_DIR") or os.path.join(MEDIA_LOCATION, "transcoded")

    # Application locations
    ATOMICPARSLEY_BIN                   = os.environ.get("ATOMICPARSLEY_BIN") or "/opt/homebrew/bin/AtomicParsley"
    HANDBRAKE_BIN                       = os.environ.get("HANDBRAKE_BIN") or "/opt/homebrew/bin/HandBrakeCLI"
    MKVMERGE_BIN                        = os.environ.get("MKVMERGE_BIN") or "/opt/homebrew/bin/mkvmerge"
    MKVPROPEDIT_BIN                     = os.environ.get("MKVPROPEDIT_LOCATION") or "/opt/homebrew/bin/mkvpropedit"

    # AWS configuration
    ARCHIVE_ORIGINAL_MEDIA              = os.environ.get("ARCHIVE_ORIGINAL_MEDIA") is not None
    AWS_BUCKET                          = os.environ.get("AWS_BUCKET") or None
    AWS_ACCESS_KEY                      = os.environ.get("AWS_ACCESS_KEY") or None
    AWS_SECRET_KEY                      = os.environ.get("AWS_SECRET_KEY") or None
    AWS_UNTOUCHED_PREFIX                = os.environ.get("AWS_UNTOUCHED_PREFIX") or "untouched"
    IGNORE_ETAGS                        = os.environ.get("IGNORE_ETAGS") is not None
    AWS_SQS_URL                         = os.environ.get("AWS_SQS_URL") or None

    # Mail server configuration
    MAIL_USERNAME                       = os.environ.get("MAIL_USERNAME")
    MAIL_PASSWORD                       = os.environ.get("MAIL_PASSWORD")
    MAIL_SERVER                         = os.environ.get("MAIL_SERVER")
    MAIL_PORT                           = int(os.environ.get("MAIL_PORT") or 25)
    MAIL_USE_TLS                        = os.environ.get("MAIL_USE_TLS") is not None
    SERVER_EMAIL                        = os.environ.get("SERVER_EMAIL") or os.environ.get("MAIL_USERNAME")
    ADMIN_EMAIL                         = os.environ.get("ADMIN_EMAIL") or os.environ.get("MAIL_USERNAME")
    TODO_EMAIL                          = os.environ.get("TODO_EMAIL") or None

    # Transcoding configuration
    HANDBRAKE_PRESET                    = os.environ.get("HANDBRAKE_PRESET") or "Apple 1080p60 Surround"
    HANDBRAKE_EXTENSION                 = os.environ.get("HANDBRAKE_EXTENSION") or "m4v"

    # Sonarr configuration
    SONARR_API_KEY                      = os.environ.get("SONARR_API_KEY") or None
    SONARR_URL                          = os.environ.get("SONARR_URL") or None

    # Radarr configuration
    RADARR_API_KEY                      = os.environ.get("RADARR_API_KEY") or None
    RADARR_URL                          = os.environ.get("RADARR_URL") or None
    RADARR_PROXY_URL                    = os.environ.get("RADARR_PROXY_URL") or RADARR_URL

    # TMDb configuration
    TMDB_API_KEY                        = os.environ.get("TMDB_API_KEY") or None
    TMDB_API_URL                        = os.environ.get("TMDB_API_URL") or "https://api.themoviedb.org/3"

    WIKIPEDIA_CRITERION_COLLECTION_URL  = "https://en.wikipedia.org/wiki/List_of_Criterion_Collection_releases"


    # Task timeouts; if specifying in the .env file, set as number of seconds
    LOCALIZATION_TASK_TIMEOUT           = os.environ.get("LOCALIZATION_TASK_TIMEOUT") or ONE_DAY
    SQL_TASK_TIMEOUT                    = os.environ.get("SQL_TASK_TIMEOUT") or TEN_MINUTES
    UPLOAD_TASK_TIMEOUT                 = os.environ.get("UPLOAD_TASK_TIMEOUT") or SIX_HOURS
    TRANSCODE_TASK_TIMEOUT              = os.environ.get("TRANSCODE_TASK_TIMEOUT") or TWO_DAYS
    MKVPROPEDIT_TASK_TIMEOUT            = os.environ.get("MKVPROPEDIT_TASK_TIMEOUT") or SIX_HOURS

    # fmt: on
