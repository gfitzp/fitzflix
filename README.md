# fitzflix
A media library manager. Fitzflix was created by Glenn Fitzpatrick so he would know what was in his family's library when browsing for movies at thrift shops, and to keep track of his movie reviews.

<img width="1208" alt="Screen Shot 2022-05-31 at 11 50 36 AM" src="https://user-images.githubusercontent.com/10539597/171218753-2616f91e-677a-483b-bceb-03048b372df3.png">

Fitzflix takes video files for movies and TV shows, uploads to AWS S3 Glacier Deep-Archive storage for backup, sorts them into a Plex-compatible folder hierarchy, removes non-native languages and subtitles to save space, and lets you easily see what movies and TV shows you have in your library and in what formats to help upgrade their quality.

Files named like these…

<img width="602" alt="Screen Shot 2022-05-31 at 11 59 46 AM" src="https://user-images.githubusercontent.com/10539597/171218705-b31a6263-0fc2-489e-8f9f-efdc3f00fae3.png">

…become sorted like so…

<img width="358" alt="Screen Shot 2022-05-31 at 12 05 56 PM" src="https://user-images.githubusercontent.com/10539597/171219194-941736ed-95e2-4dd5-889d-07de0323c4a7.png">

…and are displayed in the application as…

<img width="1219" alt="Screen Shot 2022-05-31 at 11 55 06 AM" src="https://user-images.githubusercontent.com/10539597/171219305-080c44a5-7455-42d0-8dd5-119fbbf1bd36.png">

<img width="1215" alt="Screen Shot 2022-05-31 at 12 15 25 PM" src="https://user-images.githubusercontent.com/10539597/171221742-e41c84d5-3c3b-47a0-9847-16cdfd65d8b4.png">

…and show associated information from TMDb:

<img width="1208" alt="Screen Shot 2022-05-31 at 11 53 15 AM" src="https://user-images.githubusercontent.com/10539597/171219470-d5d819a0-aa6e-4dc7-a09e-3aa97881936a.png">

It supports reviewing films to help keep track of what you've seen:

<img width="1206" alt="Screen Shot 2022-05-31 at 11 56 40 AM" src="https://user-images.githubusercontent.com/10539597/171219852-9de3c5de-863f-4c9a-b88f-c844186e57ca.png">

It also supports TV shows:

<img width="1204" alt="Screen Shot 2022-05-31 at 11 56 13 AM" src="https://user-images.githubusercontent.com/10539597/171219677-f56fa57b-e55b-4dc1-974e-ddfec5a40f69.png">

And makes a great shopping list for searching for films that aren't as good as they could be (e.g. finding non-fullscreen versions of films, upgrading from DVD to Blu-Ray, etc.):

<img width="1203" alt="Screen Shot 2022-05-31 at 11 55 31 AM" src="https://user-images.githubusercontent.com/10539597/171219618-695489d4-adc7-4af5-97b2-90c47a74e223.png">


## How to use

Drop video files into the import directory (`IMPORT_DIR`, by default `../fitzflix/import` relative to the application). Fitzflix watches that directory and automatically processes each file it finds: it parses the filename to identify the movie or TV episode, looks up the canonical title on TMDb, strips non-native-language audio and subtitle tracks, sorts the file into a Plex-compatible folder hierarchy under the library directory, and (if configured) uploads the original to AWS S3 for archival.

### Naming movies

```
Title (Year) - [Quality].ext
```

The quality tag must be one of the known quality titles (e.g. `SDTV`, `DVD`, `WEBDL-480p`, `HDTV-720p`, `WEBRip-1080p`, `Bluray-1080p`, `Bluray-2160p Remux`, etc.); files with an unrecognized quality are rejected. A `{edition-...}` tag marks an alternate cut, and a version string between the title and quality can mark a full screen version:

| Input filename | Sorted into library as |
| --- | --- |
| `Jaws (1975) - [Bluray-1080p].mkv` | `Movies/Jaws (1975)/Jaws (1975) - [Bluray-1080p].mkv` |
| `Blade Runner (1982) {edition-Final Cut} - [Bluray-2160p].mkv` | `Movies/Blade Runner (1982) {edition-Final Cut}/Blade Runner (1982) {edition-Final Cut} - [Bluray-2160p].mkv` |
| `The Terminator (1984) - Fullscreen [DVD].mkv` | `Movies/The Terminator (1984)/The Terminator (1984) - Full Screen [DVD].mkv` |

### Naming movie special features

Adding a special feature type and name after the title files the video in a special-feature folder inside the movie's directory, named so Plex displays it as an extra. The supported feature types are `Behind The Scenes`, `Deleted Scenes`, `Featurettes`, `Interviews`, `Scenes`, `Shorts`, `Trailers`, and `Other`:

| Input filename | Sorted into library as |
| --- | --- |
| `Jaws (1975) - Behind The Scenes - The Making of Jaws [DVD].mkv` | `Movies/Jaws (1975)/Behind The Scenes/The Making of Jaws.mkv` |
| `Jaws (1975) - Deleted Scenes - Alternate Ending [DVD].mkv` | `Movies/Jaws (1975)/Deleted Scenes/Alternate Ending.mkv` |
| `Jaws (1975) - Featurettes - From the Set [DVD].mkv` | `Movies/Jaws (1975)/Featurettes/From the Set.mkv` |
| `Jaws (1975) - Interviews - A Conversation with Steven Spielberg [DVD].mkv` | `Movies/Jaws (1975)/Interviews/A Conversation with Steven Spielberg.mkv` |
| `Jaws (1975) - Scenes - Opening Scene [DVD].mkv` | `Movies/Jaws (1975)/Scenes/Opening Scene.mkv` |
| `Jaws (1975) - Shorts - The Shark Is Not Working [DVD].mkv` | `Movies/Jaws (1975)/Shorts/The Shark Is Not Working.mkv` |
| `Jaws (1975) - Trailers - Theatrical Trailer [DVD].mkv` | `Movies/Jaws (1975)/Trailers/Theatrical Trailer.mkv` |
| `Jaws (1975) - Other - Storyboard Gallery [DVD].mkv` | `Movies/Jaws (1975)/Other/Storyboard Gallery.mkv` |

### Naming TV shows

```
Series Title - SxxEyy - Optional Episode Title [Quality].ext
```

Season `00` marks a special, which is filed into the show's `Specials` folder:

| Input filename | Sorted into library as |
| --- | --- |
| `Doctor Who (2005) - S01E01 - [DVD].mkv` | `TV Shows/Doctor Who (2005)/Season 01/Doctor Who (2005) - S01E01 - [DVD].mkv` |
| `Doctor Who (2005) - S00E01 - The Christmas Invasion [HDTV-1080p].mkv` | `TV Shows/Doctor Who (2005)/Specials/Doctor Who (2005) - S00E01 - The Christmas Invasion [HDTV-1080p].mkv` |
| `Planet Earth - S01E05-E06 - [Bluray-1080p].mkv` | `TV Shows/Planet Earth/Season 01/Planet Earth - S01E05-E06 - [Bluray-1080p].mkv` |

### After import

Fitzflix tracks every file's quality, so the library pages show the best copy you own of each title, and the shopping list pages show which titles could be upgraded (e.g. a full screen DVD that could be replaced with a widescreen Blu-ray). Movies are matched to TMDb for artwork, cast and crew, and review tracking; the file detail page lets you set default audio/subtitle tracks, strip unwanted tracks, transcode with HandBrake, and manage the AWS archive copy.


## Importing from Sonarr and Radarr

Fitzflix can sit downstream of Sonarr and Radarr, importing every file they download. In Sonarr/Radarr, add a **Webhook** connection (Settings → Connect → Webhook) pointed at Fitzflix:

- URL: `http://<fitzflix host>:8000/api/sonarr/add` (Sonarr) or `http://<fitzflix host>:8000/api/radarr/add` (Radarr)
- Method: `POST`, triggered **On Import** / **On Upgrade**
- Credentials: a Fitzflix account's email and password (HTTP Basic authentication); the connection test in Sonarr/Radarr should succeed once these are set

When a download completes, Fitzflix renames the file with a *downgraded* quality title before importing — physical-media quality names are reserved for files ripped from actual discs, and `Remux` isn't used to label downloads:

| Sonarr/Radarr quality | Imported as |
| --- | --- |
| `DVD` | `WEBDL-480p` |
| `Bluray-480p` | `WEBDL-480p` |
| `Bluray-720p` | `WEBDL-720p` |
| `Bluray-1080p` | `WEBDL-1080p` |
| `Bluray-1080p Remux` | `WEBDL-1080p` |

Downloads with a custom format score below 1600 are labeled `WEBRip` instead of `WEBDL`. The file is imported in place from the download client's folder (not copied to the import directory), TV episodes that aired within the last 14 days jump to the front of the import queue, and Sonarr is asked to rescan the series after the rename so its records stay accurate.

## System requirements

Fitzflix is developed and run on macOS with [Homebrew](https://brew.sh), and the default binary paths point at `/opt/homebrew/bin`; every path below can be overridden in the `.env` file, so any platform that provides these tools should work.

### Core

- **Python 3.14** (any recent Python 3 should work)
- **MySQL** — the application database, connected via `mysql+pymysql://` (set `SQLALCHEMY_DATABASE_URI`); falls back to a local SQLite file if unset
- **Redis** — backs the task queues, scheduler, and file-import locks (set `REDIS_URL`)

### Third-party binaries

| Binary | `.env` setting | Used for |
| --- | --- | --- |
| [MediaInfo](https://mediaarea.net/MediaInfo) (`libmediainfo`) | — (loaded as a library by `pymediainfo`) | Scanning every imported file's video, audio, and subtitle tracks |
| [mkvmerge](https://mkvtoolnix.download) (MKVToolNix) | `MKVMERGE_BIN` | Remuxing Matroska files: stripping non-native-language and empty tracks |
| [mkvpropedit](https://mkvtoolnix.download) (MKVToolNix) | `MKVPROPEDIT_LOCATION` | Editing Matroska properties in place: default/forced track flags, track statistics |
| [HandBrakeCLI](https://handbrake.fr) | `HANDBRAKE_BIN` | Transcoding library files to smaller Plex-friendly versions (see `HANDBRAKE_PRESET` / `HANDBRAKE_PRESET_FILE` for preset info) |
| [ffmpeg](https://ffmpeg.org) | `FFMPEG_BIN` | Video conversion functions |
| [AtomicParsley](https://github.com/wez/atomicparsley) | `ATOMICPARSLEY_BIN` | Stripping embedded metadata from MP4 files during import |

```
brew install mediainfo mkvtoolnix handbrake ffmpeg atomicparsley mysql redis
```

At startup, Fitzflix logs a warning for any configured binary or directory it can't find, so a missing tool will show up in `logs/fitzflix.log`.

### Optional services

- **TMDb API key** (`TMDB_API_KEY`) — canonical titles, artwork, cast/crew, and review metadata
- **AWS S3** (`AWS_BUCKET`, `AWS_ACCESS_KEY`, `AWS_SECRET_KEY`, `AWS_SQS_URL`) — offsite archival of original files in Glacier Deep Archive
- **SMTP server** (`MAIL_SERVER` and related settings) — error notifications and password-reset emails
- **Sonarr** (`SONARR_URL`, `SONARR_API_KEY`)
- **Radarr** (`RADARR_URL`, `RADARR_API_KEY`, `RADARR_PROXY_URL`)
- **supervisorctl** — process management for the web app and workers (see below); everything can also be run manually

## Configuration

Create a `.env` file in the project root; it is read at startup by `config.py`. A minimal working configuration:

```
SECRET_KEY=<long random string>
SQLALCHEMY_DATABASE_URI=mysql+pymysql://fitzflix:<password>@localhost/fitzflix
REDIS_URL=redis://localhost:6379
MEDIA_LOCATION=/path/to/media
TMDB_API_KEY=<your TMDb API key>
```

### Notable settings

| Setting | Purpose |
| --- | --- |
| `MEDIA_LOCATION` | Root of the media folders; `IMPORT_DIR`, `LIBRARY_DIR`, `REJECTS_DIR`, and `TRANSCODES_DIR` default to `import`, `library`, `rejects`, and `transcoded` inside it, and can each be set individually |
| `ISO_639_2_NATIVE_LANGUAGE` | Three-letter language code of *your* native language (default `eng`) — audio and subtitle tracks in other languages are stripped during import (except for foreign-language films) |
| `SERVER_NAME`, `PREFERRED_URL_SCHEME` | Hostname and scheme used when building links in emails |
| `PREVENT_ACCOUNT_CREATION` | Once an admin account exists, disables the registration page |
| `ARCHIVE_ORIGINAL_MEDIA` | Upload each imported original to AWS S3 for archival |
| `AWS_BUCKET`, `AWS_ACCESS_KEY`, `AWS_SECRET_KEY` | Credentials for the archival bucket; all three are required for uploads |
| `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_USE_TLS` | SMTP server for error notifications and password-reset emails, sent from `SERVER_EMAIL` to `ADMIN_EMAIL` (both default to `MAIL_USERNAME`) |
| `HANDBRAKE_PRESET`, `HANDBRAKE_PRESET_FILE`, `HANDBRAKE_EXTENSION` | Transcoding preset name, an optional exported preset file it lives in, and the output container |
| `LOG_FILE`, `LOG_RETENTION_DAYS` | Application log location (default `logs/fitzflix.log`) and how many days of rotated archives to keep (default 14) |
| `*_TASK_TIMEOUT` | Per-queue job timeouts in seconds (`LOCALIZATION_TASK_TIMEOUT`, `SQL_TASK_TIMEOUT`, `UPLOAD_TASK_TIMEOUT`, `TRANSCODE_TASK_TIMEOUT`, `MKVPROPEDIT_TASK_TIMEOUT`) |

On/off settings (`PREVENT_ACCOUNT_CREATION`, `ARCHIVE_ORIGINAL_MEDIA`, `MAIL_USE_TLS`, `IGNORE_ETAGS`, `FORCE_UPLOAD`) are enabled by being present with any value — leave them out of `.env` entirely to disable them. Binary paths are covered under [System requirements](#system-requirements).

## Installation

```
python3 -m venv venv &&
source venv/bin/activate &&
pip install -r requirements.txt &&
pip install gunicorn &&
flask db upgrade
```

### First run

`flask db upgrade` creates the database schema and seeds the reference data (quality titles, special feature types), so imports work immediately. Start the application ([supervisor](#running-via-supervisor) serves it at `http://localhost:8000`; [`flask run`](#flask) at `http://localhost:5000`), browse to it, and register an account — **the first account registered automatically becomes the admin**. Once that's done, set `PREVENT_ACCOUNT_CREATION` in `.env` to disable further registration.

## Running via supervisor

Update `command`, `directory`, and `user` fields in `fitzflix_supervisor.ini` file with installation and user information.

```
brew install supervisor &&
cp fitzflix_supervisor.ini /opt/homebrew/etc/supervisor.d/ &&
brew services start supervisor
```

## Worker queues

Background work is split across six Redis queues; the supervisor config and the manual worker commands below decide how many workers serve each. What each queue does:

| Queue | Handles |
| --- | --- |
| `fitzflix-import` | Importing new files: parsing, stripping non-native tracks, sorting into the library |
| `fitzflix-file-operation` | Per-file operations: S3 uploads and downloads, Matroska property edits |
| `fitzflix-transcode` | HandBrake transcodes (CPU-heavy; usually one worker) |
| `fitzflix-sql` | Database writes and TMDb metadata refreshes — run exactly one worker so they're serialized |
| `fitzflix-user-request` | Jobs triggered from the web UI and CLI: manual scans, S3 sync, SQS polling |
| `fitzflix-maintenance` | Scheduled application upkeep, such as nightly log rotation — one worker |

## Running Manually

### Redis

#### Scheduler

```
source venv/bin/activate &&
rqscheduler
```

#### Workers

Run a max of 1 SQL worker so database operations are properly serialized.

```
source venv/bin/activate &&
rq worker fitzflix-sql
```

Run 1 maintenance worker for scheduled application-maintenance tasks like log rotation:

```
source venv/bin/activate &&
rq worker fitzflix-maintenance
```

Vary the number of following workers according to needs:

```
source venv/bin/activate &&
rq worker fitzflix-user-request
```

```
source venv/bin/activate &&
rq worker fitzflix-import fitzflix-file-operation
```

```
source venv/bin/activate &&
rq worker fitzflix-transcode fitzflix-import fitzflix-file-operation
```

```
source venv/bin/activate &&
rq worker fitzflix-file-operation fitzflix-import
```

### Flask

```
flask run
```

## Command-line tasks

Run from the project root with the venv activated. Each command queues a background job, so the workers must be running for anything to happen:

| Command | What it does |
| --- | --- |
| `flask scan` | Scan the import directory for files to import (the directory is also watched continuously; this forces a scan) |
| `flask sync` | Prune files from AWS S3 storage that are no longer in the library |
| `flask sqs` | Poll AWS SQS for completed Glacier restores and download them |
| `flask refresh tmdb` | Refresh TMDb metadata for every matched movie and TV series |
| `flask refresh tmdb movie <tmdb_id>` / `flask refresh tmdb tv <tmdb_id>` | Refresh TMDb metadata for a single title |
| `flask refresh file <file_id>` | Rescan one file's audio/subtitle track metadata |
| `flask refresh criterion` | Refresh Criterion Collection spine numbers (currently non-functional — its Wikipedia source page no longer exists) |

## Restoring files from AWS

Archived originals live in S3 Glacier Deep Archive, so getting one back is a two-step process:

1. On the file's detail page, request the download — Fitzflix asks AWS to restore the object from Glacier. Restores from Deep Archive typically take hours to complete.
2. When AWS finishes the restore, it posts a notification to the SQS queue (`AWS_SQS_URL`; the S3 bucket must be configured to send its restore-completed event notifications there). Run `flask sqs` to poll the queue — each completed restore found is downloaded back into the library.

## Logs and maintenance

All processes write to a shared log, `logs/fitzflix.log` (configurable via `LOG_FILE`). Configuration problems — missing binaries, unreachable media directories, incomplete AWS or mail settings — are reported there as warnings at startup, so check the log first when something isn't working.

The log rotates automatically every night at midnight: the day's file is gzipped alongside as `fitzflix.log.<date>.gz`, and archives older than `LOG_RETENTION_DAYS` (default 14) are deleted.

### Rejected files

Files that can't be imported — an unparseable filename, an unrecognized quality tag, or a file that fails processing — are moved into a subfolder of `REJECTS_DIR` named for the reason they were rejected, so the folder name tells you what to fix before re-importing. Active and queued jobs can be watched on the `/queue` page.

### Updating

```
git pull &&
venv/bin/pip install -r requirements.txt &&
venv/bin/flask db upgrade &&
supervisorctl restart "fitzflix:*"
```
