# The map of the Fitzflix code

This file tells you where each part of Fitzflix lives. Read it before you open the code. The README tells you how to install and how to use the application. The file `tests/README.md` tells you how the tests work. The file `RUNBOOK.md` tells you what to do when the operation goes wrong.

The application has approximately 100 Python modules. You do not need to know all of them. Most changes touch 1 subject, and each subject has its own folder. Find the subject in the tables below, then open only that folder.

## The programs that run

Supervisor runs 11 processes from 8 programs. The file `fitzflix_supervisor.ini` lists them.

| Program | Processes | Start file | Function |
| --- | --- | --- | --- |
| `fitzflix-web` | 1 | `fitzflix.py` | Serves the pages and receives the webhooks (gunicorn) |
| `fitzflix-rqscheduler` | 1 | `scheduler.py` | Starts each scheduled job and each retry that waits |
| `fitzflix-import` | 2 | `supervisor.py` | Workers for new files. They also watch the import directory |
| `fitzflix-file-operation` | 2 | `supervisor.py` | Workers for uploads, downloads, and track edits |
| `fitzflix-transcode` | 1 | `supervisor.py` | Worker for the conversions that use much CPU |
| `fitzflix-sql` | 1 | `supervisor.py` | Worker for the database writes, 1 at a time |
| `fitzflix-user-request` | 2 | `supervisor.py` | Workers for the jobs that a page or a command starts |
| `fitzflix-maintenance` | 1 | `supervisor.py` | Worker for the scheduled maintenance |

Each process builds the same application object with `create_app` in `app/__init__.py`.

The web process does no slow work. A page puts a job on a queue in Redis, and a worker does the job. The README section "Worker queues" tells you what each of the 7 queues is for.

Fitzflix keeps its data in 4 places:

- MariaDB holds the records. The file `app/models.py` defines each table.
- Redis holds the queues, the locks, the caches, and the trail of each file.
- The disks hold the library, the import directory, and the staging area.
- AWS S3 holds the archive copy of each file.

## The base files

These files are in `app/` itself, not in a folder of a subject. Each other module uses them. Read them first.

| File | Contents |
| --- | --- |
| `config.py` | Each setting of the `.env` file, with its default value |
| `app/__init__.py` | `create_app` builds the application. `cron_table` is the schedule of each recurring job |
| `app/models.py` | All the database tables |
| `app/tasks.py` | `TASKS` lists each background job with its queue and its time limit. `enqueue_task` starts a job. `requeue` books a retry |
| `app/queues.py` | The list of the 7 queues |
| `app/settings.py` | The settings that the Admin Settings page can change. `get_setting` reads one |
| `app/pipeline.py` | The trail of each file, which the Queue page shows |
| `app/cache.py` and `app/redis_guard.py` | The cached values in Redis, and the pages that must work while Redis is down |
| `app/email.py` | The mail to the users and to the admin |
| `app/templating.py` | The filters that the templates use |
| `app/cli.py` | The `flask` commands that belong to no folder |
| `migrations/versions/` | 1 file for each change of the database structure |

## The path of a page request

1. Flask finds the function for the address in a route module. The table below lists the route modules.
2. The function reads the records through the classes of `app/models.py`. It gets the larger answers from a subject module.
3. The function renders a template of `app/templates/`. The template usually has the name of the page, for example `movie.html`.
4. For slow work, the function calls `enqueue_task`. Then a worker does the work, and the page shows the progress.

A template whose name starts with an underscore is a part that other templates include. The template `base.html` is the frame of each page.

The JavaScript that all pages share is in files of `app/static/`. The template `base.html` loads them in this order:

| Script | Subject |
| --- | --- |
| `passkeys.js` | The sign-in with a passkey |
| `site.js` | The progress line at the top of the page. It loads for each visitor |
| `search.js` | The suggestions of the search box |
| `live.js` | The functions that the live parts share: the poll (`startPoll`) and the background post (`backgroundPost`) |
| `queue_badge.js` | The queue badge and the progress of the tasks that run |
| `poster_card.js` | The card that shows when the pointer is on a poster |
| `tiles.js` | The star row and the watchlist button of a poster, and the state that each poster gets after the page load |
| `plex_play.js` | The buttons that start a film on the Apple TV |

The template `bootstrap.html` loads `pull_to_refresh.js` before them. That template also holds 2 short scripts that must run before the first paint. They set the dark mode and the October font.

A script that is only for 1 page is a file whose name starts with `page_`. The rest of the name is the name of the template, for example `page_queue.js` for `queue.html`. The template gives each address that the script needs as an attribute of the script tag.

| Route module | Pages |
| --- | --- |
| `app/main/discover.py` | The landing page, Recommendations, Watchlist, Newly added, Leaving, File Activity |
| `app/main/movie_pages.py` | The movie library and the pages of 1 film |
| `app/main/tv_pages.py` | The TV library and the pages of 1 series |
| `app/main/file_pages.py` | The page of 1 file: its tracks, its archive, and its delete |
| `app/main/people_pages.py` | The People page and the page of 1 person |
| `app/main/criterion_pages.py` | The page of the Criterion Collection |
| `app/main/shopping.py` | The 2 shopping lists |
| `app/main/search.py` | The search of the library and of TMDB |
| `app/main/account.py` | Profile, History, Stats, and the review editor |
| `app/main/admin.py` | Queue, System, Settings, Rejects, the file list, and each Maintenance page |
| `app/main/posters.py` | The poster picker |
| `app/main/game.py` | Name That Frame |
| `app/main/helpers.py` | Functions that 2 or more route modules use |
| `app/main/forms.py` | Each form of the pages |
| `app/auth/` | Sign-in, registration, password reset, and passkeys |
| `app/api/` | The Sonarr, Radarr, and Plex webhooks |

## The path of a new file

This is the most important sequence of the application. Almost all of it is in the folder `app/media/importing/`. The file names below are in that folder.

1. A file arrives through 1 of 3 doors. The doors are the watcher of the import directory (`app/media/import_watch.py`), a Sonarr or Radarr webhook (`app/api/arr.py`), and the hourly sweep (`manual_import_task` in `sweep.py`). Each door calls `enqueue_localization` in `chain.py`.
2. `localization_task` in `localization.py` runs on the import queue. It reads the name of the file with `evaluate_filename` of `filenames.py`, and it takes the lock of the title. It checks the quality and the free space, then copies the file to the staging area. It uploads the untouched file to S3. Then it removes the audio and subtitle tracks that are not in your language.
3. `convert_localized_file` runs on the transcode queue, only for an HDR file. It is in `app/media/sdr_conversion.py`.
4. `move_localized_file` in `library_move.py` runs on the handoff queue. It copies the result into the library under a hidden name, and it reads the details of the media.
5. `finalize_localization` in `cataloging.py` runs on the sql queue. It writes the records, gives the hidden copy its final name, and commits. It also deletes the worse copies of the same title.
6. The follow-up jobs start after the commit, from `follow_ups.py`. Examples are the TMDB refresh, the Atmos audio twin, and the upload of a changed file to the archive.

A file that fails goes to the rejects directory (`move_to_rejects` in `filenames.py`). The Rejects page lists these files. Each stage writes its status to the trail of the file. The Queue page shows the trail.

Each step is a separate job. A job gives the file to the next job with `enqueue_task`. To follow the sequence in the code, search for the name of the next job.

## Where each subject lives

Each subject has a folder in `app/`. The files that all subjects use are in `app/` itself. The section "The base files" lists them.

| Folder | Subject |
| --- | --- |
| `app/media/` | The media files of the library: the import, the tracks, the names, and the conversions |
| `app/archive/` | The archive of the library in AWS |
| `app/metadata/` | The data about films and series from TMDB and Wikidata |
| `app/arr/` | The calls to Radarr and Sonarr |
| `app/plex/` | The Plex library |
| `app/playback/` | The start of a film on an Apple TV |
| `app/viewing/` | The viewing diary and its statistics |
| `app/discovery/` | The recommendation engine |
| `app/availability/` | Where a film streams |
| `app/criterion/` | The Criterion Collection and the Criterion Channel |
| `app/operations/` | Maintenance, the logs, and the Queue page |
| `app/main/` | The pages. The section "The path of a page request" lists its files |
| `app/auth/` | Sign-in, registration, password reset, passkeys, and the limit on failed sign-ins |
| `app/api/` | The Sonarr, Radarr, and Plex webhooks |
| `app/errors/` | The error pages |

A file that ends in `_cli.py` holds the `flask` commands of its folder. The file `app/cli.py` holds the other commands.

One subject has only 1 file, and that file is in `app/` itself. The file `app/frames.py` holds the pictures for Name That Frame.

### `app/media/`: the media files

| Module | Subject |
| --- | --- |
| `importing/filenames.py` | The rules for filenames: parse a name, build a name, and reject a file |
| `importing/chain.py` | The jobs of the import that wait or run, and the lock of a title |
| `importing/space.py` | The free space of the library volume and of the staging area |
| `importing/localization.py` | The first job of an import |
| `importing/library_move.py` | The second job: the copy into the library |
| `importing/cataloging.py` | The third job: the records and the final name |
| `importing/follow_ups.py` | The work after the commit of an import |
| `importing/sweep.py` | The hourly sweep of the import directory |
| `import_watch.py` | The watcher of the import directory |
| `manual_imports.py` | The downloads that Sonarr and Radarr cannot import |
| `sdr_conversion.py` | The conversion of HDR files to SDR |
| `tracks/scan.py` | The scan of the tracks of a file with MediaInfo, and the track records |
| `tracks/languages.py` | The language codes and the language names of the tracks |
| `tracks/edits.py` | The edits of the tracks: flags, languages, order, and removal |
| `tracks/remux.py` and `tracks/rewrite_guard.py` | The remux of a library file, and the checks around a rewrite |
| `tracks/audio_supplements.py` | The second audio tracks for players that cannot decode a lossless track |
| `atmos.py` | The second Atmos track that more players can decode |
| `videos.py` | The code that all file tasks share: the copy with progress, the title lock, and the control of the command-line tools |
| `file_ranking.py` | The rule that selects the best copy of a title |
| `naming.py` and `title_keys.py` | The names of the library files, and the keys that compare titles |
| `series_rename.py` | The rename of a TV series on disk and in the database, and the nightly Sonarr path check |
| `missing_files.py` | The best files that have no local copy |
| `custom_artwork.py` | The custom posters on disk |
| `triage.py` | The clips and pictures that help you examine a doubtful track |
| `worker_recovery.py` | The cleanup after a worker that died |

### `app/archive/`: the archive in AWS

| Module | Subject |
| --- | --- |
| `aws_clients.py` | The S3 and SQS clients, and the small functions that all the archive code uses |
| `archive_keys.py` | The key of an archive, and the records that claim a key |
| `uploads.py` | The upload of a file to its archive |
| `restores.py` | The restore of an archive from cold storage, and the SQS poll |
| `downloads.py` and `cdn.py` | The download of an archive into the library, direct or through CloudFront |
| `moves.py` | The move and the delete of an archive object |
| `sync.py` | The weekly audit of the bucket against the library |
| `s3_layout.py` | The folders of the bucket |
| `aws_setup.py` | The setup of the AWS components |

### `app/metadata/`, `app/arr/`, `app/plex/`, and `app/playback/`: the other services

| Module | Subject |
| --- | --- |
| `metadata/tmdb_refresh.py`, `metadata/tmdb_apply.py`, `metadata/tmdb_changes.py` | The film and series data from TMDB |
| `metadata/awards.py` | The film awards from Wikidata |
| `arr/arr_client.py`, `arr/radarr_push.py`, `arr/sonarr_push.py` | The calls to Radarr and Sonarr |
| `api/arr.py`, `api/radarr.py`, `api/sonarr.py` | The webhooks from Radarr and Sonarr |
| `plex/plex_api.py`, `plex/plex_library.py`, `plex/plex_rename.py` | The Plex library: refreshes and renames |
| `plex/plex_collections.py`, `plex/plex_selection.py`, `plex/plex_titles.py` | The Plex collections, selected tracks, and episode titles |
| `plex/plex_watchlist.py` | The sync of the 2 watchlists |
| `playback/plex_player.py` and `playback/infuse_player.py` | The start of a film on an Apple TV |

### `app/viewing/`, `app/discovery/`, `app/availability/`, and `app/criterion/`: the diary and the discovery pages

| Module | Subject |
| --- | --- |
| `viewing/diary.py` | The diary: reviews, Plex watches, and the import of a Letterboxd export |
| `viewing/letterboxd.py` and `viewing/richtext.py` | The sync with Letterboxd, and the text format of a review |
| `viewing/stats.py` | The viewing statistics |
| `discovery/recommendations.py`, `discovery/rec_shelves.py`, `discovery/copref.py` | The recommendation engine and its shelves |
| `discovery/elicitation.py`, `discovery/estimate_warm.py`, `discovery/funnel.py` | The smaller parts of the recommendations |
| `availability/streaming.py`, `availability/streaming_rail.py`, `availability/availability_alerts.py` | Where a film streams, and the alerts for the watchlist |
| `availability/provider_catalog.py` and `availability/newly_added.py` | The catalogs of the streaming services, and the films that they added |
| `criterion/criterion_catalog.py`, `criterion/criterion_now.py`, `criterion/leaving_criterion.py` | The Criterion Collection and the Criterion Channel |

### `app/operations/`: the operation of the application

| Module | Subject |
| --- | --- |
| `maintenance.py` | Backups, log rotation, the health checks, and the cleanup jobs |
| `queue_page.py` | The data of the Queue page |
| `logging_setup.py`, `redaction.py`, `access_log.py`, `log_digest.py` | The logs |
| `mail_timeout.py` | The time limit of the mail connection |

## Things that can surprise you

- **A job starts through its name, not through a call**. A job name is a text string, for example `"app.media.importing.localization.localization_task"`. The name gives the module and the function. An editor cannot follow such a string, so search for the text to find each place that starts the job.
- **Some imports are inside functions**. Most imports are at the top of their module. An import inside a function has 1 of 2 causes. The app factory, the command files, and the base files must not load the subject folders when they load. Or the import at the top would make a loop between 2 modules.
- **A function of another file of the same folder has the file name before it**. For example, `filenames.evaluate_filename(...)` in `localization.py` calls the function `evaluate_filename` of `filenames.py`. The import folder, the track folder, the archive folder, and the page modules use this form.
- **A task that changes a title takes the lock of that title**. If another task has the lock, the task books itself for a later time. The function is `acquire_lock_or_defer` in `app/media/videos.py`.
- **There are 2 sources of settings**. The `.env` file gives the settings through `config.py`. The Admin Settings page changes some of them through `app/settings.py`. A value in `.env` has priority.
- **The web process reads a template when a page first needs it**. After you change a template, deploy, or the processes show different versions.
- **The tests share 1 application object**. Read the section "The traps" of `tests/README.md` before you write a test.

## How to find a thing

The function of a page address:

```
grep -rn '"/watchlist' app/main app/auth app/api
```

The function of a job:

```
grep -rn 'def localization_task' app
```

Each place that starts a job:

```
grep -rn 'localization_task"' app
```

Each place that uses a setting:

```
grep -rn 'ARCHIVE_ORIGINAL_MEDIA' config.py app README.md
```

The schedule of a recurring job is in `cron_table` in `app/__init__.py`. The System page also shows the last run and the next run of each job.

## How to change the code safely

1. Make the change, and add a test for it.
2. Run the tests:

```
venv/bin/python -m pytest -q -p no:cacheprovider
```

3. Commit. The commit hooks format the code and check the comments. The README section "Code formatting" tells you about them.
4. Deploy with `scripts/deploy`. The README section "Updating" tells you about this script. The file `RUNBOOK.md` tells you what to do when a deploy goes wrong.
