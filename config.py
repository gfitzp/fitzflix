import os

from dotenv import load_dotenv

basedir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(basedir, ".env"))

# Skip system proxy detection: on macOS it loads an Objective-C framework,
# which aborts the process when it happens inside a forked gunicorn worker

os.environ.setdefault("no_proxy", "*")


class Config(object):
    # fmt: off

    # Time constants
    ONE_SECOND      = 1
    ONE_MINUTE      = ONE_SECOND * 60
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

    # When the site is served over https, refuse to send session cookies over
    # plain http (e.g. direct LAN requests to the gunicorn port)

    SESSION_COOKIE_SECURE               = PREFERRED_URL_SCHEME == "https"
    REMEMBER_COOKIE_SECURE              = PREFERRED_URL_SCHEME == "https"
    SESSION_COOKIE_SAMESITE             = "Lax"

    # Fitzflix core configuration

    NATIVE_LANGUAGE                     = os.environ.get("ISO_639_2_NATIVE_LANGUAGE") or "eng"
    PREVENT_ACCOUNT_CREATION            = os.environ.get("PREVENT_ACCOUNT_CREATION") is not None
    REDIS_URL                           = os.environ.get("REDIS_URL") or "redis://"
    SECRET_KEY                          = os.environ.get("SECRET_KEY") or "fitzflix-secret"
    SQLALCHEMY_DATABASE_URI             = os.environ.get("SQLALCHEMY_DATABASE_URI") or "sqlite:///" + os.path.join(basedir, "app.db")
    SQLALCHEMY_ENGINE_OPTIONS           = {"pool_pre_ping": True, "pool_recycle": 300}
    SQLALCHEMY_TRACK_MODIFICATIONS      = os.environ.get("SQLALCHEMY_TRACK_MODIFICATIONS") is not None

    # Fitzflix directories
    MEDIA_LOCATION                      = os.environ.get("MEDIA_LOCATION") or os.path.join(basedir, "..", "fitzflix")
    IMPORT_DIR                          = os.environ.get("IMPORT_DIR") or os.path.join(MEDIA_LOCATION, "import")
    REJECTS_DIR                         = os.environ.get("REJECTS_DIR") or os.path.join(MEDIA_LOCATION, "rejects")
    TRANSCODES_DIR                      = os.environ.get("TRANSCODES_DIR") or os.path.join(MEDIA_LOCATION, "transcoded")

    # Local scratch space: localization copies each source here and does its
    # processing against local disk, so sustained tool I/O never runs over SMB
    STAGING_DIR                         = os.environ.get("STAGING_DIR") or os.path.join(MEDIA_LOCATION, "staging")

    LIBRARY_DIR                         = os.environ.get("LIBRARY_DIR") or os.path.join(MEDIA_LOCATION, "library")
    MOVIE_LIBRARY                       = os.environ.get("MOVIE_LIBRARY") or os.path.join(LIBRARY_DIR, "Movies")
    TV_LIBRARY                          = os.environ.get("TV_LIBRARY") or os.path.join(LIBRARY_DIR, "TV Shows")

    # SMB server URL prefix (e.g. smb://user@nas.local) for remounting dead
    # network volumes; when unset, mount problems alert but aren't self-healed
    SMB_URL_PREFIX                      = os.environ.get("SMB_URL_PREFIX") or None

    # Application locations
    ATOMICPARSLEY_BIN                   = os.environ.get("ATOMICPARSLEY_BIN") or "/opt/homebrew/bin/AtomicParsley"
    HANDBRAKE_BIN                       = os.environ.get("HANDBRAKE_BIN") or "/opt/homebrew/bin/HandBrakeCLI"
    MKVMERGE_BIN                        = os.environ.get("MKVMERGE_BIN") or "/opt/homebrew/bin/mkvmerge"
    MKVPROPEDIT_BIN                     = os.environ.get("MKVPROPEDIT_LOCATION") or "/opt/homebrew/bin/mkvpropedit"
    FFMPEG_BIN                          = os.environ.get("FFMPEG_BIN") or "/opt/homebrew/bin/ffmpeg"
    FFPROBE_BIN                         = os.environ.get("FFPROBE_BIN") or "/opt/homebrew/bin/ffprobe"

    # AWS configuration
    ARCHIVE_ORIGINAL_MEDIA              = os.environ.get("ARCHIVE_ORIGINAL_MEDIA") is not None
    AWS_BUCKET                          = os.environ.get("AWS_BUCKET") or None
    AWS_ACCESS_KEY                      = os.environ.get("AWS_ACCESS_KEY") or None
    AWS_SECRET_KEY                      = os.environ.get("AWS_SECRET_KEY") or None
    AWS_UNTOUCHED_PREFIX                = os.environ.get("AWS_UNTOUCHED_PREFIX") or "untouched"
    AWS_BACKUP_PREFIX                   = os.environ.get("AWS_BACKUP_PREFIX") or "backup"
    AWS_CUSTOM_POSTERS_PREFIX           = os.environ.get("AWS_CUSTOM_POSTERS_PREFIX") or "custom-posters"
    IGNORE_ETAGS                        = os.environ.get("IGNORE_ETAGS") is not None
    FORCE_UPLOAD                        = os.environ.get("FORCE_UPLOAD") is not None
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

    # Logging configuration
    LOG_FILE                            = os.environ.get("LOG_FILE") or os.path.join(basedir, "logs", "fitzflix.log")
    LOG_RETENTION_DAYS                  = int(os.environ.get("LOG_RETENTION_DAYS") or 14)

    # Database backup configuration
    MYSQLDUMP_BIN                       = os.environ.get("MYSQLDUMP_BIN") or "/opt/homebrew/bin/mysqldump"
    MYSQL_BIN                           = os.environ.get("MYSQL_BIN") or "/opt/homebrew/bin/mysql"
    DB_BACKUP_DIR                       = os.environ.get("DB_BACKUP_DIR") or os.path.join(basedir, "backups")
    DB_BACKUP_RETENTION_DAYS            = int(os.environ.get("DB_BACKUP_RETENTION_DAYS") or 14)
    BACKUP_PASSPHRASE                   = os.environ.get("BACKUP_PASSPHRASE") or None
    ENV_FILE                            = os.environ.get("ENV_FILE") or os.path.join(basedir, ".env")
    CUSTOM_ARTWORK_DIR                  = os.environ.get("CUSTOM_ARTWORK_DIR") or os.path.join(basedir, "app", "static", "custom")

    # Subtitle-triage inspection aids: static-served but outside the
    # custom-artwork tree, so backups ignore them

    TRIAGE_SNAPSHOT_DIR                 = os.environ.get("TRIAGE_SNAPSHOT_DIR") or os.path.join(basedir, "app", "static", "triage")

    # AWS Glacier restore cost estimation, in USD: a per-object retrieval
    # request fee, a per-GB retrieval fee, and the per-GB transfer-out fee.
    # Adjust to match the current AWS rate card if prices change
    AWS_RESTORE_PER_1K_REQUEST_COST      = float(os.environ.get("AWS_RESTORE_PER_1K_REQUEST_COST") or 0.10)
    AWS_RESTORE_PER_1K_REQUEST_BULK_COST = float(os.environ.get("AWS_RESTORE_PER_1K_REQUEST_BULK_COST") or 0.025)
    AWS_RESTORE_PER_GB_COST              = float(os.environ.get("AWS_RESTORE_PER_GB_COST") or 0.02)
    AWS_RESTORE_PER_GB_BULK_COST         = float(os.environ.get("AWS_RESTORE_PER_GB_BULK_COST") or 0.0025)
    AWS_DOWNLOAD_PER_GB_COST             = float(os.environ.get("AWS_DOWNLOAD_PER_GB_COST") or 0.09)

    # Health monitoring: alert when a volume's free space falls below this,
    # rather than on percent used, since the NAS library volumes are kept
    # nearly full by design
    DISK_ALERT_FREE_GB                  = int(os.environ.get("DISK_ALERT_FREE_GB") or 100)
    SUPERVISORCTL_BIN                   = os.environ.get("SUPERVISORCTL_BIN") or "/opt/homebrew/bin/supervisorctl"

    # Transcoding configuration
    HANDBRAKE_PRESET                    = os.environ.get("HANDBRAKE_PRESET") or "Apple 1080p60 Surround"
    HANDBRAKE_PRESET_FILE               = os.environ.get("HANDBRAKE_PRESET_FILE") or None
    HANDBRAKE_EXTENSION                 = os.environ.get("HANDBRAKE_EXTENSION") or "mp4"

    # Sonarr configuration
    SONARR_API_KEY                      = os.environ.get("SONARR_API_KEY") or None
    SONARR_URL                          = os.environ.get("SONARR_URL") or None

    # Plex configuration: URL + token enable the watch-history poller, the
    # webhook token gates the /api/plex/webhook endpoint
    PLEX_URL                            = os.environ.get("PLEX_URL") or None
    PLEX_TOKEN                          = os.environ.get("PLEX_TOKEN") or None
    PLEX_WEBHOOK_TOKEN                  = os.environ.get("PLEX_WEBHOOK_TOKEN") or None

    # Radarr configuration
    RADARR_API_KEY                      = os.environ.get("RADARR_API_KEY") or None
    RADARR_URL                          = os.environ.get("RADARR_URL") or None
    RADARR_PROXY_URL                    = os.environ.get("RADARR_PROXY_URL") or RADARR_URL

    # TMDb configuration
    TMDB_API_KEY                        = os.environ.get("TMDB_API_KEY") or None
    TMDB_API_URL                        = os.environ.get("TMDB_API_URL") or "https://api.themoviedb.org/3"

    # Combined ceiling for all TMDb API requests across every worker
    # process; TMDb rate-limits at roughly 40-50 requests per second per
    # IP, so stay well below that
    TMDB_REQUESTS_PER_SECOND            = int(os.environ.get("TMDB_REQUESTS_PER_SECOND") or 10)

    # Poster and cast artwork is hotlinked straight from TMDb's image CDN
    TMDB_IMAGE_URL                      = os.environ.get("TMDB_IMAGE_URL") or "https://image.tmdb.org/t/p"

    WIKIDATA_SPARQL_URL                 = os.environ.get("WIKIDATA_SPARQL_URL") or "https://query.wikidata.org/sparql"

    # Task timeouts; if specifying in the .env file, set as number of seconds
    # Note: LOCALIZATION_TASK_TIMEOUT also sets the title-lock TTL protecting
    # the whole localization -> move -> finalize chain, including queue waits
    LOCALIZATION_TASK_TIMEOUT           = int(os.environ.get("LOCALIZATION_TASK_TIMEOUT") or ONE_DAY)
    SQL_TASK_TIMEOUT                    = int(os.environ.get("SQL_TASK_TIMEOUT") or TEN_MINUTES)
    UPLOAD_TASK_TIMEOUT                 = int(os.environ.get("UPLOAD_TASK_TIMEOUT") or SIX_HOURS)
    TRANSCODE_TASK_TIMEOUT              = int(os.environ.get("TRANSCODE_TASK_TIMEOUT") or TWO_DAYS)
    MKVPROPEDIT_TASK_TIMEOUT            = int(os.environ.get("MKVPROPEDIT_TASK_TIMEOUT") or SIX_HOURS)
    # Library copies are LAN-bound (minutes, not hours), and small file-queue
    # jobs like S3 deletes need only a wedge-detector
    MOVE_TASK_TIMEOUT                   = int(os.environ.get("MOVE_TASK_TIMEOUT") or TWO_HOURS)
    FILE_TASK_TIMEOUT                   = int(os.environ.get("FILE_TASK_TIMEOUT") or TEN_MINUTES)

    # File upload settings
    MAX_CONTENT_LENGTH                  = 1024 * 1024 * 10 # ten megabytes

    # fmt: on
