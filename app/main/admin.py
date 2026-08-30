"""Operations pages (the routes.py split): system health, scheduled
tasks, library maintenance, triage surfaces, the queue, and the
pipeline trails."""

import os
import shutil
import time


from datetime import datetime, timezone

from flask import (
    current_app,
    make_response,
    render_template,
    flash,
    redirect,
    url_for,
    request,
)

# flask.Markup was removed in Flask 2.4; import from its actual home
from flask_login import current_user, login_required

from app import db, enqueue_import_scan
from app.main.forms import (
    CriterionRefreshForm,
    FailedJobForm,
    FilenameTestForm,
    LossyAudioTriageForm,
    SubtitleTriageForm,
    ImportForm,
    LibrarySearchForm,
    MovieMergeForm,
    RejectActionForm,
    SyncAWSStorageForm,
    QualityFilterForm,
    RuntimeMismatchForm,
    TMDBRefreshForm,
    TMDBTriageForm,
    TrackMetadataScanForm,
)
from app.models import (
    File,
    FileAudioTrack,
    FileSubtitleTrack,
    Movie,
    RefFeatureType,
    RefQuality,
    TVSeries,
    movie_file_rank,
    tv_file_rank,
)
from app.main import bp
from app.main.helpers import admin_required
from app.maintenance import system_health
from app.triage import (
    forced_subtitle_candidates,
    lossy_audio_candidates,
    lossy_audio_presentation,
    remove_audio_comparison,
    remove_triage_snapshots,
    runtime_mismatch_candidates,
    triage_presentation,
)
from app.videos import (
    evaluate_filename,
)
from rq.cron import CronScheduler
from rq.exceptions import NoSuchJobError
from rq.job import Job
from rq.registry import FailedJobRegistry


@bp.route("/system", methods=["GET", "POST"])
@login_required
@admin_required
def system():
    """System status: health, worker and scheduler state, and failed jobs."""

    queues_by_name = {
        queue.name: queue
        for queue in (
            current_app.import_queue,
            current_app.sql_queue,
            current_app.request_queue,
            current_app.transcode_queue,
            current_app.file_queue,
            current_app.maintenance_queue,
        )
    }

    # Form to requeue or forget a failed background job

    failed_job_form = FailedJobForm()
    if (
        failed_job_form.requeue_submit.data or failed_job_form.forget_submit.data
    ) and failed_job_form.validate_on_submit():
        queue = queues_by_name.get(failed_job_form.failed_queue.data)
        job_id = failed_job_form.failed_job_id.data
        registry = FailedJobRegistry(queue=queue) if queue else None

        if registry and job_id in registry.get_job_ids():
            if failed_job_form.requeue_submit.data:
                registry.requeue(job_id)
                flash(f"Requeued '{job_id}'", "info")
            else:
                try:
                    job = Job.fetch(job_id, connection=current_app.redis)
                    registry.remove(job, delete_job=True)
                except NoSuchJobError:
                    registry.connection.zrem(registry.key, job_id)
                flash(f"Removed failed job '{job_id}'", "info")
        else:
            flash("That failed job no longer exists.", "warning")

        return redirect(url_for("main.system"))

    failed_jobs = []
    for queue_name, queue in queues_by_name.items():
        registry = FailedJobRegistry(queue=queue)
        for job_id in registry.get_job_ids():
            job = queue.fetch_job(job_id)
            if job is None:
                continue

            # rq 2's stored Result carries the structured error;
            # exc_info remains as the fallback for older failures

            error = ""
            try:
                result = job.latest_result()
            except Exception:
                result = None
            if result is not None and result.exc_string:
                error_lines = result.exc_string.strip().splitlines()
                error = error_lines[-1][:200] if error_lines else ""
            if not error:
                exc_lines = (job.exc_info or "").strip().splitlines()
                error = exc_lines[-1][:200] if exc_lines else ""
            failed_jobs.append(
                {
                    "id": job_id,
                    "queue": queue_name,
                    "description": job.description or job.func_name,
                    "failed_at": job.ended_at,
                    "error": error,
                }
            )
    # rq 2 job timestamps are timezone-aware, so the missing-date fallback
    # must be aware too or the sort can't compare them

    failed_jobs.sort(
        key=lambda job: job["failed_at"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    return render_template(
        "system.html",
        title="System",
        health=system_health(current_app),
        scheduled_tasks=_scheduled_tasks(),
        relative_time=_relative_time,
        local_time=_local_time_text,
        failed_jobs=failed_jobs,
        failed_job_form=failed_job_form,
    )


def _scheduled_tasks():
    """Status of the recurring scheduled tasks, for the polled fragment.

    The schedulers share one scheduled-jobs set, so each scheduler's
    results are filtered to its own queue.
    """

    scheduled_tasks = []
    for scheduler in CronScheduler.all(current_app.redis):
        for cron_job in scheduler.get_jobs():
            meta = cron_job.job_options.get("meta") or {}
            cron_string = cron_job.cron or meta.get("cron_string", "")
            next_run = cron_job.next_enqueue_time or cron_job.get_next_enqueue_time()
            scheduled_tasks.append(
                {
                    "name": meta.get("description") or cron_job.func_name,
                    "schedule": _cron_description(cron_string),
                    "cron_string": cron_string,
                    # rq.cron records enqueue times, so "last ran" means
                    # "last started" now, not "last finished"
                    "last_run": _naive_utc(cron_job.latest_enqueue_time),
                    "next_run": _naive_utc(next_run),
                    "next_run_text": _next_run_text(_naive_utc(next_run)),
                }
            )

    # Most-frequent first (Glenn's ordering): every-X-minutes by X,
    # hourly by minute, daily by time, weekly by day and time, monthly by
    # day-of-month and time

    scheduled_tasks.sort(key=lambda task: _cron_frequency_key(task["cron_string"]))
    return scheduled_tasks


# cron counts day-of-week from Sunday, and accepts 7 as a second
# spelling of it

_WEEKDAY_NAMES = (
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
)

# How a within-the-hour schedule reads out loud: "Twice hourly at :20
# and :50". Counts past twelve keep the numeral ("13 times hourly")

_TIMES_PER_HOUR = {
    2: "Twice",
    3: "Three times",
    4: "Four times",
    5: "Five times",
    6: "Six times",
    7: "Seven times",
    8: "Eight times",
    9: "Nine times",
    10: "Ten times",
    11: "Eleven times",
    12: "Twelve times",
}


def _cron_description(cron_string):
    """Human-readable text for a five-field cron string: "Daily at
    1:45 AM", "Four times hourly at :03, :18, :33, and :48".

    Generated rather than looked up. The map this replaces had drifted
    nine schedules behind the cron table (app/__init__.py), because a
    lookup only stays right if whoever adds a job remembers to add a
    second line here. Grammar the rules below don't cover — ranges,
    hour steps, a month field — falls back to the raw cron string,
    which is terse but never wrong.
    """

    try:
        minute, hour, dom, month, dow = cron_string.split()
    except (ValueError, AttributeError):
        return cron_string

    if month != "*":
        return cron_string

    if hour == "*":
        if dom != "*" or dow != "*":
            return cron_string
        return _within_the_hour_text(minute) or cron_string

    minutes = _cron_field_values(minute)
    hours = _cron_field_values(hour)
    if not minutes or not hours or len(minutes) != 1 or len(hours) != 1:
        return cron_string
    at = _clock_text(hours[0], minutes[0])
    if at is None:
        return cron_string

    if dom == "*" and dow == "*":
        return f"Daily at {at}"
    if dom == "*":
        days = _cron_field_values(dow)
        if not days or len(days) != 1 or not 0 <= days[0] <= 7:
            return cron_string
        return f"Weekly on {_WEEKDAY_NAMES[days[0] % 7]} at {at}"
    if dow == "*":
        days = _cron_field_values(dom)
        if not days or len(days) != 1 or not 1 <= days[0] <= 31:
            return cron_string
        return f"Monthly on the {_ordinal(days[0])} at {at}"
    return cron_string


def _within_the_hour_text(minute):
    """Text for a schedule whose hour field is a wildcard — a step
    ("Every 10 minutes") or the minutes it lands on ("Hourly at :30").
    None when the field uses syntax past a step or a plain list."""

    if minute == "*":
        return "Every minute"
    if minute.startswith("*/"):
        try:
            step = int(minute[2:])
        except ValueError:
            return None
        return "Every minute" if step == 1 else f"Every {step} minutes"

    minutes = _cron_field_values(minute)
    if not minutes or any(not 0 <= value <= 59 for value in minutes):
        return None
    if len(minutes) == 1:
        return "Hourly" if minutes[0] == 0 else f"Hourly at :{minutes[0]:02d}"
    times = _TIMES_PER_HOUR.get(len(minutes), f"{len(minutes)} times")
    marks = _and_list([f":{value:02d}" for value in sorted(minutes)])
    return f"{times} hourly at {marks}"


def _cron_field_values(field):
    """The plain integers a cron field lists, or None if it uses any
    syntax past a comma-separated list — a wildcard, range, or step."""

    try:
        return [int(value) for value in field.split(",")]
    except ValueError:
        return None


def _and_list(items):
    """Join items the way the schedule text reads them: "a and b" for
    two, and an Oxford-comma list for three or more."""

    if len(items) == 2:
        return " and ".join(items)
    if len(items) > 2:
        return ", ".join(items[:-1]) + f", and {items[-1]}"
    return "".join(items)


def _clock_text(hour, minute):
    """A 24-hour cron time as the page says it: "1:45 AM", or the words
    for the two times that have them. None if either field is out of
    range."""

    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    if minute == 0 and hour == 0:
        return "midnight"
    if minute == 0 and hour == 12:
        return "noon"
    return f"{hour % 12 or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"


def _ordinal(number):
    """1 -> "1st", 18 -> "18th": the day-of-month in a monthly
    schedule's description."""

    if 11 <= number % 100 <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def _naive_utc(when):
    """rq.cron hands back timezone-aware UTC datetimes; the relative and
    tooltip renderers speak naive-UTC like the rest of rq."""

    if when is None:
        return None
    if when.tzinfo is not None:
        return when.astimezone(timezone.utc).replace(tzinfo=None)
    return when


def _cron_frequency_key(cron_string):
    """Sort key for the scheduled-tasks table: frequency class first
    (every-X-minutes, hourly, daily, weekly, monthly), then the class's
    own parameter — X, the minute, the time, the day+time."""

    try:
        minute, hour, dom, _, dow = cron_string.split()
        if minute.startswith("*/"):
            return (0, (int(minute[2:]),))
        if hour == "*":
            return (1, (int(minute.split(",")[0]),))
        if dom == "*" and dow == "*":
            return (2, (int(hour), int(minute)))
        if dow != "*":
            return (3, (int(dow), int(hour), int(minute)))
        return (4, (int(dom), int(hour), int(minute)))
    except (ValueError, AttributeError):
        return (9, ())


def _local_time_text(when):
    """A naive-UTC timestamp (rq job and scheduler times) rendered in
    the server's local zone for mouseover tooltips — the server shares
    a household, and therefore a timezone, with its viewers. Matches
    moment.js's LLL format so the queue table's browser-local tooltips
    and these server-local ones read identically."""

    if when is None:
        return ""
    local = when.replace(tzinfo=timezone.utc).astimezone()
    return local.strftime("%B %-d, %Y %-I:%M %p")


def _next_run_text(next_run):
    """Render a task's next-run time without ever calling it the past.

    A due job's stored time sits in the past until the scheduler's next
    tick (60s interval) moves it onto the queue and re-computes the
    following run, so the 5s poll routinely catches slightly-past values:
    those are "due now". Older than a couple of ticks means the scheduler
    has actually stalled, which the health card's badge also shows.
    """

    if next_run.tzinfo is None:
        next_run = next_run.replace(tzinfo=timezone.utc)
    lateness = (datetime.now(timezone.utc) - next_run).total_seconds()
    if lateness <= 0:
        return _relative_time(next_run)
    if lateness <= 120:
        return "due now"
    return "overdue"


def _relative_time(moment_dt):
    """Coarse relative-time text: '4 minutes ago', or 'in 4 minutes' for
    future times like a task's next run.

    The health fragment is re-rendered by every poll, so server-side text
    stays current without flask-moment — whose scripts wouldn't re-run
    inside swapped-in HTML anyway.
    """

    if moment_dt.tzinfo is None:
        moment_dt = moment_dt.replace(tzinfo=timezone.utc)
    # round(), not int(): truncation toward zero would undercount future
    # spans ("in 3 days" minus a microsecond is still 3 days, not 2)
    seconds = round((datetime.now(timezone.utc) - moment_dt).total_seconds())
    future = seconds < 0
    seconds = abs(seconds)
    minutes = seconds // 60
    hours = minutes // 60
    if seconds < 60:
        text = "under a minute"
    elif minutes < 60:
        text = f"{minutes} minute{'s' if minutes != 1 else ''}"
    elif hours < 24:
        text = f"{hours} hour{'s' if hours != 1 else ''}"
    else:
        days = hours // 24
        text = f"{days} day{'s' if days != 1 else ''}"
    return f"in {text}" if future else f"{text} ago"


@bp.route("/system/metrics")
@login_required
@admin_required
def system_metrics():
    """The live health fragment the System page's poller swaps in.

    Everything rendered here reads Redis or the local filesystem; the
    external-service badges come from the health_probe task's snapshot, so
    polling generates no external traffic.
    """

    fragment = render_template(
        "_system_health.html",
        health=system_health(current_app),
        scheduled_tasks=_scheduled_tasks(),
        relative_time=_relative_time,
        local_time=_local_time_text,
    )
    response = make_response(fragment)
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.route("/maintenance", methods=["GET", "POST"])
@login_required
@admin_required
def maintenance():
    """Library maintenance: rejected-file triage, duplicate movies, the
    filename tester, and the library-wide bulk operations."""

    # Form to update the Criterion Collection information for the entire movie library

    criterion_refresh_form = CriterionRefreshForm()
    if (
        criterion_refresh_form.criterion_refresh.data
        and criterion_refresh_form.validate_on_submit()
    ):
        # On the user-request queue, like the monthly scheduled refresh runs
        # on maintenance: the forced Wikidata fetch would otherwise block
        # the single sql worker on network I/O

        current_app.request_queue.enqueue(
            "app.videos.refresh_criterion_collection_info",
            args=None,
            job_timeout="1h",
            description="Refreshing Criterion Collection information for all movies in library",
            at_front=True,
        )
        flash(
            "Refreshing Criterion Collection information for all movies in library",
            "info",
        )
        return redirect(url_for("main.maintenance"))

    # Form to update the TMDB data for the entire library, both movies and TV shows

    tmdb_refresh_form = TMDBRefreshForm()
    if tmdb_refresh_form.tmdb_refresh.data and tmdb_refresh_form.validate_on_submit():
        # Records detached from TMDB (#207) are left out: refresh_tmdb_info
        # would decline them anyway, and a title search is exactly what
        # detaching them was meant to prevent

        movies = (
            Movie.query.filter(Movie.tmdb_ignored == False)
            .order_by(Movie.title.asc(), Movie.year.asc())
            .all()
        )
        tv_shows = (
            TVSeries.query.filter(TVSeries.tmdb_ignored == False)
            .order_by(TVSeries.title.asc())
            .all()
        )

        # On the user-request queue: each job is a TMDB API call plus
        # artwork downloads, and thousands of them would starve the single
        # sql worker of import work for the whole run

        for movie in movies:
            current_app.request_queue.enqueue(
                "app.videos.refresh_tmdb_info",
                args=("Movies", movie.id, movie.tmdb_id),
                job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                description=f"Refreshing TMDB data for '{movie.title} ({movie.year})'",
            )

        for tv in tv_shows:
            current_app.request_queue.enqueue(
                "app.videos.refresh_tmdb_info",
                args=("TV Shows", tv.id, tv.tmdb_id),
                job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                description=f"Refreshing TMDB data for '{tv.title}'",
            )

        flash("Refreshing TMDB information for entire library", "info")
        return redirect(url_for("main.maintenance"))

    sync_form = SyncAWSStorageForm()
    if sync_form.sync_submit.data and sync_form.validate_on_submit():
        if not current_user.admin:
            flash("Need to be an admin user for this task!", "danger")

        elif current_user.check_password(sync_form.password.data):
            current_app.sql_queue.enqueue(
                "app.videos.sync_aws_s3_storage_task",
                args=None,
                job_timeout="24h",
                description="Syncing files from AWS S3 storage",
                at_front=True,
            )
            flash("Syncing files with AWS S3 storage", "info")

        else:
            flash("Incorrect password provided!", "danger")

        return redirect(url_for("main.maintenance"))

    # Form to rescan metadata for all the files

    metadata_scan_form = TrackMetadataScanForm()

    if metadata_scan_form.scan_submit.data and metadata_scan_form.validate_on_submit():
        current_app.sql_queue.enqueue(
            "app.videos.track_metadata_scan_library",
            args=(),
            job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
            description="Scanning track metadata for all files in the library",
        )
        flash("Scanning track metadata for all files in the library", "info")
        return redirect(url_for("main.maintenance"))

    import_form = ImportForm()
    if import_form.submit.data and import_form.validate_on_submit():
        enqueue_import_scan(
            current_app.request_queue,
            description="Manually scanning import directory for files",
            at_front=True,
        )
        current_app.logger.info("Manually scanning import directory for files")
        flash("Manually scanning import directory for files", "info")
        return redirect(url_for("main.maintenance"))

    # Form to merge a group of movies that share a TMDB id: each duplicate
    # is fed through refresh_tmdb_info, whose merge path (serialized with
    # the import pipeline by title locks) moves files and reviews to the
    # oldest record and deletes the duplicate

    movie_merge_form = MovieMergeForm()
    if movie_merge_form.merge_submit.data and movie_merge_form.validate_on_submit():
        merge_tmdb_id = int(movie_merge_form.merge_tmdb_id.data)
        group = (
            Movie.query.filter_by(tmdb_id=merge_tmdb_id)
            .order_by(Movie.date_created.asc())
            .all()
        )
        if len(group) < 2:
            flash("No duplicates found for that TMDB id.", "danger")

        else:
            canonical = group[0]
            for duplicate in group[1:]:
                current_app.request_queue.enqueue(
                    "app.videos.refresh_tmdb_info",
                    args=("Movies", duplicate.id, merge_tmdb_id),
                    job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                    description=(
                        f"Merging '{duplicate.title} ({duplicate.year})' into "
                        f"'{canonical.title} ({canonical.year})'"
                    ),
                )
            flash(
                f"Merging {len(group) - 1} duplicate(s) into "
                f"'{canonical.title} ({canonical.year})'",
                "info",
            )
        return redirect(url_for("main.maintenance"))

    # Form to preview how a filename would be parsed and filed on import

    filename_test_form = FilenameTestForm()
    filename_test_result = None
    if (
        filename_test_form.filename_test_submit.data
        and filename_test_form.validate_on_submit()
    ):
        test_filename = filename_test_form.test_filename.data.strip()
        filename_test_result = {
            "filename": test_filename,
            "details": evaluate_filename(test_filename, log=False),
        }

    from app.tv_validation import validation_report

    return render_template(
        "maintenance.html",
        title="Library Maintenance",
        rejected_count=len(_rejected_files()),
        subtitle_triage_count=len(forced_subtitle_candidates()),
        lossy_triage_count=len(lossy_audio_candidates()),
        tmdb_triage_count=sum(len(bucket) for bucket in _tmdb_unmatched()),
        runtime_mismatch_count=len(runtime_mismatch_candidates()),
        tv_suspect_count=sum(1 for e in validation_report() if e["suspect"]),
        duplicate_groups=_duplicate_movie_groups(),
        movie_merge_form=movie_merge_form,
        filename_test_form=filename_test_form,
        filename_test_result=filename_test_result,
        criterion_refresh_form=criterion_refresh_form,
        tmdb_refresh_form=tmdb_refresh_form,
        sync_form=sync_form,
        metadata_scan_form=metadata_scan_form,
        import_form=import_form,
    )


@bp.route("/maintenance/tv-titles")
@login_required
@admin_required
def tv_title_validation():
    """Per-series episode-title verdicts: how well TMDB's
    titles agree with Plex's for the same files, suspects first."""

    from app.tv_validation import MIN_COMPARED, SUSPECT_BELOW, validation_report

    return render_template(
        "tv_validation.html",
        title="TV episode titles",
        entries=validation_report(),
        min_compared=MIN_COMPARED,
        suspect_below=SUSPECT_BELOW,
    )


@bp.route("/maintenance/runtime", methods=["GET", "POST"])
@login_required
@admin_required
def runtime_triage():
    """Triage files whose estimated length disagrees with their film's
    TMDb runtime (#234) — the shape of a title collision at capture
    time, or a truncated download. The page lists the estimate's
    ingredients; Acknowledge accepts a known-benign mismatch (a
    full-disc rip, a deliberately longer recording) so it stops
    reappearing. Re-importing the file clears the acknowledgement."""

    form = RuntimeMismatchForm()
    if form.acknowledge_submit.data and form.validate_on_submit() and form.file_id.data:
        file = db.session.get(File, form.file_id.data)
        if file is None:
            flash("That file no longer exists.", "warning")
            return redirect(url_for("main.runtime_triage"))
        file.runtime_mismatch_reviewed = datetime.now()
        db.session.commit()
        flash(
            f"Acknowledged '{file.plex_title}' — its length is accepted "
            f"as-is until the file is replaced",
            "success",
        )
        return redirect(url_for("main.runtime_triage"))

    return render_template(
        "runtime_triage.html",
        title="Runtime mismatches",
        candidates=runtime_mismatch_candidates(),
        runtime_form=RuntimeMismatchForm(),
    )


def _tmdb_unmatched():
    """The records still sitting at a NULL tmdb_id without the ignored
    flag — the rows the TMDB triage page (#226) exists to empty, and
    the population the maintenance page's bulk refresh would otherwise
    answer with a blind title search."""

    movies = (
        Movie.query.filter(Movie.tmdb_id.is_(None), Movie.tmdb_ignored.isnot(True))
        .order_by(Movie.title.asc())
        .all()
    )
    series = (
        TVSeries.query.filter(
            TVSeries.tmdb_id.is_(None), TVSeries.tmdb_ignored.isnot(True)
        )
        .order_by(TVSeries.title.asc())
        .all()
    )
    return movies, series


@bp.route("/maintenance/tmdb", methods=["GET", "POST"])
@login_required
@admin_required
def tmdb_triage():
    """Triage records with no TMDB match (#226): every movie and series
    still at a NULL tmdb_id without the ignored flag, with per-row
    actions — flag it as unmatchable through the Remove button's clear
    path, or match it to an id entered by hand. Every action removes a
    row, so the list is naturally self-emptying; a newly imported
    record only appears here if its title search missed."""

    form = TMDBTriageForm()
    if form.validate_on_submit() and (form.flag_submit.data or form.lookup_submit.data):
        record = None
        library = None
        if form.movie_id.data:
            record = db.session.get(Movie, form.movie_id.data)
            library = "Movies"
        elif form.series_id.data:
            record = db.session.get(TVSeries, form.series_id.data)
            library = "TV Shows"

        # A record that gained an id (or the flag) since the page
        # rendered has left the list — never flag a matched record

        if record is None or record.tmdb_id is not None or record.tmdb_ignored:
            flash("That record isn't awaiting TMDB triage.", "warning")
            return redirect(url_for("main.tmdb_triage"))

        display = (
            f"{record.title} ({record.year})" if library == "Movies" else record.title
        )

        if form.flag_submit.data:
            if library == "Movies":
                record.tmdb_movie_clear()
            else:
                record.tmdb_tv_clear()
            db.session.commit()
            flash(
                f"Flagged '{display}' as unmatchable; no refresh will "
                f"search TMDB for it again",
                "success",
            )
            return redirect(url_for("main.tmdb_triage"))

        if form.tmdb_id.data is None:
            flash(f"Enter a TMDB ID to match '{display}'.", "warning")
            return redirect(url_for("main.tmdb_triage"))

        # The movie/TV pages' by-hand match, minus their redirect
        # gymnastics: enqueue the refresh at the front of the queue and
        # give it a moment to apply so the row is gone on reload. If
        # the id already belongs to another record, the refresh's merge
        # path folds this one into it

        refresh_job = current_app.sql_queue.enqueue(
            "app.videos.refresh_tmdb_info",
            args=(library, record.id, form.tmdb_id.data),
            job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
            description=f"Refreshing TMDB data for '{display}'",
            at_front=True,
        )
        waited_seconds = 0
        while refresh_job.result is None and waited_seconds < 10:
            time.sleep(1)
            waited_seconds = waited_seconds + 1
        if refresh_job.result:
            flash(f"Matched '{display}' to TMDB id {form.tmdb_id.data}", "success")
        else:
            flash(
                f"Still refreshing TMDB data for '{display}' — reload in " f"a moment",
                "info",
            )
        return redirect(url_for("main.tmdb_triage"))

    movies, series = _tmdb_unmatched()
    return render_template(
        "tmdb_triage.html",
        title="Unmatched TMDB records",
        movies=movies,
        series=series,
        triage_form=TMDBTriageForm(),
    )


@bp.route("/maintenance/subtitles", methods=["GET", "POST"], defaults={"file_id": None})
@bp.route("/maintenance/subtitles/<int:file_id>", methods=["GET", "POST"])
@login_required
@admin_required
def subtitle_triage(file_id):
    """Triage subtitle tracks that look forced but aren't flagged.

    A file can hide more than one forced track, so candidates carry
    checkboxes and the selected set is flagged in one mkvpropedit
    invocation, preserving the file's current defaults; dismissing
    marks the whole file's subtitles as reviewed. Either action retires
    the file's inspection aids.

    With a file_id the page shows ONE file's candidates — the
    all-files page loads every pending file's snapshots at once, so
    the per-file view is the fast path from a file's own page. An
    `origin` query param carries where the visitor came from; actions
    redirect back there.
    """

    # Only ever bounce to a local path — an absolute or scheme-relative
    # origin would be an open redirect

    origin = request.args.get("origin", "", type=str)
    if not origin.startswith("/") or origin.startswith("//"):
        origin = None

    def done():
        """After a successful action: back to the origin page, or the
        triage list the form lived on."""

        return redirect(origin or url_for("main.subtitle_triage", file_id=file_id))

    def stay():
        """After a refused action: back to the same triage view,
        keeping the origin for the next attempt."""

        return redirect(url_for("main.subtitle_triage", file_id=file_id, origin=origin))

    triage_form = SubtitleTriageForm()

    if triage_form.mark_forced_submit.data and triage_form.validate_on_submit():
        file = File.query.filter_by(id=triage_form.file_id.data).first_or_404()
        track_ids = request.form.getlist("track_ids", type=int)
        tracks = FileSubtitleTrack.query.filter(
            FileSubtitleTrack.id.in_(track_ids or [0]),
            FileSubtitleTrack.file_id == file.id,
        ).all()
        if not tracks:
            flash("Select at least one track to flag as forced.", "warning")
            return stay()

        if file.container != "Matroska":
            flash(
                f"'{file.basename}' isn't an MKV file, so its subtitle flags "
                f"can't be edited in place.",
                "danger",
            )
            return stay()
        if not os.path.isfile(
            os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
        ):
            flash(f"'{file.basename}' is not present locally.", "warning")
            return stay()

        # Preserve the file's current selections, adding the selected
        # tracks to the forced set — one mkvpropedit invocation per file

        audio_default = FileAudioTrack.query.filter_by(
            file_id=file.id, default=True
        ).first()
        default_audio_track = str(audio_default.track) if audio_default else None
        subtitle_default = FileSubtitleTrack.query.filter_by(
            file_id=file.id, default=True
        ).first()
        default_subtitle_track = (
            str(subtitle_default.track) if subtitle_default else None
        )
        forced_tracks = sorted(
            {
                str(existing.track)
                for existing in FileSubtitleTrack.query.filter_by(
                    file_id=file.id, forced=True
                )
            }
            | {str(track.track) for track in tracks},
            key=int,
        )

        current_app.file_queue.enqueue(
            "app.videos.mkvpropedit_task",
            args=(
                file.id,
                default_audio_track,
                default_subtitle_track,
                forced_tracks,
            ),
            job_timeout=current_app.config["MKVPROPEDIT_TASK_TIMEOUT"],
            description=f"'{file.basename}'",
        )
        remove_triage_snapshots(file.id)
        numbers = ", ".join(
            str(track.track) for track in sorted(tracks, key=lambda t: t.track)
        )
        flash(
            f"Marking track{'s' if len(tracks) != 1 else ''} {numbers} of "
            f"'{file.basename}' as forced",
            "info",
        )
        return done()

    if triage_form.dismiss_submit.data and triage_form.validate_on_submit():
        file = File.query.filter_by(id=triage_form.file_id.data).first_or_404()
        file.subtitle_triage_reviewed = datetime.now()
        db.session.commit()
        remove_triage_snapshots(file.id)
        flash(f"Marked '{file.basename}' subtitles as reviewed", "success")
        return done()

    focus_file = (
        File.query.filter_by(id=file_id).first_or_404() if file_id is not None else None
    )
    candidates = forced_subtitle_candidates(file_id=file_id)

    # The inspection aids (cue timelines, burned-in snapshots) are the
    # expensive part, and only the per-file view renders them —
    # the all-files page is just the worklist of links

    if focus_file:
        for entry in candidates:
            for item in entry["tracks"]:
                item["aids"] = triage_presentation(
                    entry["file"].id, item["track"].track
                )

    return render_template(
        "subtitle_triage.html",
        title=(
            f'Possibly-forced subtitles in "{focus_file.basename}"'
            if focus_file
            else "Possibly-forced subtitles"
        ),
        candidates=candidates,
        focus_file=focus_file,
        origin=origin,
        triage_form=triage_form,
    )


@bp.route(
    "/maintenance/lossy-audio", methods=["GET", "POST"], defaults={"file_id": None}
)
@bp.route("/maintenance/lossy-audio/<int:file_id>", methods=["GET", "POST"])
@login_required
@admin_required
def lossy_audio_triage(file_id):
    """Triage files whose first audio track is lossy while a lossless
    track rides behind (#212).

    Promoting a lossless track enqueues the same mkvpropedit task the
    file page's default-audio radio uses — a non-first default audio
    track triggers the remux that puts it in the lead — preserving the
    file's subtitle defaults and forced flags; "Keep as-is" records
    that the pairing is not redundant (a commentary, say) so the file
    stops reappearing. The listening-clip comparison (#223) is the
    evidence for that call, generated proactively on import and on
    demand here.

    Same page split as the subtitle triage: the all-files page is the
    worklist, the per-file view carries the clips and forms, and an
    `origin` query param bounces actions back where the visitor
    came from.
    """

    origin = request.args.get("origin", "", type=str)
    if not origin.startswith("/") or origin.startswith("//"):
        origin = None

    def done():
        return redirect(origin or url_for("main.lossy_audio_triage", file_id=file_id))

    def stay():
        return redirect(
            url_for("main.lossy_audio_triage", file_id=file_id, origin=origin)
        )

    triage_form = LossyAudioTriageForm()

    if triage_form.promote_submit.data and triage_form.validate_on_submit():
        file = File.query.filter_by(id=triage_form.file_id.data).first_or_404()
        chosen = request.form.get("lossless_track", type=int)
        track = FileAudioTrack.query.filter_by(
            file_id=file.id, track=chosen or 0
        ).first()
        if track is None or track.compression_mode != "Lossless":
            flash("Select a lossless track to lead.", "warning")
            return stay()

        if file.container != "Matroska":
            flash(
                f"'{file.basename}' isn't an MKV file, so its tracks can't "
                f"be reordered in place.",
                "danger",
            )
            return stay()
        if not os.path.isfile(
            os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
        ):
            flash(f"'{file.basename}' is not present locally.", "warning")
            return stay()

        # Preserve the file's current subtitle selections; the promoted
        # track becomes the default audio, and mkvpropedit's remux pass
        # moves it into the lead

        subtitle_default = FileSubtitleTrack.query.filter_by(
            file_id=file.id, default=True
        ).first()
        default_subtitle_track = (
            str(subtitle_default.track) if subtitle_default else None
        )
        forced_tracks = sorted(
            {
                str(existing.track)
                for existing in FileSubtitleTrack.query.filter_by(
                    file_id=file.id, forced=True
                )
            },
            key=int,
        )

        current_app.file_queue.enqueue(
            "app.videos.mkvpropedit_task",
            args=(
                file.id,
                str(track.track),
                default_subtitle_track,
                forced_tracks,
            ),
            job_timeout=current_app.config["MKVPROPEDIT_TASK_TIMEOUT"],
            description=f"'{file.basename}'",
        )

        # The remux renumbers tracks, so every aid set pictures streams
        # that are about to move — drop them all

        remove_triage_snapshots(file.id)
        flash(
            f"Remuxing '{file.basename}' with track {track.track} "
            f"({track.codec}) in the lead",
            "info",
        )
        return done()

    if triage_form.dismiss_submit.data and triage_form.validate_on_submit():
        file = File.query.filter_by(id=triage_form.file_id.data).first_or_404()
        file.lossy_audio_reviewed = datetime.now()
        db.session.commit()
        remove_audio_comparison(file.id)
        flash(f"Marked '{file.basename}' audio as reviewed", "success")
        return done()

    if triage_form.generate_submit.data and triage_form.validate_on_submit():
        file = File.query.filter_by(id=triage_form.file_id.data).first_or_404()
        if not os.path.isfile(
            os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
        ):
            flash(f"'{file.basename}' is not present locally.", "warning")
            return stay()
        current_app.transcode_queue.enqueue(
            "app.triage.generate_audio_comparison",
            args=(file.id,),
            job_timeout="2h",
            description=f"Audio comparison for '{file.basename}'",
        )
        flash(
            f"Generating listening clips for '{file.basename}' — they'll "
            f"appear here once the transcode queue gets to it",
            "info",
        )
        return stay()

    focus_file = (
        File.query.filter_by(id=file_id).first_or_404() if file_id is not None else None
    )
    candidates = lossy_audio_candidates(file_id=file_id)

    # The listening clips are the expensive part, and only the per-file
    # view renders them — the all-files page is just the worklist

    if focus_file:
        for entry in candidates:
            entry["comparison"] = lossy_audio_presentation(entry["file"].id)
            entry["local"] = os.path.isfile(
                os.path.join(current_app.config["LIBRARY_DIR"], entry["file"].file_path)
            )

    return render_template(
        "lossy_audio_triage.html",
        title=(
            f'Lossy-audio lead in "{focus_file.basename}"'
            if focus_file
            else "Lossy-audio leads"
        ),
        candidates=candidates,
        focus_file=focus_file,
        origin=origin,
        triage_form=triage_form,
    )


def _duplicate_movie_groups():
    """Movies sharing a TMDB id, each group oldest-first.

    The oldest record is the one refresh_tmdb_info keeps when merging, so
    the first movie in each group is the survivor.
    """

    duplicated_ids = [
        tmdb_id
        for (tmdb_id,) in db.session.query(Movie.tmdb_id)
        .filter(Movie.tmdb_id != None)
        .group_by(Movie.tmdb_id)
        .having(db.func.count(Movie.id) > 1)
        .all()
    ]
    if not duplicated_ids:
        return []

    groups = {}
    for movie in (
        Movie.query.filter(Movie.tmdb_id.in_(duplicated_ids))
        .order_by(Movie.tmdb_id.asc(), Movie.date_created.asc())
        .all()
    ):
        groups.setdefault(movie.tmdb_id, []).append(movie)
    return list(groups.values())


def _rejected_files():
    """Every real file under the rejects directory, newest first."""

    rejects_dir = os.path.realpath(current_app.config["REJECTS_DIR"])
    entries = []
    for dirpath, dirnames, filenames in os.walk(rejects_dir):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            full_path = os.path.join(dirpath, name)
            try:
                stats = os.stat(full_path)
            except OSError:
                continue
            relative_path = os.path.relpath(full_path, rejects_dir)
            entries.append(
                {
                    "path": relative_path,
                    "basename": name,
                    "reason": os.path.dirname(relative_path) or "unknown",
                    "size": stats.st_size,
                    # ctime, not mtime: the move into the rejects tree
                    # updates the inode change time, while both rename
                    # and copy2 PRESERVE the file's own (possibly
                    # years-old) mtime — and the SMB share refuses
                    # utime, so stamping at reject time isn't an
                    # option (the Army of Darkness report)
                    "rejected_at": datetime.fromtimestamp(stats.st_ctime, timezone.utc),
                }
            )
    entries.sort(key=lambda entry: entry["rejected_at"], reverse=True)
    return entries


@bp.route("/rejects", methods=["GET", "POST"])
@login_required
@admin_required
def rejects():
    """Triage rejected files: send them back for re-import, or delete them.

    Re-importing is just a move into the import directory — the filesystem
    watcher and the hourly sweep take it from there.
    """

    rejects_dir = os.path.realpath(current_app.config["REJECTS_DIR"])
    form = RejectActionForm()

    if form.validate_on_submit():
        # The posted path must resolve to a real file inside the rejects
        # directory: no traversal, no symlink escapes

        requested = os.path.realpath(os.path.join(rejects_dir, form.file_path.data))
        if not requested.startswith(rejects_dir + os.sep) or not os.path.isfile(
            requested
        ):
            flash("That file no longer exists in the rejects directory.", "danger")
            return redirect(url_for("main.rejects"))

        basename = os.path.basename(requested)

        if form.delete_submit.data:
            os.remove(requested)
            current_app.logger.info(f"'{basename}' Deleted from the rejects directory")
            flash(f"Deleted '{basename}'.", "success")

        else:
            destination = os.path.join(current_app.config["IMPORT_DIR"], basename)
            if os.path.exists(destination):
                flash(
                    f"'{basename}' already exists in the import directory; "
                    f"not overwriting it.",
                    "danger",
                )
                return redirect(url_for("main.rejects"))
            shutil.move(requested, destination)
            current_app.logger.info(
                f"'{basename}' Moved from rejects to the import directory"
            )
            flash(f"Moved '{basename}' to the import directory.", "success")

        # Tidy the reason folder if this was its last file (never the
        # rejects directory itself)

        reason_dir = os.path.dirname(requested)
        if reason_dir != rejects_dir:
            try:
                os.rmdir(reason_dir)
            except OSError:
                pass

        return redirect(url_for("main.rejects"))

    return render_template(
        "rejects.html",
        title="Rejected files",
        rejected=_rejected_files(),
        form=form,
    )


@bp.route("/queue")
@login_required
def queue():
    """Show a list of all localization and transcode tasks in queue.

    See api.queue_details for how the queue is generated.
    """

    return render_template("queue.html", title="Queue")


@bp.route("/library/files", methods=["GET", "POST"])
@login_required
def files():
    """Show a list of all the files in the library."""

    page = request.args.get("page", 1, type=int)
    q = request.args.get("q", None, type=str)
    quality = request.args.get("quality", "0", type=str)
    audio = request.args.get("audio", None, type=str)

    movie_rank = (
        db.session.query(
            File.id,
            movie_file_rank(),
        )
        .join(Movie, (Movie.id == File.movie_id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .subquery()
    )

    tv_rank = (
        db.session.query(
            File.id,
            tv_file_rank(),
        )
        .join(TVSeries, (TVSeries.id == File.series_id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .subquery()
    )

    files_with_lossless = (
        db.session.query(FileAudioTrack.file_id)
        .filter(FileAudioTrack.compression_mode == "Lossless")
        .subquery()
    )

    # A lossy FIRST track flags a file — except the E-AC-3 Atmos twin
    # leading the Atmos pipeline's trio (#55b), which is deliberate:
    # DD+ Atmos first for Apple TV passthrough, the lossless original
    # riding behind. Those files are configured exactly as wanted, so
    # they're not lossless-upgrade candidates (#212). atmos imports
    # lazily like its other callers — it resolves the worker app
    # singleton at module import time

    from app.atmos import EAC3_ATMOS_CODEC

    lossy_files = (
        db.session.query(FileAudioTrack.file_id)
        .filter(FileAudioTrack.track == 1)
        .filter(FileAudioTrack.compression_mode != "Lossless")
        .filter(
            db.or_(
                FileAudioTrack.codec.is_(None),
                FileAudioTrack.codec != EAC3_ATMOS_CODEC,
            )
        )
        .subquery()
    )

    if q and int(quality) > 0:
        this_quality = RefQuality.query.filter_by(id=int(quality)).first_or_404()
        title = f"{this_quality.quality_title} files matching '{q}'"
        q = q.replace(" ", "%")
        files = (
            db.session.query(
                File,
                RefQuality,
                RefFeatureType,
                Movie,
                TVSeries,
                db.case(
                    (movie_rank.c.rank == 1, 1), (tv_rank.c.rank == 1, 1), else_=0
                ).label("rank"),
            )
            .join(RefQuality, (RefQuality.id == File.quality_id))
            .outerjoin(RefFeatureType, (RefFeatureType.id == File.feature_type_id))
            .outerjoin(Movie, (Movie.id == File.movie_id))
            .outerjoin(TVSeries, (TVSeries.id == File.series_id))
            .outerjoin(movie_rank, (movie_rank.c.id == File.id))
            .outerjoin(tv_rank, (tv_rank.c.id == File.id))
            .filter(File.basename.ilike(f"%{q}%"))
            .filter(RefQuality.id == int(quality))
            .order_by(
                File.media_library,
                db.func.regexp_replace(
                    db.case(
                        (Movie.tmdb_title != None, Movie.tmdb_title),
                        else_=Movie.title,
                    ),
                    "^(The|A|An) ",
                    "",
                ).asc(),
                db.case(
                    (Movie.tmdb_title != None, Movie.tmdb_release_date),
                    else_=Movie.year,
                ).asc(),
                File.edition.asc(),
                RefFeatureType.feature_type.asc(),
                db.func.regexp_replace(
                    db.case(
                        (TVSeries.tmdb_name != None, TVSeries.tmdb_name),
                        else_=TVSeries.title,
                    ),
                    "^(The|A|An) ",
                    "",
                ).asc(),
                File.season.asc(),
                File.episode.asc(),
                File.last_episode.asc(),
                RefQuality.preference.asc(),
                File.basename.asc(),
            )
            .paginate(page=page, per_page=1000, error_out=False)
        )

    elif q:
        title = f"Files matching '{q}'"
        q = q.replace(" ", "%")
        files = (
            db.session.query(
                File,
                RefQuality,
                RefFeatureType,
                Movie,
                TVSeries,
                db.case(
                    (movie_rank.c.rank == 1, 1), (tv_rank.c.rank == 1, 1), else_=0
                ).label("rank"),
            )
            .join(RefQuality, (RefQuality.id == File.quality_id))
            .outerjoin(RefFeatureType, (RefFeatureType.id == File.feature_type_id))
            .outerjoin(Movie, (Movie.id == File.movie_id))
            .outerjoin(TVSeries, (TVSeries.id == File.series_id))
            .outerjoin(movie_rank, (movie_rank.c.id == File.id))
            .outerjoin(tv_rank, (tv_rank.c.id == File.id))
            .filter(File.basename.ilike(f"%{q}%"))
            .order_by(
                File.media_library,
                db.func.regexp_replace(
                    db.case(
                        (Movie.tmdb_title != None, Movie.tmdb_title),
                        else_=Movie.title,
                    ),
                    "^(The|A|An) ",
                    "",
                ).asc(),
                db.case(
                    (Movie.tmdb_title != None, Movie.tmdb_release_date),
                    else_=Movie.year,
                ).asc(),
                File.edition.asc(),
                RefFeatureType.feature_type.asc(),
                db.func.regexp_replace(
                    db.case(
                        (TVSeries.tmdb_name != None, TVSeries.tmdb_name),
                        else_=TVSeries.title,
                    ),
                    "^(The|A|An) ",
                    "",
                ).asc(),
                File.season.asc(),
                File.episode.asc(),
                File.last_episode.asc(),
                RefQuality.preference.asc(),
                File.basename.asc(),
            )
            .paginate(page=page, per_page=1000, error_out=False)
        )

    elif int(quality) > 0:
        this_quality = RefQuality.query.filter_by(id=int(quality)).first_or_404()
        title = f"{this_quality.quality_title} files"
        files = (
            db.session.query(
                File,
                RefQuality,
                RefFeatureType,
                Movie,
                TVSeries,
                db.case(
                    (movie_rank.c.rank == 1, 1), (tv_rank.c.rank == 1, 1), else_=0
                ).label("rank"),
            )
            .join(RefQuality, (RefQuality.id == File.quality_id))
            .outerjoin(RefFeatureType, (RefFeatureType.id == File.feature_type_id))
            .outerjoin(Movie, (Movie.id == File.movie_id))
            .outerjoin(TVSeries, (TVSeries.id == File.series_id))
            .outerjoin(movie_rank, (movie_rank.c.id == File.id))
            .outerjoin(tv_rank, (tv_rank.c.id == File.id))
            .filter(RefQuality.id == int(quality))
            .order_by(
                File.media_library,
                db.func.regexp_replace(
                    db.case(
                        (Movie.tmdb_title != None, Movie.tmdb_title),
                        else_=Movie.title,
                    ),
                    "^(The|A|An) ",
                    "",
                ).asc(),
                db.case(
                    (Movie.tmdb_title != None, Movie.tmdb_release_date),
                    else_=Movie.year,
                ).asc(),
                File.edition.asc(),
                RefFeatureType.feature_type.asc(),
                db.func.regexp_replace(
                    db.case(
                        (TVSeries.tmdb_name != None, TVSeries.tmdb_name),
                        else_=TVSeries.title,
                    ),
                    "^(The|A|An) ",
                    "",
                ).asc(),
                File.season.asc(),
                File.episode.asc(),
                File.last_episode.asc(),
                RefQuality.preference.asc(),
                File.basename.asc(),
            )
            .paginate(page=page, per_page=1000, error_out=False)
        )

    elif audio == "lossy":
        title = "Files that have lossy first audio tracks"
        files = (
            db.session.query(
                File,
                RefQuality,
                RefFeatureType,
                Movie,
                TVSeries,
                db.case(
                    (movie_rank.c.rank == 1, 1), (tv_rank.c.rank == 1, 1), else_=0
                ).label("rank"),
            )
            .join(RefQuality, (RefQuality.id == File.quality_id))
            .outerjoin(RefFeatureType, (RefFeatureType.id == File.feature_type_id))
            .outerjoin(Movie, (Movie.id == File.movie_id))
            .outerjoin(TVSeries, (TVSeries.id == File.series_id))
            .outerjoin(movie_rank, (movie_rank.c.id == File.id))
            .outerjoin(tv_rank, (tv_rank.c.id == File.id))
            .filter(File.id.in_(files_with_lossless))
            .filter(File.id.in_(lossy_files))
            .order_by(
                File.media_library,
                db.func.regexp_replace(
                    db.case(
                        (Movie.tmdb_title != None, Movie.tmdb_title),
                        else_=Movie.title,
                    ),
                    "^(The|A|An) ",
                    "",
                ).asc(),
                db.case(
                    (Movie.tmdb_title != None, Movie.tmdb_release_date),
                    else_=Movie.year,
                ).asc(),
                File.edition.asc(),
                RefFeatureType.feature_type.asc(),
                db.func.regexp_replace(
                    db.case(
                        (TVSeries.tmdb_name != None, TVSeries.tmdb_name),
                        else_=TVSeries.title,
                    ),
                    "^(The|A|An) ",
                    "",
                ).asc(),
                File.season.asc(),
                File.episode.asc(),
                File.last_episode.asc(),
                RefQuality.preference.asc(),
                File.basename.asc(),
            )
            .paginate(page=page, per_page=1000, error_out=False)
        )

    else:
        title = "All Files"
        files = (
            db.session.query(
                File,
                RefQuality,
                RefFeatureType,
                Movie,
                TVSeries,
                db.case(
                    (movie_rank.c.rank == 1, 1), (tv_rank.c.rank == 1, 1), else_=0
                ).label("rank"),
            )
            .join(RefQuality, (RefQuality.id == File.quality_id))
            .outerjoin(RefFeatureType, (RefFeatureType.id == File.feature_type_id))
            .outerjoin(Movie, (Movie.id == File.movie_id))
            .outerjoin(TVSeries, (TVSeries.id == File.series_id))
            .outerjoin(movie_rank, (movie_rank.c.id == File.id))
            .outerjoin(tv_rank, (tv_rank.c.id == File.id))
            .order_by(
                File.media_library,
                db.func.regexp_replace(
                    db.case(
                        (Movie.tmdb_title != None, Movie.tmdb_title),
                        else_=Movie.title,
                    ),
                    "^(The|A|An) ",
                    "",
                ).asc(),
                db.case(
                    (Movie.tmdb_title != None, Movie.tmdb_release_date),
                    else_=Movie.year,
                ).asc(),
                File.edition.asc(),
                RefFeatureType.feature_type.asc(),
                db.func.regexp_replace(
                    db.case(
                        (TVSeries.tmdb_name != None, TVSeries.tmdb_name),
                        else_=TVSeries.title,
                    ),
                    "^(The|A|An) ",
                    "",
                ).asc(),
                File.season.asc(),
                File.episode.asc(),
                File.last_episode.asc(),
                RefQuality.preference.asc(),
                File.basename.asc(),
            )
            .paginate(page=page, per_page=1000, error_out=False)
        )

    next_url = (
        url_for("main.files", page=files.next_num, quality=quality)
        if files.has_next
        else None
    )
    prev_url = (
        url_for("main.files", page=files.prev_num, quality=quality)
        if files.has_prev
        else None
    )

    filter_form = QualityFilterForm()

    qualities = (
        db.session.query(RefQuality.id, RefQuality.quality_title)
        .join(File, (File.quality_id == RefQuality.id))
        .distinct()
        .filter(File.movie_id != None)
        .filter(File.feature_type_id == None)
        .order_by(RefQuality.preference.asc())
        .all()
    )
    filter_form.quality.choices = [("0", "All")] + [
        (str(id), title) for (id, title) in qualities
    ]

    filter_form.quality.default = quality

    if filter_form.validate_on_submit():
        return redirect(url_for("main.files", q=q, quality=filter_form.quality.data))

    filter_form.process()

    library_search_form = LibrarySearchForm()
    if library_search_form.validate_on_submit():
        return redirect(
            url_for(
                "main.files",
                q=library_search_form.search_query.data,
                quality=filter_form.quality.data,
            )
        )

    return render_template(
        "files.html",
        title=title,
        files=files.items,
        next_url=next_url,
        prev_url=prev_url,
        pages=files,
        filter_form=filter_form,
        library_search_form=library_search_form,
    )
