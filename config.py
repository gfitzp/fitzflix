import os
from urllib.parse import unquote

from dotenv import load_dotenv

basedir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(basedir, ".env"))


def _path_list(raw, default):
    """Parse a colon-separated list of paths, or return the default.

    Each entry is stripped. Thus, a space after a colon does not become
    part of a path that a webhook root check then never matches."""

    paths = [entry.strip() for entry in (raw or "").split(":")]
    return [path for path in paths if path] or default


def _mount_urls(raw):
    """Parse a comma-separated list of share URLs into {share name: URL}.

    The name of a share is the URL-decoded basename of its URL. This is
    the name that macOS gives its mount point under /Volumes. Each share
    has its own full URL. The shares do not share a server prefix, because
    NFS exports do not have one. The same server exports /volume2/Movies
    and /volume3/TV Shows. No single prefix can address both.
    """

    urls = {}
    for url in (raw or "").split(","):
        url = url.strip().rstrip("/")
        if url:
            urls[unquote(url.rsplit("/", 1)[-1])] = url
    return urls


# Do not run the system proxy detection. On macOS it loads an Objective-C
# framework. That load aborts the process if it occurs inside a forked
# gunicorn worker.

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

    # If the site is served over https, do not send session cookies over
    # plain http (for example, direct LAN requests to the gunicorn port).

    SESSION_COOKIE_SECURE               = PREFERRED_URL_SCHEME == "https"
    REMEMBER_COOKIE_SECURE              = PREFERRED_URL_SCHEME == "https"
    SESSION_COOKIE_SAMESITE             = "Lax"
    # The remember cookie of Flask-Login has no SameSite by default. A
    # cross-site POST then authenticates a remembered user again from it.
    REMEMBER_COOKIE_SAMESITE            = "Lax"
    # The signed-in session is the boundary for form tokens. The 1-hour
    # default of Flask-WTF would fail the play buttons on a page left open.
    WTF_CSRF_TIME_LIMIT                 = None

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

    # Local scratch space. Localization copies each source here and does its
    # processing on the local disk. Thus, sustained tool I/O never runs over
    # SMB.
    STAGING_DIR                         = os.environ.get("STAGING_DIR") or os.path.join(MEDIA_LOCATION, "staging")

    LIBRARY_DIR                         = os.environ.get("LIBRARY_DIR") or os.path.join(MEDIA_LOCATION, "library")
    MOVIE_LIBRARY                       = os.environ.get("MOVIE_LIBRARY") or os.path.join(LIBRARY_DIR, "Movies")
    TV_LIBRARY                          = os.environ.get("TV_LIBRARY") or os.path.join(LIBRARY_DIR, "TV Shows")

    # Mount URLs for the network shares, comma-separated (for example,
    # smb://user@nas.local/Movies,nfs://nas.local/volume2/Movies). Fitzflix
    # uses them to remount a dead network volume. A dead share with no URL
    # here causes an alert. Fitzflix does not heal it.
    MOUNT_URLS                          = _mount_urls(os.environ.get("MOUNT_URLS"))

    # Application locations
    ATOMICPARSLEY_BIN                   = os.environ.get("ATOMICPARSLEY_BIN") or "/opt/homebrew/bin/AtomicParsley"
    HANDBRAKE_BIN                       = os.environ.get("HANDBRAKE_BIN") or "/opt/homebrew/bin/HandBrakeCLI"
    MKVMERGE_BIN                        = os.environ.get("MKVMERGE_BIN") or "/opt/homebrew/bin/mkvmerge"
    MKVPROPEDIT_BIN                     = os.environ.get("MKVPROPEDIT_LOCATION") or "/opt/homebrew/bin/mkvpropedit"
    FFMPEG_BIN                          = os.environ.get("FFMPEG_BIN") or "/opt/homebrew/bin/ffmpeg"
    FFPROBE_BIN                         = os.environ.get("FFPROBE_BIN") or "/opt/homebrew/bin/ffprobe"
    MKVEXTRACT_BIN                      = os.environ.get("MKVEXTRACT_BIN") or "/opt/homebrew/bin/mkvextract"
    TRUEHDD_BIN                         = os.environ.get("TRUEHDD_BIN") or "/Users/server/bin/truehdd"

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

    # CloudFront download path for a library rebuild (refer to the README
    # section "Downloading through CloudFront" and to infra/README.md).
    # Off by default. When it is on, each restore download fetches the
    # bytes through a CloudFront distribution with a signed URL, not
    # through S3 egress. The restore requests still use the S3 API.
    AWS_DOWNLOAD_VIA_CDN                = os.environ.get("AWS_DOWNLOAD_VIA_CDN") is not None
    CDN_DOMAIN                          = os.environ.get("CDN_DOMAIN") or None
    CDN_KEY_PAIR_ID                     = os.environ.get("CDN_KEY_PAIR_ID") or None
    CDN_PRIVATE_KEY                     = os.environ.get("CDN_PRIVATE_KEY") or None
    CDN_URL_EXPIRY                      = int(os.environ.get("CDN_URL_EXPIRY") or 3600)

    # MediaConvert (TrueHD Atmos -> E-AC-3 Atmos supplement pipeline)
    AWS_MEDIACONVERT_PREFIX             = os.environ.get("AWS_MEDIACONVERT_PREFIX") or "mediaconvert-scratch"
    MEDIACONVERT_ENDPOINT               = os.environ.get("MEDIACONVERT_ENDPOINT") or "https://mediaconvert.us-east-1.amazonaws.com"
    MEDIACONVERT_REGION                 = os.environ.get("MEDIACONVERT_REGION") or "us-east-1"
    MEDIACONVERT_ROLE_ARN               = os.environ.get("MEDIACONVERT_ROLE_ARN") or "arn:aws:iam::726348906822:role/fitzflix-mediaconvert-role"
    EAC3_ATMOS_BITRATE                  = int(os.environ.get("EAC3_ATMOS_BITRATE") or 1024000)

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

    # Subtitle-triage inspection aids. Fitzflix serves them as static files.
    # They are outside the custom-artwork tree. Thus, backups ignore them.

    TRIAGE_SNAPSHOT_DIR                 = os.environ.get("TRIAGE_SNAPSHOT_DIR") or os.path.join(basedir, "app", "static", "triage")

    # Name That Frame: the nightly pre-extracted frame pool. Fitzflix serves
    # it through an authenticated route, never through the public static
    # path, because the filename of a frame must not show its answer.

    FRAME_POOL_DIR                      = os.environ.get("FRAME_POOL_DIR") or os.path.join(basedir, "app", "frame_pool")
    FRAME_POOL_SIZE                     = int(os.environ.get("FRAME_POOL_SIZE") or 600)
    FRAME_POOL_ROTATE                   = int(os.environ.get("FRAME_POOL_ROTATE") or 60)

    # Easy mode deals only films that the player has rated. Thus, the
    # nightly refresh gives each reviewer at least this many pooled frames
    # from their own diary (limited by the number of their rated films).

    FRAME_POOL_MIN_RATED                = int(os.environ.get("FRAME_POOL_MIN_RATED") or 200)

    # AWS Glacier restore cost estimate, in USD: a per-object retrieval
    # request fee, a per-GB retrieval fee, and the per-GB transfer-out fee.
    # If the prices change, adjust these to the current AWS rate card.
    AWS_RESTORE_PER_1K_REQUEST_COST      = float(os.environ.get("AWS_RESTORE_PER_1K_REQUEST_COST") or 0.10)
    AWS_RESTORE_PER_1K_REQUEST_BULK_COST = float(os.environ.get("AWS_RESTORE_PER_1K_REQUEST_BULK_COST") or 0.025)
    AWS_RESTORE_PER_GB_COST              = float(os.environ.get("AWS_RESTORE_PER_GB_COST") or 0.02)
    AWS_RESTORE_PER_GB_BULK_COST         = float(os.environ.get("AWS_RESTORE_PER_GB_BULK_COST") or 0.0025)
    AWS_DOWNLOAD_PER_GB_COST             = float(os.environ.get("AWS_DOWNLOAD_PER_GB_COST") or 0.09)

    # Health monitoring. Alert when the free space of a volume falls below
    # this value, not on the percent used. The NAS library volumes are
    # almost full by design.
    DISK_ALERT_FREE_GB                  = int(os.environ.get("DISK_ALERT_FREE_GB") or 100)
    SUPERVISORCTL_BIN                   = os.environ.get("SUPERVISORCTL_BIN") or "/opt/homebrew/bin/supervisorctl"

    # Transcoding configuration
    HANDBRAKE_PRESET                    = os.environ.get("HANDBRAKE_PRESET") or "Apple 1080p60 Surround"
    HANDBRAKE_PRESET_FILE               = os.environ.get("HANDBRAKE_PRESET_FILE") or None
    HANDBRAKE_EXTENSION                 = os.environ.get("HANDBRAKE_EXTENSION") or "mp4"

    # Sonarr configuration
    SONARR_API_KEY                      = os.environ.get("SONARR_API_KEY") or None
    SONARR_URL                          = os.environ.get("SONARR_URL") or None
    # The root folders of Sonarr, as this host sees them (colon-separated).
    # The import webhook acts only on the files under one of them.
    SONARR_ROOT_FOLDERS                 = _path_list(os.environ.get("SONARR_ROOT_FOLDERS"), [TV_LIBRARY])

    # Plex configuration. The URL and the token enable the watch-history
    # poller. The webhook token gates the /api/plex/webhook endpoint.
    PLEX_URL                            = os.environ.get("PLEX_URL") or None
    PLEX_TOKEN                          = os.environ.get("PLEX_TOKEN") or None
    PLEX_WEBHOOK_TOKEN                  = os.environ.get("PLEX_WEBHOOK_TOKEN") or None

    # Virtual DVR channels (#182). The token gates the M3U/XMLTV/stream
    # endpoints. If the token is not set, the feature is off and every route
    # returns 404. Plex tunes the playlist as an M3U tuner. The channel count
    # and the channel size limit the ffprobe work of the nightly lineup
    # build. DVR_TUNER_URL is the Fitzflix origin AS PLEX REACHES IT. Use
    # loopback if Plex runs on the same machine. Then tuning never goes
    # through the public host.
    DVR_TOKEN                           = os.environ.get("DVR_TOKEN") or None
    DVR_TUNER_URL                       = os.environ.get("DVR_TUNER_URL") or "http://127.0.0.1:8000"
    DVR_GENRE_CHANNELS                  = int(os.environ.get("DVR_GENRE_CHANNELS") or 6)
    DVR_CHANNEL_FILMS                   = int(os.environ.get("DVR_CHANNEL_FILMS") or 40)
    DVR_TV_CHANNELS                     = int(os.environ.get("DVR_TV_CHANNELS") or 5)
    DVR_CHANNEL_EPISODES                = int(os.environ.get("DVR_CHANNEL_EPISODES") or 60)
    DVR_VIDEO_BITRATE_KBPS              = int(os.environ.get("DVR_VIDEO_BITRATE_KBPS") or 8000)

    # Remote playback through Plex Companion. This is the server address AS
    # THE PLAYERS REACH IT: an https URI that resolves from the networks of
    # the players. It is not PLEX_URL. PLEX_URL is loopback. The player to
    # command is per user (User.plex_player_address and _id, set on the
    # Profile page). GDM discovery cannot cross the DMZ VLAN. Thus, Fitzflix
    # addresses the players directly. It never discovers them.
    PLEX_PLAYER_SERVER_URI              = os.environ.get("PLEX_PLAYER_SERVER_URI") or None

    # Radarr configuration
    RADARR_API_KEY                      = os.environ.get("RADARR_API_KEY") or None
    RADARR_URL                          = os.environ.get("RADARR_URL") or None
    # The root folders of Radarr, as this host sees them (colon-separated).
    RADARR_ROOT_FOLDERS                 = _path_list(os.environ.get("RADARR_ROOT_FOLDERS"), [MOVIE_LIBRARY])
    RADARR_PROXY_URL                    = os.environ.get("RADARR_PROXY_URL") or RADARR_URL

    # TMDB configuration
    TMDB_API_KEY                        = os.environ.get("TMDB_API_KEY") or None
    TMDB_API_URL                        = os.environ.get("TMDB_API_URL") or "https://api.themoviedb.org/3"

    # The combined limit for all TMDB API requests across every worker
    # process. TMDB rate-limits at approximately 40-50 requests per second
    # per IP. Stay well below that rate.
    TMDB_REQUESTS_PER_SECOND            = int(os.environ.get("TMDB_REQUESTS_PER_SECOND") or 10)

    # Fitzflix hotlinks poster and cast artwork directly from the TMDB
    # image CDN.
    TMDB_IMAGE_URL                      = os.environ.get("TMDB_IMAGE_URL") or "https://image.tmdb.org/t/p"

    WIKIDATA_SPARQL_URL                 = os.environ.get("WIKIDATA_SPARQL_URL") or "https://query.wikidata.org/sparql"

    # Task timeouts. In the .env file, set them as a number of seconds.
    # Note: LOCALIZATION_TASK_TIMEOUT also sets the title-lock TTL. That
    # lock protects the whole localization -> move -> finalize chain,
    # including the queue waits.
    LOCALIZATION_TASK_TIMEOUT           = int(os.environ.get("LOCALIZATION_TASK_TIMEOUT") or ONE_DAY)
    SQL_TASK_TIMEOUT                    = int(os.environ.get("SQL_TASK_TIMEOUT") or TEN_MINUTES)
    UPLOAD_TASK_TIMEOUT                 = int(os.environ.get("UPLOAD_TASK_TIMEOUT") or SIX_HOURS)
    TRANSCODE_TASK_TIMEOUT              = int(os.environ.get("TRANSCODE_TASK_TIMEOUT") or TWO_DAYS)
    MKVPROPEDIT_TASK_TIMEOUT            = int(os.environ.get("MKVPROPEDIT_TASK_TIMEOUT") or SIX_HOURS)
    # A library copy runs on the LAN (minutes, not hours). A small file-queue
    # job, for example an S3 delete, needs only a stall detector.
    MOVE_TASK_TIMEOUT                   = int(os.environ.get("MOVE_TASK_TIMEOUT") or TWO_HOURS)
    FILE_TASK_TIMEOUT                   = int(os.environ.get("FILE_TASK_TIMEOUT") or TEN_MINUTES)

    # File upload settings
    MAX_CONTENT_LENGTH                  = 1024 * 1024 * 10 # 10 megabytes

    # fmt: on
