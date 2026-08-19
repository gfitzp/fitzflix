# fitzflix
A media library manager. Fitzflix was created by Glenn Fitzpatrick so he would know what was in his family's library when browsing for movies at thrift shops, and to keep track of his movie reviews.

<img width="1208" alt="Screen Shot 2022-05-31 at 11 50 36 AM" src="https://user-images.githubusercontent.com/10539597/171218753-2616f91e-677a-483b-bceb-03048b372df3.png">

Fitzflix takes video files for movies and TV shows, uploads to AWS S3 Glacier Deep-Archive storage for backup, sorts them into a Plex-compatible folder hierarchy, removes non-native languages and subtitles to save space, and lets you easily see what movies and TV shows you have in your library and in what formats to help upgrade their quality. On top of the library it builds a personal discovery layer — a taste profile from your own viewing diary drives nightly-recomputed recommendation shelves, a watchlist, a rating drive, and per-film award and streaming-availability data (see [Discovery](#discovery-the-landing-page-and-the-recommendation-engine)).

Files named like these…

<img width="602" alt="Screen Shot 2022-05-31 at 11 59 46 AM" src="https://user-images.githubusercontent.com/10539597/171218705-b31a6263-0fc2-489e-8f9f-efdc3f00fae3.png">

…become sorted like so…

<img width="358" alt="Screen Shot 2022-05-31 at 12 05 56 PM" src="https://user-images.githubusercontent.com/10539597/171219194-941736ed-95e2-4dd5-889d-07de0323c4a7.png">

…and are displayed in the application as…

<img width="1219" alt="Screen Shot 2022-05-31 at 11 55 06 AM" src="https://user-images.githubusercontent.com/10539597/171219305-080c44a5-7455-42d0-8dd5-119fbbf1bd36.png">

<img width="1215" alt="Screen Shot 2022-05-31 at 12 15 25 PM" src="https://user-images.githubusercontent.com/10539597/171221742-e41c84d5-3c3b-47a0-9847-16cdfd65d8b4.png">

…and show associated information from TMDb:

<img width="1208" alt="Screen Shot 2022-05-31 at 11 53 15 AM" src="https://user-images.githubusercontent.com/10539597/171219470-d5d819a0-aa6e-4dc7-a09e-3aa97881936a.png">

It supports reviewing films to help keep track of what you've seen. Reviews interoperate with [Letterboxd](https://letterboxd.com) in both directions: the My Movie Reviews page imports a Letterboxd account-export zip as-is — combining `diary.csv` (watch dates), `ratings.csv`, `reviews.csv`, `likes/films.csv`, and `watchlist.csv` into review and watchlist records, matching films against the library or TMDb (films you've seen but don't own are created as review-only records), and merging idempotently so re-importing a newer export updates rather than duplicates — and the export button emails a CSV in the [Letterboxd import format](https://letterboxd.com/about/importing-data/), ready to upload to Letterboxd's importer. Exports default to only the entries added or edited since the last export, with a checkbox for a full export.

Ongoing sync is hands-free: enter a Letterboxd username on the Profile page and Fitzflix polls that account's public RSS feed twice an hour, merging each diary entry or review into the local diary by its feed id — new watches add rows, edited reviews update in place, and a bare Plex-recorded watch of the same film is *completed* with the Letterboxd verdict (rating, review text, like, rewatch flag, spoiler flag) rather than duplicated, so one viewing stays one row no matter how many systems report it. Reviews keep Letterboxd's inline formatting (`<i>`, `<b>`, and friends render as formatting, never as visible tags), likes are stored verbatim (a sub-three-star guilty pleasure keeps its heart), and feed-synced rows are excluded from the CSV export so the two directions never ping-pong. The CSV import remains the backfill path for history older than the feed's ~50-item window.

<img width="1206" alt="Screen Shot 2022-05-31 at 11 56 40 AM" src="https://user-images.githubusercontent.com/10539597/171219852-9de3c5de-863f-4c9a-b88f-c844186e57ca.png">

It also supports TV shows:

<img width="1204" alt="Screen Shot 2022-05-31 at 11 56 13 AM" src="https://user-images.githubusercontent.com/10539597/171219677-f56fa57b-e55b-4dc1-974e-ddfec5a40f69.png">

And makes a great shopping list for searching for films that aren't as good as they could be (e.g. finding non-fullscreen versions of films, upgrading from DVD to Blu-Ray, etc.):

<img width="1203" alt="Screen Shot 2022-05-31 at 11 55 31 AM" src="https://user-images.githubusercontent.com/10539597/171219618-695489d4-adc7-4af5-97b2-90c47a74e223.png">


## How to use

Drop video files into the import directory (`IMPORT_DIR`, by default `../fitzflix/import` relative to the application). Fitzflix watches that directory and automatically processes each file it finds: it parses the filename to identify the movie or TV episode, looks up the canonical title on TMDb, strips non-native-language audio and subtitle tracks, sorts the file into a Plex-compatible folder hierarchy under the library directory, and (if configured) uploads the original to AWS S3 for archival.

Before importing, Fitzflix confirms the file is completely copied: a file whose size is still changing waits, a Matroska or MP4 file whose container reports truncation (a partial copy, even a stalled one) waits until it's structurally complete, and formats that can't be probed wait until they haven't been modified for a couple of minutes. When copying files in manually over a slow or unreliable connection, the most reliable approach is still to copy to a temporary name the importer ignores — a dot-prefixed name like `.Movie (2021) - [Bluray-1080p].mkv` or a non-video extension like `.partial` — and rename it into place once the copy finishes; the watcher and the hourly sweep both skip dotfiles, so the rename is what makes the file visible, and a half-copied file can never be picked up.

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

Fitzflix tracks every file's quality, so the library pages show the best copy you own of each title, and the shopping list pages show which titles could be upgraded (e.g. a full screen DVD that could be replaced with a widescreen Blu-ray). Movies are matched to TMDb for artwork, cast and crew, and review tracking; the file detail page lets you set default audio/subtitle tracks, strip unwanted tracks, transcode with HandBrake, and manage the AWS archive copy. Track scanning also records each file's video format, bitrate, and HDR format — including the Dolby Vision flavor (profile 5, 7, 8.1, …) parsed from MediaInfo, badged on the file page.

### Using Fitzflix on a phone

Fitzflix follows the system light/dark appearance automatically (Bootstrap 5.3 color modes), and the installed app supports pull-to-refresh — drag down from the top of any page to reload it fresh.

Fitzflix installs as a web app for shopping trips: open it in the phone's browser and use **Add to Home Screen** (iOS Safari) or **Install app** (Android Chrome). The installed app opens to the landing page — the recommendation shelves — in a full-screen window (`start_url` in `app/static/site.webmanifest`), and the search box in the navigation bar is always visible for the "do I own this?" check — search results flag upgrade-candidate seasons and movies in amber. When Fitzflix is served over HTTPS, a service worker also keeps recently-viewed pages available offline, so the shopping list still opens in stores with no reception; over plain HTTP the app still installs and works, but offline caching is disabled (browsers only allow service workers in secure contexts).


## Reviews, the watchlist, and the rating drive

Every film page carries a quick-answer rating ladder — **Not interested** (zero stars) through **Loved it** (five) — plus an optional review text and watch date; a rating of three stars or more automatically flags the film as liked. Until you rate a film, the ladder previews the engine's **estimated rating** for you in paler gold, at full fractional precision (a 3.75 estimate fills the fourth star three-quarters); your own taps stay whole-star, while imported Letterboxd ratings can carry halves and display that way. Rating twice on the same day edits that day's verdict in place rather than logging a phantom rewatch; the newest diary entry's rating is a film's standing verdict everywhere. Log entries default to date-less (Plex watches carry real timestamps; films seen elsewhere usually don't), with the date field there when it's known. Films you haven't got can be logged too, from their TMDb page — a review-only record is created and enriched through the normal TMDb refresh.

Each user also has a **watchlist**: any movie page (owned or not) has an add/remove toggle, the My Watchlist page lists everything with streaming availability per row, and logging a film — by hand, from Plex, or via a Letterboxd import — removes it automatically. Watchlisted films influence the recommendation shelves (below) without blocking them, and feed the taste profile as a mild interest signal.

The **Rate Films** page is a Netflix-style rating drive for seeding taste data: it deals one film at a time from the library, chosen to maximize what each answer reveals about your taste, with the quick-answer ladder plus **Add to watchlist** and **No Opinion** (for films seen but unremembered as much as never seen — out of the drive for two years, and still ratable any time from the movie page). Rating a film positively earns two or three "Since you liked…" suggestions, and the same suggestion strip appears on a movie page right after rating it there.

Every poster on the gallery surfaces — the landing shelves and rails, the leaving-Criterion shelf, and the suggestion strips — carries a **hover card**: hovering (or, on a phone, tapping once; tapping again opens the film's page) pops a compact card with the film's credits, synopsis, availability badges, the live rating ladder (your verdict, or the engine's estimate until you have one), and a watchlist toggle. Everything on the card acts in place without leaving the page, and rating or banking a film from a card never disturbs the rating drive's "Since you liked…" strip — only rating the featured film (or a film on its own page) steers the drive.

Search results, TMDb results, filmographies, and movie pages all wear per-user **funnel badges** along the way: *Might interest you* (taste profile) → *On your watchlist* (intent) → *Seen* (diary).

## Discovery: the landing page and the recommendation engine

The landing page is built around "what should we watch tonight": a **library shelf** of twelve owned films picked from a taste-ranked pool so that nothing repeats within roughly a month, a **Watch it again** shelf of old favorites not seen in two years or more, a **streaming shelf** of films on the services you've picked (see below), and — for Criterion Channel subscribers — an **On Criterion24/7 now** card showing what the Channel's 24/7 feed is airing this minute (scraped from [whatsonnow.criterionchannel.com](https://whatsonnow.criterionchannel.com) by a poller that re-checks right as each film ends; the card carries the TMDb poster and rating ladder on a director-verified match, filmography-linked credits, and Watch Live/More links) plus a **Leaving the Criterion Channel** shelf of the month's departures with a full inventory page behind it. Watchlisted films pin into the shelves (capped, so discovery keeps the majority of the cards), each day's cards shuffle to day-stable positions, and a runtime filter ("only films that fit your evening") trims every shelf at once. Each card says *why* it was picked.

The engine behind it is content-based and deliberately free of ML runtime dependencies: a nightly job (1:45 AM) builds a per-user taste profile from that user's own diary — likes, chosen watches, rewatches, and mean-centered star ratings, spread across genre, decade, language, director, actor, cinematographer, composer, writer, editor, and keyword features with Bayesian shrinkage — and scores every owned, unwatched film against it. Three quality signals ride on top:

- **Awards** — wins and nominations fetched weekly from [Wikidata](https://www.wikidata.org) (film items, plus craft categories like Best Director that Wikidata records on *person* items with a "for work" qualifier). They appear on movie pages and add a capped prior to films the profile already likes; awards alone never recommend a taste mismatch.
- **Co-preference** — "people who loved what you loved also loved this", from the [MovieLens](https://grouplens.org/datasets/movielens/) ML-32M dataset's 32 million ratings: item-to-item similarities are precomputed into the database for every MovieLens film with 50+ raters, so the signal covers films the library hasn't even met yet. Cards driven by it say so ("liked by people who liked …"). Rebuilding the table (only needed when adopting a new MovieLens snapshot) is `flask recs copref <extracted-ml-32m-dir>` and requires `numpy` and `scipy` installed ad hoc — they are build-time tools, deliberately not in `requirements.txt`. The dataset itself is not kept: download it fresh from GroupLens (research/non-commercial license, no redistribution).
- **Watchlist interest** — a small positive weight for wanting a film you haven't watched.

`flask recs evaluate` measures the whole arrangement by leave-one-out ranking over your own diary, and is the gate for engine changes: signals ship only when the metrics improve. (A craft-award person-prior and a mean-derived liked flag were both evaluated this way and rejected on the numbers.)

## Streaming availability

Each user picks their streaming services on their Profile page (any provider TMDb's registry knows). Movie pages, TMDb search results, filmographies, the watchlist, and the streaming shelf then show provider-logo badges for films streamable on *your* services — rentals shown only for unowned films, digital purchase never (buying happens on physical media in this house). Availability data comes from JustWatch via TMDb, day-cached per title, and every surface that shows it carries the required "Streaming data by JustWatch" credit.

## Browsing: people and the Criterion Collection

The **People** page (Library → People) is a browsable grid of everyone credited across the library's films — filterable by cast, crew, or both, defaulting to cast — and every name links to a filmography page showing the person's entire TMDb career with library badges on the films you own. Key crew roles (director, writer, cinematographer, composer, editor) are first-class: they appear in search with dominant-role badges ("Director · 41 films"), and multi-role credit lines read in closing-credits order.

The **Criterion Collection** page lists the entire spine catalog from Wikidata (~1,350 releases), not just the library: owned films show whether the copy is *settled* (disc owned, file matching the release's format) or wearing an amber quality badge that means "go find the Criterion version"; unowned releases are watchlistable and show a Criterion Channel badge when currently streamable; box-set members sort at their set's spine. Filters: all releases / in library / owned & settled. Full Wikidata refreshes also create library records for spine films Fitzflix has never seen, so the whole catalog stays first-class permanently.

## Importing from Sonarr and Radarr

Fitzflix can sit downstream of Sonarr and Radarr, importing every file they download. In Sonarr/Radarr, add a **Webhook** connection (Settings → Connect → Webhook) pointed at Fitzflix:

- URL: `http://<fitzflix host>:8000/api/sonarr/add` (Sonarr) or `http://<fitzflix host>:8000/api/radarr/add` (Radarr)
- Method: `POST`, triggered **On Import** / **On Upgrade**
- Credentials (HTTP Basic authentication): a Fitzflix account's email as the username, and its **API key** shown on the user's Fitzflix admin page as the password. (The account password is not accepted; the key can be regenerated from the admin page without changing your login password.)

When a download completes, Fitzflix renames the file with a *downgraded* quality title before importing — physical-media quality names are reserved for files ripped from actual discs, and `Remux` isn't used to label downloads:

| Sonarr/Radarr quality | Imported as |
| --- | --- |
| `DVD` | `WEBDL-480p` |
| `Bluray-480p` | `WEBDL-480p` |
| `Bluray-720p` | `WEBDL-720p` |
| `Bluray-1080p` | `WEBDL-1080p` |
| `Bluray-1080p Remux` | `WEBDL-1080p` |

Downloads with a custom format score below 1600 are labeled `WEBRip` instead of `WEBDL`. The file is imported in place from the download client's folder (not copied to the import directory), TV episodes that aired within the last 14 days jump to the front of the import queue, and Sonarr is asked to rescan the series after the rename so its records stay accurate.

Before accepting a webhook delivery, Fitzflix verifies the downloaded file is structurally complete (the same truncation probe the import directory uses). An incomplete file — a stalled or corrupted download — is not imported: Fitzflix marks the grab **failed** back in Sonarr/Radarr, which blocklists that release and searches for another, then deletes the bad file and emails a report; if the failure can't be reported (so the *arr wouldn't re-download), the file is left in place for manual handling and the email says so.

## Tracking Plex watches

Fitzflix can record movie watches straight from Plex. Every watch bumps the movie's shopping-list priority for the whole household, and a watcher mapped to a Fitzflix account also gets the watch recorded in their diary as an unrated entry — flagged as a rewatch when they've logged the film before. Paired with the Letterboxd RSS sync above, the whole diary loop is hands-free: Plex supplies the timestamped watch, the Letterboxd review supplies the verdict, and the sync merges them into one row. Two sources feed the same recording logic and de-duplicate against each other, so they can (and ideally should) run together: the webhook reports watches in real time, and the poller catches anything the webhook missed while Fitzflix was down.

### Webhook (real time; requires Plex Pass)

1. Set `PLEX_WEBHOOK_TOKEN` in `.env` to a long random string (e.g. `python3 -c "import secrets; print(secrets.token_hex(24))"`) and restart Fitzflix.
2. In Plex Web, open **Settings → Account → Webhooks** and add:

   `https://<fitzflix host>/api/plex/webhook/<PLEX_WEBHOOK_TOKEN>`

Plex can't send credentials with webhooks, so the secret in the URL is the authentication — the endpoint answers 404 to anything else. Only movie `media.scrobble` events are recorded (a scrobble fires when Plex considers the item watched, at about 90% played); play/pause/rating events and TV episodes are ignored. Movies are matched to the library by their TMDb guid, covering both the current Plex Movie agent and the legacy TMDb agent.

### History poller (self-healing backstop; no Plex Pass needed)

Set `PLEX_URL` (e.g. `http://<plex host>:32400`) and `PLEX_TOKEN` ([finding your token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)) in `.env` and restart. A scheduled task then polls Plex's watch history every 15 minutes from a stored cursor, so watches scrobbled while Fitzflix was down are picked up on the next poll. The first poll only plants the cursor — history from before the feature was enabled is not imported.

### Mapping watchers to users

Each Fitzflix user can enter their **Plex username** on their Profile page. Watches by that Plex account then land in their diary; watches by unmapped accounts (house guests, unlinked managed users) still count toward the household shopping-list priority, just without a diary entry.

If Tautulli has been calling `/api/add-to-cart`, disable that notifier once direct tracking is confirmed working — the endpoint still works, but Tautulli and the direct sources would each count the same watch.

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
| [mkvmerge](https://mkvtoolnix.download) (MKVToolNix) | `MKVMERGE_BIN` | Remuxing Matroska files: stripping non-native-language and empty tracks, and converting other importable containers to Matroska |
| [mkvpropedit](https://mkvtoolnix.download) (MKVToolNix) | `MKVPROPEDIT_LOCATION` | Editing Matroska properties in place: default/forced track flags, track statistics |
| [HandBrakeCLI](https://handbrake.fr) | `HANDBRAKE_BIN` | Transcoding library files to smaller Plex-friendly versions (see `HANDBRAKE_PRESET` / `HANDBRAKE_PRESET_FILE` for preset info) |
| [ffmpeg](https://ffmpeg.org) | `FFMPEG_BIN` | Video conversion functions |
| [AtomicParsley](https://github.com/wez/atomicparsley) | `ATOMICPARSLEY_BIN` | Stripping embedded metadata from MP4 files that can't be converted to Matroska |

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
| `PLEX_URL`, `PLEX_TOKEN`, `PLEX_WEBHOOK_TOKEN` | Direct Plex watch tracking: URL and token enable the 15-minute history poller, and the webhook token gates the `/api/plex/webhook/<token>` endpoint (see [Tracking Plex watches](#tracking-plex-watches)) |
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
| `fitzflix-file-operation` | Per-file operations: S3 uploads and downloads, Matroska property edits, carrying localized files from staging into the library |
| `fitzflix-transcode` | HandBrake transcodes (CPU-heavy; usually one worker) |
| `fitzflix-sql` | Database writes, including the database half of TMDb refreshes — run exactly one worker so they're serialized |
| `fitzflix-user-request` | Jobs triggered from the web UI and CLI: manual scans, S3 sync, SQS polling, and the network half of TMDb refreshes |
| `fitzflix-maintenance` | Scheduled application upkeep — nightly log rotation and backups, recommendation recomputes, awards and Criterion refreshes, availability cache warming — one worker |

A TMDb refresh runs in two phases: the API queries happen on `fitzflix-user-request` (safe to run several at once, since nothing touches the database), and the fetched payload is then applied — record updates, file renames, duplicate merges — on the single-worker `fitzflix-sql` queue, so database writes never run concurrently. All TMDb API traffic flows through a shared Redis rate limiter capped at `TMDB_REQUESTS_PER_SECOND` (default 10) across every process, keeping Fitzflix well under [TMDb's ~40–50 requests/second limit](https://developer.themoviedb.org/docs/rate-limiting). Poster and cast artwork isn't stored locally at all — the pages hotlink [TMDb's image CDN](https://developer.themoviedb.org/docs/image-basics) directly (base URL configurable via `TMDB_IMAGE_URL`), and the service worker's cross-origin caching keeps recently viewed artwork available offline.

## Running Manually

### Redis

#### Scheduler

One scheduler process handles all recurring and deferred jobs (rq's native cron plus the scheduled-job mover — no separate `rq-scheduler` package). Cron expressions are evaluated on the server's local clock, and each task's last and next run shows on the System page:

```
source venv/bin/activate &&
python scheduler.py
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
| `flask refresh criterion` | Refresh Criterion Collection data from Wikidata (also runs automatically on the 18th of each month, after Criterion's mid-month announcements) |
| `flask recs recompute` | Rebuild every user's taste profile and stored recommendations now, instead of waiting for the nightly 1:45 AM run |
| `flask recs streaming` | Rebuild the streaming shelf now (nightly at 2:15 AM otherwise) |
| `flask recs leaving` | Refresh the leaving-Criterion set now (monthly on the 1st otherwise) |
| `flask recs awards` | Refresh Wikidata award records now — the film-item pass, then the person-item craft pass (weekly on Mondays otherwise) |
| `flask recs copref <dataset-dir>` | Rebuild the MovieLens co-preference table from an extracted ml-32m directory (needs `numpy`/`scipy` installed ad hoc; only when adopting a new snapshot) |
| `flask recs evaluate` | Leave-one-out ranking metrics for the engine — the measuring stick for any scoring change |
| `flask triage backfill` | Queue subtitle-triage inspection aids for every existing candidate file |

Criterion data comes from [Wikidata](https://www.wikidata.org): each movie is matched by TMDb id (falling back to title and year) to pick up its spine number and a direct link to its film page at criterion.com. Box sets are supported too — a film released only inside a set (say, a Godzilla Showa-era or Olympic-films collection) takes its set's spine number, and the set title is filled in automatically when one hasn't been entered by hand. The refresh is additive for anything hand-set — it never clears spine numbers or overwrites hand-curated set titles, and in-print/disc-owned flags stay whatever they've been set to — and a full refresh also creates library records for spine releases Fitzflix has never seen, so newly announced titles join the Criterion catalog page automatically.

## AWS infrastructure

Everything Fitzflix keeps at AWS lives in one S3 bucket (`AWS_BUCKET`), laid out by prefix:

| Prefix | Contents | Lifecycle |
| --- | --- | --- |
| `untouched/` (`AWS_UNTOUCHED_PREFIX`) | Archived original media, uploaded on import when `ARCHIVE_ORIGINAL_MEDIA` is set | Uploaded as `STANDARD`, transitioned to **Glacier Deep Archive** by a day-0 lifecycle rule |
| `backup/` (`AWS_BACKUP_PREFIX`) | Nightly database dumps and the encrypted `.env` copy | Pruned by the backup task's own retention window |
| `custom-posters/` (`AWS_CUSTOM_POSTERS_PREFIX`) | Mirror of the custom artwork tree, synced by the nightly backup (deletions propagate) | Noncurrent versions expire after 30 days |

**Bucket versioning** stays enabled — it is the recovery layer for deleted or overwritten objects, and the [disaster recovery](#disaster-recovery) procedure depends on it. Versioning is bucket-wide (S3 has no per-prefix versioning); what varies per prefix is how long noncurrent versions are kept. For `untouched/`, a deleted or replaced original's previous version stays recoverable for **180 days** — chosen to match Deep Archive's 180-day minimum storage duration, so the window is free: expiring noncurrent versions any sooner would cost the same in early-deletion fees. A bucket-wide lifecycle rule also aborts **incomplete multipart uploads** after one day, so an interrupted archive or backup upload can't silently accumulate billable, invisible parts.

**Restore notifications**: the bucket sends `s3:ObjectRestore:Completed` events for `untouched/` to an SQS queue (`AWS_SQS_URL`); the hourly poll drains it and downloads each completed restore. See [Restoring files from AWS](#restoring-files-from-aws).

### Provisioning

```bash
flask aws provision
```

idempotently creates whatever is missing and reports each component — the bucket, versioning, the lifecycle rules, the SQS queue (printing the `AWS_SQS_URL` line to add to `.env` if it created one), the queue policy that lets S3 deliver to it, and the restore-event notification. Existing configuration is always preserved: lifecycle rules and notifications are appended to, never replaced, so hand-made rules survive. Re-running against a fully configured account changes nothing and says so. Every run saves the as-found lifecycle and notification configuration to `logs/aws-snapshots/` before writing, and refuses to proceed if the bucket reports fewer lifecycle rules than the newest snapshot — pass `--force` when the reduction is intentional (for example, wiping the rules to rebuild the provision-managed set from a clean slate).

### IAM

The app runs fine with a policy scoped to its bucket and queue. Day-to-day operation needs:

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject", "s3:RestoreObject", "s3:ListBucket"],
            "Resource": ["arn:aws:s3:::<bucket>", "arn:aws:s3:::<bucket>/*"]
        },
        {
            "Effect": "Allow",
            "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"],
            "Resource": "arn:aws:sqs:<region>:<account>:<queue>"
        }
    ]
}
```

`flask aws provision` additionally needs `s3:CreateBucket`, `s3:PutBucketVersioning`, `s3:GetBucketVersioning`, `s3:PutLifecycleConfiguration`, `s3:GetLifecycleConfiguration`, `s3:PutBucketNotification`, `s3:GetBucketNotification`, `sqs:CreateQueue`, and `sqs:SetQueueAttributes` — grant those temporarily, or run provisioning with an admin credential once and drop back to the runtime policy.

## Restoring files from AWS

Archived originals live in S3 Glacier Deep Archive, so getting one back is a two-step process:

1. On the file's detail page, request the download — Fitzflix asks AWS to restore the object from Glacier. Restores from Deep Archive typically take hours to complete. To restore in bulk, a TV series page's **Restore series from AWS** button (or a season page's **Restore season from AWS**) requests every best-ranked archived file at once. Because restores cost real money, each restore button shows an estimated cost (per-request, per-GB retrieval, and per-GB transfer fees — season/series restores use the cheaper Bulk retrieval tier, single files use Standard; tune the `AWS_RESTORE_PER_1K_REQUEST_COST`, `AWS_RESTORE_PER_1K_REQUEST_BULK_COST`, `AWS_RESTORE_PER_GB_COST`, `AWS_RESTORE_PER_GB_BULK_COST`, and `AWS_DOWNLOAD_PER_GB_COST` values in `.env` to match the current AWS rate card) and requires your account password to confirm.
2. When AWS finishes the restore, it posts a notification to the SQS queue (`AWS_SQS_URL`; the S3 bucket must be configured to send its restore-completed event notifications there). Fitzflix polls the queue automatically every hour and downloads each completed restore back into the library; run `flask sqs` to poll immediately instead of waiting for the next scheduled check.


## Disaster recovery

Everything needed to rebuild Fitzflix on a fresh machine derives from three things kept outside the machine: **AWS credentials + the bucket name, the `BACKUP_PASSPHRASE`, and access to this git repository** — keep all three in a password manager. The steps, in order:

1. **Install the system requirements** (Homebrew: MariaDB, Redis, supervisor, and the third-party binaries listed above), clone this repository, create the virtualenv, and `pip install -r requirements.txt`.
2. **Recover `.env`**: download the newest `backup/dotenv-<date>.enc` from the S3 bucket and decrypt it into the project root:

   ```
   BACKUP_PASSPHRASE=<from your password manager> openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in dotenv-<date>.enc -out .env -pass env:BACKUP_PASSPHRASE
   ```
3. **Create the database and user** named in the recovered `SQLALCHEMY_DATABASE_URI`, grant the user all privileges on that database, and add the restore-drill grant while you're there:

   ```
   GRANT ALL PRIVILEGES ON `fitzflix_restore_check`.* TO '<user>'@'localhost';
   ```
4. **Restore the newest dump** from `backup/` in the bucket:

   ```
   zcat fitzflix_db-<date>.sql.gz | mysql --user=<user> --password <database>
   ```

   then bring the schema up to the current code with `flask db upgrade` (a no-op unless the code is newer than the dump).
5. **Restore the custom posters**: copy the bucket's `custom-posters/` prefix back to `app/static/custom/` (e.g. `aws s3 sync s3://<bucket>/custom-posters/ app/static/custom/`). TMDb artwork doesn't need restoring — it's hotlinked from TMDb's image CDN and never stored locally.
6. **Mount the NAS volumes** (see the SMB notes: pin the NAS hostname in `/etc/hosts`, and `protocol_vers_map`/signing settings in `/etc/nsmb.conf`), and recreate the staging directory on local disk.
7. **Start the workers** via supervisor and confirm the System page's health card is green. Scheduled jobs re-register themselves on startup; Redis needs no restoration — the only Redis-resident data of consequence (recommendation rankings, availability caches) rebuilds itself within a day, or immediately via the `flask recs` commands.
8. **Only if the NAS was also lost**: the localized library can be rebuilt from the untouched archives — the S3 sync task queues Bulk restores for every rank-1 file missing locally, and `inventory/rank_1.csv` in the bucket supports an S3 Batch Operations restore of everything at once.

## Logs and maintenance

All processes write to a shared log, `logs/fitzflix.log` (configurable via `LOG_FILE`). Configuration problems — missing binaries, unreachable media directories, incomplete AWS or mail settings — are reported there as warnings at startup, so check the log first when something isn't working.

The log rotates automatically every night at midnight: the day's file is gzipped alongside as `fitzflix.log.<date>.gz`, and archives older than `LOG_RETENTION_DAYS` (default 14) are deleted.

The database is backed up nightly at 12:30 AM to a compressed dump in `DB_BACKUP_DIR` (default `backups/` in the project root), keeping `DB_BACKUP_RETENTION_DAYS` (default 14) days of dumps — the media files are archived at AWS, but reviews, Criterion details, and shopping priorities exist only in the database. When AWS is configured, each dump is also uploaded to the S3 bucket under `AWS_BACKUP_PREFIX` (default `backup`) in Standard storage, and remote dumps past the retention window are pruned on the same schedule, so losing the machine doesn't lose the database. The nightly backup also uploads an encrypted copy of `.env` (AES-256, requires `BACKUP_PASSPHRASE` to be set — keep the passphrase in a password manager, since it's the key to recovering everything else) and mirrors the custom posters in `app/static/custom/` to the bucket under `AWS_CUSTOM_POSTERS_PREFIX` (default `custom-posters`). On the 1st of each month a restore drill downloads the newest offsite dump, restores it into a scratch `fitzflix_restore_check` database, and compares row counts against the live database, so a dump that won't restore is discovered within a month instead of during a disaster; the drill needs a one-time grant (see Disaster recovery below). The System page shows live worker health, each scheduled task's last and next run, and any failed background jobs with requeue/forget buttons; the Library Maintenance page includes a filename tester that previews how a file would be parsed and filed without importing anything.

### Subtitle triage

Discs sometimes carry a forced-subtitle track (the translations burned over foreign dialogue) without the forced flag set, so Plex never shows it. The **Library Maintenance → Possibly-forced subtitles** page applies a heuristic to every file — an unforced track with a small fraction of its largest same-language sibling's cue count — and presents each candidate with inspection aids generated at import: a cue-density timeline and five burned-in snapshots taken at the track's own cue times, so a real forced track (sparse translation cues) is easy to tell from a trivia or commentary track. Flagging selected tracks sets the forced flag in place with mkvpropedit; dismissing marks the file reviewed. Each candidate also gets a **per-file triage page** (linked from its card and from the file's own page while candidates are pending) that loads just that file's snapshots, with actions returning to whichever page you came from. Re-importing a replacement file resets the file's triage state — a new file is new evidence, so its candidates are re-presented even if an earlier copy was reviewed. `flask triage backfill` queues aids for files that predate the feature.

### Custom posters

Any movie or file can carry custom artwork: the poster picker on a movie page shows TMDb's full poster gallery (grouped by language, with TMDb's default highlighted) for one-click selection, or accepts an upload. Custom posters live under `app/static/custom/`, are mirrored to the S3 bucket by the nightly backup, and can be removed with one click to fall back to the library's precedence rules.

### Rejected files

Files that can't be imported — an unparseable filename, an unrecognized quality tag, or a file that fails processing — are moved into a subfolder of `REJECTS_DIR` named for the reason they were rejected, so the folder name tells you what to fix. The `/rejects` page (linked from the Library Maintenance page) lists them for triage: one click re-imports a file (moving it back to the import directory, where it's picked up automatically) or deletes it. The Library Maintenance page also surfaces movies that share a TMDb id, with a one-click merge that moves the duplicates' files and reviews to the oldest record. Active and queued jobs can be watched on the `/queue` page.

### Updating

```
git pull &&
venv/bin/pip install -r requirements-dev.txt &&
venv/bin/flask db upgrade &&
supervisorctl restart "fitzflix:*"
```

(`requirements-dev.txt` includes `requirements.txt`; a runtime-only install
can use `requirements.txt` alone.)

## Code formatting

Commits are formatted with [black](https://github.com/psf/black) and linted
with pyflakes via [pre-commit](https://pre-commit.com). One-time setup per
clone:

```
venv/bin/pip install -r requirements-dev.txt
```

```
venv/bin/pre-commit install
```

The hooks run on staged Python files at each commit (the `migrations/`
directory is excluded — Alembic's generated scripts keep their generated
formatting). If black reformats anything, the commit stops so the changes
can be reviewed and re-staged; `git commit --no-verify` bypasses the hooks.
`venv/bin/black .` formats the whole tree by hand, honoring the same
exclusion via `pyproject.toml`.

## Running the tests

```
venv/bin/python -m pytest
```

The suite is fully isolated from a running installation: it uses a temporary
SQLite database, Redis database 9 (the app uses database 0), temporary media
directories, and no external services — TMDb, Sonarr, Radarr, AWS, and mail
are all stubbed or unreachable by configuration. The only requirements beyond
`requirements-dev.txt` are a local Redis server. It's safe to run while the real
application is up, and takes about half a minute; run it after dependency
upgrades and before deploying changes.

Covered: the filename-naming rules (every example table above is a test
fixture), webhook authentication and the Sonarr/Radarr download flow, the
deferred-retry scheduling machinery, the per-title lock contract, log
rotation and database backups, the system-health probe and its email
alerting, file-ranking queries, and page rendering.
