"""Serve the operations pages (the split from routes.py).

The pages are the system health, the scheduled tasks, the library
maintenance, the triage pages, the queue, and the pipeline trails."""

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

# Flask 2.4 removed flask.Markup. Import it from its real module
from flask_login import current_user, login_required

from app.tasks import (
    bulk_job_id,
    enqueue_task,
    free_job_id,
    requeue_job_id,
    start_bulk_run,
)
from app import db, enqueue_import_scan, local_path, safe_job_id
from app.main.forms import (
    ArchivedRestoreForm,
    CriterionRefreshForm,
    FailedJobForm,
    FilenameTestForm,
    HeldImportForm,
    LossyAudioTriageForm,
    ManualImportForm,
    QueueDismissForm,
    QueueFrontForm,
    MissingRestoreForm,
    MissingScanForm,
    SubtitleTriageForm,
    ImportForm,
    LibrarySearchForm,
    MovieMergeForm,
    RejectActionForm,
    ResetSettingForm,
    SettingsForm,
    SyncAWSStorageForm,
    QualityFilterForm,
    RuntimeMismatchForm,
    TMDBRefreshForm,
    TMDBTriageForm,
    TrackMetadataScanForm,
)
from app.models import (
    MOVIES,
    ArchivedOriginal,
    File,
    FileAudioTrack,
    FileSubtitleTrack,
    HeldImport,
    Movie,
    RefFeatureType,
    RefQuality,
    TVSeries,
    ranked_movie_files,
    ranked_tv_files,
    title_sort,
)
from app.main import bp
from app.main.helpers import admin_required, decimal_id, decimal_ids
from app.main.library import restore_cost_estimate
from app.log_digest import stored_digest
from app.maintenance import system_health
from app.manual_imports import (
    STARTED_KEY,
    STARTED_TTL,
    PENDING_KEY,
    blocked_downloads,
    download_folders,
    fetch_queue,
    fitzflix_basename,
    pending_count,
    pending_report,
    remove_from_queue,
    started,
    video_candidates,
)
from app.missing_files import missing_best_report, missing_best_summary
from app.queue_page import listed_queues
from app.queues import roster_queues
from app.settings import (
    SETTINGS,
    clear_setting,
    default_value,
    locked,
    resolve,
    set_setting,
    value_problem,
)
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
    """Show the system status: health, worker and scheduler state, and failed jobs."""

    queues_by_name = {queue.name: queue for queue in roster_queues(current_app)}

    # This form requeues or forgets a failed background job

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

            # The stored Result of rq 2 has the structured error. exc_info
            # stays as the fallback for older failures

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
    # The job timestamps of rq 2 are timezone-aware. Thus, the fallback for
    # a missing date must also be aware. Otherwise, the sort cannot compare
    # them

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
        log_digest=stored_digest(current_app.redis),
    )


def _scheduled_tasks():
    """Return the status of the recurring scheduled tasks, for the polled fragment.

    The schedulers share 1 set of scheduled jobs. Thus, this function
    filters the results of each scheduler to its own queue.
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
                    # rq.cron records the enqueue times. Thus, "last ran"
                    # now means "last started", not "last finished"
                    "last_run": _naive_utc(cron_job.latest_enqueue_time),
                    "next_run": _naive_utc(next_run),
                    "next_run_text": _next_run_text(_naive_utc(next_run)),
                }
            )

    # The most frequent task is first (order requested by Glenn). The
    # every-X-minutes tasks sort by X. The hourly tasks sort by minute.
    # The daily tasks sort by time. The weekly tasks sort by day and
    # time. The monthly tasks sort by day of month and time.

    scheduled_tasks.sort(key=lambda task: _cron_frequency_key(task["cron_string"]))
    return scheduled_tasks


# cron counts the day of week from Sunday. It also accepts 7 as a
# second spelling of Sunday

_WEEKDAY_NAMES = (
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
)

# This is the wording of a schedule inside the hour: "Twice hourly at
# :20 and :50". A count above 12 keeps the numeral ("13 times hourly")

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
    """Return readable text for a 5-field cron string.

    Examples: "Daily at 1:45 AM", "Four times hourly at :03, :18, :33,
    and :48". This function generates the text. It does not look it
    up. The map that it replaced (in app/__init__.py) was 9 schedules
    behind the cron table. A lookup stays correct only if the person who
    adds a job remembers to add a second line here. If the rules below
    do not cover the grammar (ranges, hour steps, a month field), the
    function returns the raw cron string. That string is short but
    never wrong.
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
    """Return the text for a schedule whose hour field is a wildcard.

    The text is a step ("Every 10 minutes") or the minutes of the
    schedule ("Hourly at :30"). Return None if the field uses syntax
    other than a step or a plain list."""

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
    """Return the plain integers that a cron field lists.

    Return None if the field uses syntax other than a comma-separated
    list (a wildcard, a range, or a step)."""

    try:
        return [int(value) for value in field.split(",")]
    except ValueError:
        return None


def _and_list(items):
    """Join the items in the style of the schedule text.

    The text is "a and b" for 2 items, and a list with an Oxford comma
    for 3 or more items."""

    if len(items) == 2:
        return " and ".join(items)
    if len(items) > 2:
        return ", ".join(items[:-1]) + f", and {items[-1]}"
    return "".join(items)


def _clock_text(hour, minute):
    """Return a 24-hour cron time in the style of the page.

    The text is "1:45 AM", or a word for the 2 times that have one.
    Return None if a field is out of range."""

    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    if minute == 0 and hour == 0:
        return "midnight"
    if minute == 0 and hour == 12:
        return "noon"
    return f"{hour % 12 or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"


def _ordinal(number):
    """Return the ordinal of a day of month for a monthly schedule.

    Examples: 1 -> "1st", 18 -> "18th"."""

    if 11 <= number % 100 <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def _naive_utc(when):
    """Convert a timezone-aware UTC datetime to naive UTC.

    rq.cron returns timezone-aware UTC datetimes. The relative-time and
    tooltip renderers use naive UTC, like the rest of rq."""

    if when is None:
        return None
    if when.tzinfo is not None:
        return when.astimezone(timezone.utc).replace(tzinfo=None)
    return when


def _cron_frequency_key(cron_string):
    """Return the sort key for the scheduled-tasks table.

    The frequency class comes first (every-X-minutes, hourly, daily,
    weekly, monthly). Then comes the parameter of the class (X, the
    minute, the time, or the day and time)."""

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
    """Render a naive-UTC timestamp in the local zone of the server.

    The rq job and scheduler times are naive UTC. The mouseover
    tooltips show them in the local zone of the server. The server is
    in the same household as its viewers. Thus, it is in the same
    timezone. The format matches the LLL format of moment.js. Thus,
    the browser-local tooltips of the queue table and these
    server-local tooltips look the same."""

    if when is None:
        return ""
    local = when.replace(tzinfo=timezone.utc).astimezone()
    return local.strftime("%B %-d, %Y %-I:%M %p")


def _next_run_text(next_run):
    """Render the next-run time of a task. Never call it the past.

    The stored time of a due job is in the past until the next tick of
    the scheduler (60 s interval). That tick moves the job onto the
    queue and computes the next run. Thus, the 5 s poll frequently
    sees a value a little in the past. Those values are "due now". A
    value older than 2 ticks means that the scheduler stalled. The
    badge of the health card also shows this.
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
    """Return coarse relative-time text.

    Examples: '4 minutes ago', or 'in 4 minutes' for a future time such
    as the next run of a task. Each poll renders the health fragment
    again. Thus, the server-side text stays current without
    flask-moment. The scripts of flask-moment would not run again
    inside swapped-in HTML in any case.
    """

    if moment_dt.tzinfo is None:
        moment_dt = moment_dt.replace(tzinfo=timezone.utc)
    # Use round(), not int(). Truncation toward zero would undercount the
    # future spans ("in 3 days" minus 1 microsecond is still 3 days, not
    # 2)
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
    """Return the live health fragment that the poller of the System page swaps in.

    All content rendered here reads Redis or the local filesystem. The
    external-service badges come from the snapshot of the health_probe
    task. Thus, the polling makes no external traffic.
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


def _in_page_range(name, value):
    """Return the value of a setting inside the limits of its page field.

    A limit can become smaller after a value was stored. For example,
    the DVR genre channels allowed 50 before #397. The page validates
    each field on a save. Thus, a stored value outside the limits would
    block the save of every other setting. The field shows the nearest
    limit, and the next save stores it. The stored value stays until
    that save. An info line records the change."""

    setting = SETTINGS[name]
    if setting.kind != "int" or not isinstance(value, int):
        return value
    clamped = value
    if setting.maximum is not None:
        clamped = min(clamped, setting.maximum)
    if setting.minimum is not None:
        clamped = max(clamped, setting.minimum)
    if clamped != value:
        current_app.logger.info(
            f"Settings: {name} is {value}, outside the page limits. "
            f"The field shows {clamped}"
        )
    return clamped


@bp.route("/settings", methods=["GET", "POST"])
@login_required
@admin_required
def settings():
    """Change the runtime settings. A setting that .env sets is locked.

    A saved value gets to each web and worker process in some seconds,
    without a restart. The page stores only the values that change."""

    locked_names = [name for name in SETTINGS if locked(name)]

    reset_form = ResetSettingForm()
    if reset_form.reset_submit.data:
        name = reset_form.setting_name.data
        if not reset_form.validate_on_submit():
            flash("The form is not valid. Load the page again.", "danger")
        elif name not in SETTINGS:
            flash("That setting does not exist.", "danger")
        elif name in locked_names:
            current_app.logger.warning(
                f"Settings: {current_user.email} tried to reset {name}, "
                f"which .env sets"
            )
            flash(f"{name} is set in .env. Change it there.", "danger")
        elif clear_setting(name, current_user.email):
            flash(f"{name} is now the default value.", "info")
        else:
            flash(f"{name} already has the default value.", "info")
        return redirect(url_for("main.settings"))

    form = SettingsForm()
    if form.save_submit.data:
        # A browser does not send a disabled field. Thus, a locked name in
        # the request is an attempt to change a setting that .env sets.

        tampered = [name for name in locked_names if name in request.form]
        if tampered:
            current_app.logger.warning(
                f"Settings: {current_user.email} tried to change "
                f"{', '.join(tampered)}, which .env sets"
            )
            flash(f"{', '.join(tampered)} is set in .env. Change it there.", "danger")
            return redirect(url_for("main.settings"))

    for name in locked_names:
        del form[name]

    if form.save_submit.data and form.validate_on_submit():
        changes = {}
        for name in SETTINGS:
            if name in locked_names:
                continue
            value = form[name].data
            if value == resolve(name)[0]:
                continue
            problem = value_problem(name, value)
            if problem:
                form[name].errors.append(problem)
            changes[name] = value

        if not any(form[name].errors for name in changes):
            for name, value in changes.items():
                set_setting(name, value, current_user.email)
            if changes:
                flash(
                    f"Saved {len(changes)} change{'s' if len(changes) != 1 else ''}.",
                    "info",
                )
            else:
                flash("There are no changes to save.", "info")
            return redirect(url_for("main.settings"))
        flash("Nothing is saved. Correct the settings that show an error.", "danger")
    elif form.save_submit.data and form.csrf_token.errors:
        flash("The form is not valid. Load the page again.", "danger")
        return redirect(url_for("main.settings"))
    elif form.save_submit.data:
        flash("Nothing is saved. Correct the settings that show an error.", "danger")
    elif request.method == "GET":
        for name in SETTINGS:
            if name not in locked_names:
                form[name].data = _in_page_range(name, resolve(name)[0])

    rows = []
    for name, setting in SETTINGS.items():
        value, source = resolve(name)
        rows.append(
            {
                "setting": setting,
                "value": value,
                "source": source,
                "default": default_value(name),
                "field": None if name in locked_names else form[name],
            }
        )

    return render_template(
        "settings.html",
        title="Settings",
        form=form,
        reset_form=reset_form,
        rows=rows,
    )


# The job id prefixes of the bulk maintenance runs (#439). A post
# queues nothing while a job of its run waits or runs.

CRITERION_RUN = "bulk-criterion-refresh-"
TMDB_RUN = "bulk-tmdb-refresh-"
SYNC_RUN = "bulk-s3-sync-"
TRACK_SCAN_RUN = "bulk-track-scan-"
MISSING_SCAN_RUN = "bulk-missing-scan-"
ALREADY_QUEUED = "That run is already queued or running. Nothing new was queued."


def _refresh_criterion_post(form):
    """Queue the Criterion Collection refresh of the whole movie library.

    This job goes on the maintenance queue with the timeout of the
    monthly cron row and the CLI (#343). A full Wikidata pass takes
    longer than the request queue allows, and it must not hold the
    user-request worker."""

    name = "app.videos.refresh_criterion_collection_info"
    if not start_bulk_run(CRITERION_RUN, name):
        flash(ALREADY_QUEUED, "info")
        return
    enqueue_task(
        name,
        description="Refreshing Criterion Collection information for all movies in library",
        job_id=bulk_job_id(CRITERION_RUN),
    )
    flash(
        "Refreshing Criterion Collection information for all movies in library",
        "info",
    )


def _refresh_tmdb_post(form):
    """Queue a TMDB refresh of each movie and each TV show of the library.

    This excludes the records detached from TMDB (#207).
    refresh_tmdb_info would refuse them in any case. The detachment
    exists to prevent a title search. These jobs go on the user-request
    queue. Each job is a TMDB API call plus artwork downloads. Thousands
    of them would block the import work of the single sql worker for the
    whole run."""

    if not start_bulk_run(TMDB_RUN, "app.videos.refresh_tmdb_info"):
        flash(ALREADY_QUEUED, "info")
        return
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
    for movie in movies:
        enqueue_task(
            "app.videos.refresh_tmdb_info",
            "Movies",
            movie.id,
            movie.tmdb_id,
            description=f"Refreshing TMDB data for '{movie.title} ({movie.year})'",
            job_id=bulk_job_id(TMDB_RUN),
        )
    for tv in tv_shows:
        enqueue_task(
            "app.videos.refresh_tmdb_info",
            "TV Shows",
            tv.id,
            tv.tmdb_id,
            description=f"Refreshing TMDB data for '{tv.title}'",
            job_id=bulk_job_id(TMDB_RUN),
        )
    flash("Refreshing TMDB information for entire library", "info")


def _sync_storage_post(form):
    """Queue the S3 sync after a password check.

    The sync runs for hours. The maintenance queue holds it, as in the
    cron table. On the single sql worker it blocked the cataloging of
    each import (#314)."""

    if not current_user.check_password(form.password.data):
        flash("Incorrect password provided!", "danger")
        return
    name = "app.videos.sync_aws_s3_storage_task"
    # A sync that waits for idle queues books its retry under this id.
    if not start_bulk_run((SYNC_RUN, requeue_job_id(name)), name):
        flash(ALREADY_QUEUED, "info")
        return
    enqueue_task(
        name,
        description="Syncing files with AWS S3 storage",
        job_id=bulk_job_id(SYNC_RUN),
    )
    flash("Syncing files with AWS S3 storage", "info")


def _scan_library_post(form):
    """Queue a scan of the track metadata of each file of the library."""

    names = (
        "app.videos.track_metadata_scan_library",
        "app.videos.track_metadata_scan_task",
    )
    if not start_bulk_run(TRACK_SCAN_RUN, *names):
        flash(ALREADY_QUEUED, "info")
        return
    enqueue_task(
        names[0],
        description="Scanning track metadata for all files in the library",
        job_id=bulk_job_id(TRACK_SCAN_RUN),
    )
    flash("Scanning track metadata for all files in the library", "info")


def _scan_imports_post(form):
    """Queue a scan of the import directory, at the front of the request queue."""

    enqueue_import_scan(
        current_app.request_queue,
        description="Manually scanning import directory for files",
        at_front=True,
    )
    current_app.logger.info("Manually scanning import directory for files")
    flash("Manually scanning import directory for files", "info")


def _merge_movies_post(form):
    """Merge a group of movies that share a TMDB id into the oldest record.

    Each duplicate goes through refresh_tmdb_info. Its merge path moves
    the files and the reviews to the oldest record and deletes the
    duplicate. Title locks serialize the merge path with the import
    pipeline."""

    merge_tmdb_id = decimal_id(form.merge_tmdb_id.data)
    group = (
        Movie.query.filter_by(tmdb_id=merge_tmdb_id)
        .order_by(Movie.date_created.asc())
        .all()
        if merge_tmdb_id is not None
        else []
    )
    if len(group) < 2:
        flash("No duplicates found for that TMDB id.", "danger")
        return
    canonical = group[0]
    for duplicate in group[1:]:
        enqueue_task(
            "app.videos.refresh_tmdb_info",
            "Movies",
            duplicate.id,
            merge_tmdb_id,
            description=f"Merging '{duplicate.title} ({duplicate.year})' into "
            f"'{canonical.title} ({canonical.year})'",
        )
    flash(
        f"Merging {len(group) - 1} duplicate(s) into "
        f"'{canonical.title} ({canonical.year})'",
        "info",
    )


@bp.route("/maintenance", methods=["GET", "POST"])
@login_required
@admin_required
def maintenance():
    """Show the library maintenance page.

    The page has the rejected-file triage, the duplicate movies, the
    filename tester, and the bulk operations for the whole library. Each
    form of the page has a handler. A valid post of a form runs its
    handler and goes back to the page. The filename test is not in the
    table. It renders its result on the page."""

    criterion_refresh_form = CriterionRefreshForm()
    tmdb_refresh_form = TMDBRefreshForm()
    sync_form = SyncAWSStorageForm()
    metadata_scan_form = TrackMetadataScanForm()
    import_form = ImportForm()
    movie_merge_form = MovieMergeForm()

    handlers = (
        (
            criterion_refresh_form,
            criterion_refresh_form.criterion_refresh,
            _refresh_criterion_post,
        ),
        (tmdb_refresh_form, tmdb_refresh_form.tmdb_refresh, _refresh_tmdb_post),
        (sync_form, sync_form.sync_submit, _sync_storage_post),
        (metadata_scan_form, metadata_scan_form.scan_submit, _scan_library_post),
        (import_form, import_form.submit, _scan_imports_post),
        (movie_merge_form, movie_merge_form.merge_submit, _merge_movies_post),
    )
    for form, submit, handler in handlers:
        if submit.data and form.validate_on_submit():
            handler(form)
            return redirect(url_for("main.maintenance"))

    # This form shows how Fitzflix would parse and file a filename on
    # import

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

    return render_template(
        "maintenance.html",
        title="Library Maintenance",
        rejected_count=len(_rejected_files()),
        subtitle_triage_count=len(forced_subtitle_candidates()),
        lossy_triage_count=len(lossy_audio_candidates()),
        tmdb_triage_count=sum(len(bucket) for bucket in _tmdb_unmatched()),
        runtime_mismatch_count=len(runtime_mismatch_candidates()),
        missing_best=missing_best_summary(current_app.redis),
        manual_import_count=pending_count(current_app.redis),
        held_import_count=HeldImport.query.count(),
        archived_original_count=ArchivedOriginal.query.count(),
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


@bp.route("/maintenance/missing", methods=["GET", "POST"])
@login_required
@admin_required
def missing_files():
    """List the best-quality files that have no local copy (#274).

    The page reads the stored result of the nightly scan. Scan now runs
    the scan again on the maintenance queue. The restore form requests
    a restore from AWS for each selected file, after a password check.
    That is the same path as the restore button of the file page. A
    restore costs real money. Thus, the page shows the cost estimate
    of the whole list."""

    scan_form = MissingScanForm()
    if scan_form.missing_scan_submit.data and scan_form.validate_on_submit():
        name = "app.missing_files.missing_best_files_task"
        if not start_bulk_run(MISSING_SCAN_RUN, name):
            flash(ALREADY_QUEUED, "info")
            return redirect(url_for("main.missing_files"))
        enqueue_task(
            name,
            description="Scanning for best files that have no local copy",
            at_front=True,
            job_id=bulk_job_id(MISSING_SCAN_RUN),
        )
        flash("Scanning the library for best files that have no local copy", "info")
        return redirect(url_for("main.missing_files"))

    restore_form = MissingRestoreForm()
    if restore_form.missing_restore_submit.data and restore_form.validate_on_submit():
        if not current_user.check_password(restore_form.password.data):
            flash("Incorrect password provided!", "danger")
            return redirect(url_for("main.missing_files"))

        selected = decimal_ids(request.form.getlist("file_id"))
        files = (
            File.query.filter(File.id.in_(selected), File.aws_key.isnot(None)).all()
            if selected
            else []
        )

        # The list comes from the last nightly scan. A file can be back
        # since then. A second restore pays again, and it can overwrite or
        # import the file again. Thus, only a file that is still missing
        # is restored.

        present = [
            file
            for file in files
            if os.path.isfile(
                os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
            )
        ]
        files = [file for file in files if file not in present]
        if present:
            flash(
                f"Skipped {len(present)} file{'s' if len(present) != 1 else ''} "
                f"that {'are' if len(present) != 1 else 'is'} in the library again.",
                "info",
            )
        if not files:
            flash("No archived files were selected.", "warning")
            return redirect(url_for("main.missing_files"))

        for file in files:
            enqueue_task(
                "app.videos.aws_restore",
                file.restore_key,
                description=f"'{file.untouched_basename}'",
            )
        estimate = restore_cost_estimate(files)
        flash(
            f"Requesting {len(files)} file{'s' if len(files) != 1 else ''} to be "
            f"restored from AWS Glacier (≈ ${estimate['cost']:.2f})",
            "info",
        )
        return redirect(url_for("main.missing_files"))

    report = missing_best_report(current_app.redis)
    archived = (
        [row["file"] for row in report["rows"] if row["archived"]] if report else []
    )
    return render_template(
        "missing_files.html",
        title="Missing best copies",
        report=report,
        restore_estimate=restore_cost_estimate(archived),
        scan_form=scan_form,
        restore_form=MissingRestoreForm(),
    )


@bp.route("/maintenance/held-imports", methods=["GET", "POST"])
@login_required
@admin_required
def held_imports():
    """Decide the conversions that wait because a real rip exists (#292).

    Import sends the original back through the import. Its conversion
    then replaces the library file. Archive only uploads the original
    to S3 and leaves the library alone. Discard moves the original to
    the rejects directory. The page also lists the archive-only
    originals in S3. A restore of one imports it again. If a real rip
    still exists, the import waits here again."""

    from app.importing import enqueue_localization
    from app.sdr_conversion import prune_held_imports
    from app.videos import move_to_rejects

    form = HeldImportForm()
    if (
        form.import_submit.data or form.archive_submit.data or form.discard_submit.data
    ) and form.validate_on_submit():
        held_id = decimal_id(form.held_id.data)
        held = db.session.get(HeldImport, held_id) if held_id is not None else None
        if held is None or not os.path.exists(held.held_path):
            flash("That file no longer waits for a decision.", "warning")
            return redirect(url_for("main.held_imports"))
        if held.decision:
            flash(f"'{held.untouched_basename}' is already in progress.", "info")
            return redirect(url_for("main.held_imports"))

        name = held.untouched_basename
        if form.discard_submit.data:
            move_to_rejects(held.held_path, "discarded")
            db.session.delete(held)
            db.session.commit()
            current_app.logger.info(
                f"'{name}' Held conversion discarded by {current_user.email}"
            )
            flash(f"Moved '{name}' to the rejects directory.", "success")
            return redirect(url_for("main.held_imports"))

        held.decision = "import" if form.import_submit.data else "archive"
        held.date_decided = datetime.now(timezone.utc)
        db.session.commit()
        if held.decision == "import":
            # The import reads the decision. Thus, the decision commits
            # first. If no job is queued, the decision goes back. Then the
            # admin can choose again (#434).
            if enqueue_localization(held.held_path, at_front=True) is None:
                held.decision = None
                held.date_decided = None
                db.session.commit()
                flash(
                    f"'{name}' is not queued. An import of the file waits or "
                    f"runs already. Try again later.",
                    "warning",
                )
                return redirect(url_for("main.held_imports"))
            flash(
                f"Importing '{name}'. Its conversion replaces the library file.",
                "info",
            )
        else:
            enqueue_task(
                "app.videos.archive_held_task", held.id, description=f"'{name}'"
            )
            flash(f"Archiving '{name}' to S3. The library does not change.", "info")
        current_app.logger.info(
            f"'{name}' Held conversion: {held.decision} chosen by {current_user.email}"
        )
        return redirect(url_for("main.held_imports"))

    restore_form = ArchivedRestoreForm()
    if restore_form.restore_submit.data and restore_form.validate_on_submit():
        if not current_user.check_password(restore_form.password.data):
            flash("Incorrect password provided!", "danger")
            return redirect(url_for("main.held_imports"))
        archived_id = decimal_id(restore_form.archived_id.data)
        archived = (
            db.session.get(ArchivedOriginal, archived_id)
            if archived_id is not None
            else None
        )
        if archived is None:
            flash("That original is no longer archived.", "warning")
            return redirect(url_for("main.held_imports"))
        enqueue_task(
            "app.videos.aws_restore",
            archived.aws_key,
            description=f"'{archived.untouched_basename}'",
        )
        flash(
            f"Requesting a restore of '{archived.untouched_basename}' from AWS. "
            f"It goes through the import when it arrives.",
            "info",
        )
        return redirect(url_for("main.held_imports"))

    prune_held_imports()
    return render_template(
        "held_imports.html",
        title="Held imports",
        held=HeldImport.query.order_by(HeldImport.date_held.asc()).all(),
        archived=ArchivedOriginal.query.order_by(
            ArchivedOriginal.date_added.desc()
        ).all(),
        form=form,
        restore_form=restore_form,
    )


@bp.route("/maintenance/manual-imports", methods=["GET", "POST"])
@login_required
@admin_required
def manual_imports():
    """Finish the downloads that Sonarr and Radarr cannot import (#276).

    The page reads the live queues of both apps. Import copies the
    chosen video file to the import directory under the given name.
    That runs on the file-operation queue. Then the task deletes the
    download and removes the queue items. Discard removes the queue items and tells the download
    client to delete the download. Every action checks the live queue
    again. Thus, a stale page cannot act on a download that a person
    solved in the app itself."""

    form = ManualImportForm()
    if form.validate_on_submit():
        service = form.service.data
        download_id = form.download_id.data
        if service not in ("Radarr", "Sonarr"):
            flash("Unknown service.", "danger")
            return redirect(url_for("main.manual_imports"))
        try:
            queue = fetch_queue(service)
        except Exception as e:
            flash(f"{service} did not answer: {e}", "danger")
            return redirect(url_for("main.manual_imports"))
        entry = next(
            (
                candidate
                for candidate in blocked_downloads(service, queue)
                if candidate["download_id"] == download_id
            ),
            None,
        )
        if entry is None:
            flash(f"That download is no longer in the {service} queue.", "warning")
            return redirect(url_for("main.manual_imports"))

        if form.discard_submit.data:
            try:
                remove_from_queue(service, download_id, delete_files=True)
            except Exception as e:
                flash(f"{service} refused the removal: {e}", "danger")
                return redirect(url_for("main.manual_imports"))
            current_app.redis.hdel(PENDING_KEY, download_id)
            current_app.logger.info(
                f"'{entry['title']}' Discarded from the {service} queue"
            )
            flash(
                f"Discarded '{entry['title']}'. {service} tells the download "
                f"client to delete it.",
                "success",
            )
            return redirect(url_for("main.manual_imports"))

        if started(current_app.redis, download_id):
            flash("That import is already queued.", "info")
            return redirect(url_for("main.manual_imports"))
        candidates = video_candidates(entry["output_path"])
        relative = form.source.data or ""
        if relative not in [candidate["relative"] for candidate in candidates]:
            flash(
                "That file is not a video file below ARR_DOWNLOAD_FOLDERS.",
                "danger",
            )
            return redirect(url_for("main.manual_imports"))
        new_name = (form.new_name.data or "").strip()
        if not fitzflix_basename(new_name):
            flash(
                "The name must be in the form 'Title (Year) - [Quality].mkv' or "
                "'Series - S01E02 - [Quality].mkv'.",
                "danger",
            )
            return redirect(url_for("main.manual_imports"))

        # A failed import keeps its job under the fixed id. Delete that
        # job first. Otherwise the retry stays in the failed jobs, and
        # "Forget" on the System page deletes the live retry (#438).

        job_id = safe_job_id(f"manual-import:{download_id}")
        if not free_job_id("app.manual_imports.manual_import_task", job_id):
            flash("That import is already queued.", "info")
            return redirect(url_for("main.manual_imports"))
        current_app.redis.set(
            STARTED_KEY.format(download_id=download_id), "1", ex=STARTED_TTL
        )
        enqueue_task(
            "app.manual_imports.manual_import_task",
            service,
            download_id,
            entry["output_path"],
            relative,
            new_name,
            description=f"'{new_name}'",
            job_id=job_id,
        )
        current_app.logger.info(
            f"'{new_name}' Manual import of '{relative}' requested by "
            f"{current_user.email}"
        )
        flash(f"Copying '{relative}' to the import directory as '{new_name}'.", "info")
        return redirect(url_for("main.manual_imports"))

    return render_template(
        "manual_imports.html",
        title="Manual imports",
        report=pending_report(),
        form=form,
        fenced=bool(download_folders()),
    )


@bp.route("/maintenance/runtime", methods=["GET", "POST"])
@login_required
@admin_required
def runtime_triage():
    """Triage the files whose estimated length differs from the TMDb runtime (#234).

    This is the shape of a title collision at capture time, or of a
    truncated download. The page lists the inputs of the estimate.
    Acknowledge accepts a mismatch that is known to be harmless (a
    full-disc rip, or a recording that is longer by design). Then the
    file no longer appears. A new import of the file clears the
    acknowledgement."""

    form = RuntimeMismatchForm()
    if form.acknowledge_submit.data and form.validate_on_submit() and form.file_id.data:
        file = db.session.get(File, form.file_id.data)
        if file is None:
            flash("That file no longer exists.", "warning")
            return redirect(url_for("main.runtime_triage"))
        file.runtime_mismatch_reviewed = datetime.now()
        db.session.commit()
        flash(
            f"Acknowledged '{file.plex_title}'. Its length is accepted "
            f"as it is until the file is replaced",
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
    """Return the records that still have a NULL tmdb_id and no ignored flag.

    These are the rows that the TMDB triage page (#226) exists to
    remove. Without the triage, the bulk refresh of the maintenance
    page would answer them with a blind title search."""

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
    """Triage the records with no TMDB match (#226).

    The page lists each movie and series that still has a NULL tmdb_id
    and no ignored flag. Each row has 2 actions. The first flags the
    record as unmatchable through the clear path of the Remove button.
    The second matches the record to an id typed by hand. Each action
    removes a row. Thus, the list empties itself. A new imported record
    appears here only if its title search found nothing."""

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

        # A record that got an id (or the flag) after the page rendered
        # is no longer on the list. Never flag a matched record

        if record is None or record.tmdb_id is not None or record.tmdb_ignored:
            flash("That record is not waiting for TMDB triage.", "warning")
            return redirect(url_for("main.tmdb_triage"))

        display = (
            f"{record.title} ({record.year})" if library == MOVIES else record.title
        )

        if form.flag_submit.data:
            if library == MOVIES:
                record.tmdb_movie_clear()
            else:
                record.tmdb_tv_clear()
            db.session.commit()
            flash(
                f"Flagged '{display}' as unmatchable. No refresh will "
                f"search TMDB for it again",
                "success",
            )
            return redirect(url_for("main.tmdb_triage"))

        if form.tmdb_id.data is None:
            flash(f"Enter a TMDB ID to match '{display}'.", "warning")
            return redirect(url_for("main.tmdb_triage"))

        # This is the by-hand match of the movie and TV pages, without
        # their complex redirects. Enqueue the refresh at the front of the
        # queue. Then give it a moment to apply. Thus, the row is gone on
        # reload. If the id already belongs to a different record, the
        # merge path of the refresh merges this record into it

        refresh_job = enqueue_task(
            "app.videos.refresh_tmdb_info",
            library,
            record.id,
            form.tmdb_id.data,
            apply_at_front=True,
            # The page waits for this refresh. Thus, it goes to the front
            # of the sql queue, not behind the bulk refreshes of the
            # request queue.
            queue="sql",
            description=f"Refreshing TMDB data for '{display}'",
            at_front=True,
        )
        waited_seconds = 0
        while refresh_job.result is None and waited_seconds < 10:
            time.sleep(1)
            waited_seconds = waited_seconds + 1
        # The refresh job only fetches. The apply runs next, at the front
        # of the sql queue.

        from app.tmdb_refresh import TMDB_NO_MATCH

        if refresh_job.result == TMDB_NO_MATCH:
            flash(
                f"TMDB has no data for id {form.tmdb_id.data}. '{display}' is "
                f"not matched",
                "warning",
            )
        elif refresh_job.result:
            flash(
                f"Fetched TMDB id {form.tmdb_id.data} for '{display}'. Fitzflix "
                f"applies the match next",
                "success",
            )
        else:
            flash(
                f"Still refreshing TMDB data for '{display}'. Reload in a moment",
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
    """Triage the subtitle tracks that look forced but have no flag.

    A file can hide more than 1 forced track. Thus, the candidates have
    checkboxes. One mkvpropedit call flags the selected set. It keeps
    the current defaults of the file. Dismiss marks the subtitles of
    the whole file as reviewed. Both actions delete the inspection aids
    of the file.

    With a file_id, the page shows the candidates of ONE file. The
    all-files page loads the snapshots of each pending file at the same
    time. Thus, the per-file view is the fast path from the page of a
    file. An `origin` query param records where the visitor came from.
    The actions redirect back to that page.
    """

    # Redirect only to a local path. An absolute or scheme-relative
    # origin would be an open redirect

    origin = request.args.get("origin", "", type=str)
    if not local_path(origin):
        origin = None

    def done():
        """Redirect after a successful action.

        The target is the origin page, or the triage list that had the
        form."""

        return redirect(origin or url_for("main.subtitle_triage", file_id=file_id))

    def stay():
        """Redirect after a refused action.

        The target is the same triage view. This keeps the origin for
        the next attempt."""

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
            flash("Select at least 1 track to flag as forced.", "warning")
            return stay()

        if file.container != "Matroska":
            flash(
                f"'{file.basename}' is not an MKV file. Fitzflix cannot edit "
                f"its subtitle flags in place.",
                "danger",
            )
            return stay()
        if not os.path.isfile(
            os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
        ):
            flash(f"'{file.basename}' is not present locally.", "warning")
            return stay()

        # Keep the current selections of the file. Add the selected tracks
        # to the forced set. This is 1 mkvpropedit call per file

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

        enqueue_task(
            "app.videos.mkvpropedit_task",
            file.id,
            default_audio_track,
            default_subtitle_track,
            forced_tracks,
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
    # expensive part. Only the per-file view renders them. The all-files
    # page is only the worklist of links

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
    """Triage the files whose first audio track is lossy with a lossless track behind it (#212).

    The promotion of a lossless track enqueues the same mkvpropedit
    task that the default-audio radio of the file page uses. A default
    audio track that is not first starts the remux that puts it in the
    lead. The task keeps the subtitle defaults and the forced flags of
    the file. "Keep as-is" records that the pair is not redundant (for
    example, a commentary). Then the file no longer appears. The
    listening-clip comparison (#223) is the evidence for that
    decision. Fitzflix makes it in advance on import, and on demand
    here.

    The page split is the same as in the subtitle triage. The all-files
    page is the worklist. The per-file view has the clips and the
    forms. An `origin` query param redirects the actions back to the
    page that the visitor came from.
    """

    origin = request.args.get("origin", "", type=str)
    if not local_path(origin):
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
                f"'{file.basename}' is not an MKV file. Fitzflix cannot "
                f"reorder its tracks in place.",
                "danger",
            )
            return stay()
        if not os.path.isfile(
            os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
        ):
            flash(f"'{file.basename}' is not present locally.", "warning")
            return stay()

        # Keep the current subtitle selections of the file. The promoted
        # track becomes the default audio. The remux pass of mkvpropedit
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

        enqueue_task(
            "app.videos.mkvpropedit_task",
            file.id,
            str(track.track),
            default_subtitle_track,
            forced_tracks,
            description=f"'{file.basename}'",
        )

        # The remux renumbers the tracks. Thus, each aid set shows streams
        # that will move. Delete all of them

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
        enqueue_task(
            "app.triage.generate_audio_comparison",
            file.id,
            description=f"Audio comparison for '{file.basename}'",
        )
        flash(
            f"Generating listening clips for '{file.basename}'. They appear "
            f"here when the transcode queue runs the job",
            "info",
        )
        return stay()

    focus_file = (
        File.query.filter_by(id=file_id).first_or_404() if file_id is not None else None
    )
    candidates = lossy_audio_candidates(file_id=file_id)

    # The listening clips are the expensive part. Only the per-file view
    # renders them. The all-files page is only the worklist

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
    """Return the groups of movies that share a TMDB id, oldest first in each group.

    refresh_tmdb_info keeps the oldest record when it merges. Thus, the
    first movie in each group is the one that stays.
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
    """Return each real file under the rejects directory, newest first."""

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
                    # Use ctime, not mtime. The move into the rejects
                    # tree updates the inode change time. But rename and
                    # copy2 both KEEP the mtime of the file. That mtime
                    # can be years old. The SMB share refuses utime. Thus,
                    # Fitzflix cannot set a time at reject time (the
                    # Army of Darkness report)
                    "rejected_at": datetime.fromtimestamp(stats.st_ctime, timezone.utc),
                }
            )
    entries.sort(key=lambda entry: entry["rejected_at"], reverse=True)
    return entries


@bp.route("/rejects", methods=["GET", "POST"])
@login_required
@admin_required
def rejects():
    """Triage the rejected files: send them back for import, or delete them.

    A new import is only a move into the import directory. The
    filesystem watcher and the hourly sweep do the rest.
    """

    rejects_dir = os.path.realpath(current_app.config["REJECTS_DIR"])
    form = RejectActionForm()

    if form.validate_on_submit():
        # The posted path must resolve to a real file inside the rejects
        # directory. No traversal and no symlink escape is permitted

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
                    f"'{basename}' already exists in the import directory. "
                    f"Fitzflix did not overwrite it.",
                    "danger",
                )
                return redirect(url_for("main.rejects"))
            shutil.move(requested, destination)
            current_app.logger.info(
                f"'{basename}' Moved from rejects to the import directory"
            )
            flash(f"Moved '{basename}' to the import directory.", "success")

        # Remove the reason folder if this was its last file. Never remove
        # the rejects directory

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
    """Show a list of all localization and transcode tasks in the queue.

    The server renders the rows from the same data that
    api.queue_details serves. Thus, the table is in place at the first
    paint. The 5-second poll then repaints the rows (renderAll in
    base.html) with the live state.
    """

    from app.pipeline import pipeline_trails, queue_rows

    details = current_user.get_queue_details()
    trails = pipeline_trails(current_app.redis, limit=100, journey_only=True)
    return render_template(
        "queue.html",
        title="Queue",
        rows=queue_rows(details["all"], trails),
        front_form=QueueFrontForm() if current_user.admin else None,
        dismiss_form=QueueDismissForm() if current_user.admin else None,
    )


@bp.route("/queue/dismiss", methods=["POST"])
@login_required
@admin_required
def queue_dismiss():
    """Remove a "no job in queue" row from the Queue page.

    The row is the pipeline trail of a file with no job left to list
    under. A trail with a live waiting job stays."""

    from app.pipeline import dismiss_trail

    form = QueueDismissForm()
    if not form.validate_on_submit():
        flash("The form is not valid. Load the page again.", "danger")
        return redirect(url_for("main.queue"))
    if not dismiss_trail(current_app.redis, form.digest.data):
        flash("That file has a job in a queue now. Its row stays.", "warning")
    return redirect(url_for("main.queue"))


@bp.route("/queue/front", methods=["POST"])
@login_required
@admin_required
def queue_front():
    """Move a waiting job to the front of its queue (#236).

    Only a job that waits in a queue that the queue page lists can move.
    The move is atomic. A job that a worker took before the request
    arrived does not move."""

    from app.pipeline import move_to_front

    form = QueueFrontForm()
    if not form.validate_on_submit():
        flash("The form is not valid. Load the page again.", "danger")
        return redirect(url_for("main.queue"))

    job_id = form.job_id.data
    try:
        job = Job.fetch(job_id, connection=current_app.redis)
    except NoSuchJobError:
        job = None

    queue = None
    if job is not None:
        for listed, only in listed_queues():
            if listed.name == job.origin and (only is None or job.func_name in only):
                queue = listed
                break

    index = move_to_front(queue, job_id) if queue is not None else None
    if index is None:
        flash(
            "That job does not wait in a queue now. A worker started it, or it is gone.",
            "warning",
        )
        return redirect(url_for("main.queue"))

    # Most import descriptions are the file name in single quotes

    name = str(job.meta.get("description", job.description) or job_id).strip("'")
    queue_label = queue.name.removeprefix("fitzflix-")
    flash(f"Moved '{name}' to the front of the {queue_label} queue.", "info")
    return redirect(url_for("main.queue"))


@bp.route("/library/files", methods=["GET", "POST"])
@login_required
@admin_required
def files():
    """Show a list of all the files in the library.

    This is an admin page (follow-up to #186). It is a file inventory
    of the whole library. Each row links to the file management pages."""

    page = request.args.get("page", 1, type=int)
    q = request.args.get("q", None, type=str)
    quality = request.args.get("quality", "0", type=str)

    # A stale or mistyped bookmark must not crash the page (#306).

    if not quality.isdecimal():
        quality = "0"
    audio = request.args.get("audio", None, type=str)

    movie_rank = ranked_movie_files()

    tv_rank = ranked_tv_files()

    files_with_lossless = (
        db.session.query(FileAudioTrack.file_id)
        .filter(FileAudioTrack.compression_mode == "Lossless")
        .subquery()
    )

    # A lossy FIRST track flags a file. The exception is the E-AC-3 Atmos
    # twin that leads the 3-track set of the Atmos pipeline (#55b). That
    # is by design. DD+ Atmos is first for Apple TV passthrough, and the
    # lossless original is behind it. Those files are configured exactly
    # as wanted. Thus, they are not lossless-upgrade candidates (#212).
    # The atmos import is lazy, like in its other callers. It resolves
    # the worker app singleton at module import time

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
    )

    # The search text and the quality combine. Without either, the audio
    # filter applies.

    this_quality = (
        RefQuality.query.filter_by(id=int(quality)).first_or_404()
        if int(quality) > 0
        else None
    )
    if q:
        title = (
            f"{this_quality.quality_title} files matching '{q}'"
            if this_quality
            else f"Files matching '{q}'"
        )
        files = files.filter(File.basename.ilike(f"%{q.replace(' ', '%')}%"))
        if this_quality:
            files = files.filter(RefQuality.id == int(quality))
    elif this_quality:
        title = f"{this_quality.quality_title} files"
        files = files.filter(RefQuality.id == int(quality))
    elif audio == "lossy":
        title = "Files that have lossy first audio tracks"
        files = files.filter(File.id.in_(files_with_lossless)).filter(
            File.id.in_(lossy_files)
        )
    else:
        title = "All Files"

    files = files.order_by(
        File.media_library,
        title_sort(Movie).asc(),
        db.case(
            (Movie.tmdb_title != None, Movie.tmdb_release_date),
            else_=Movie.year,
        ).asc(),
        File.edition.asc(),
        RefFeatureType.feature_type.asc(),
        title_sort(TVSeries).asc(),
        File.season.asc(),
        File.episode.asc(),
        File.last_episode.asc(),
        RefQuality.preference.asc(),
        File.basename.asc(),
    ).paginate(page=page, per_page=1000, error_out=False)

    # The links keep each filter. The search text comes from the request.

    search_text = request.args.get("q")
    filters = {"quality": quality, "q": search_text, "audio": audio}
    next_url = (
        url_for("main.files", page=files.next_num, **filters)
        if files.has_next
        else None
    )
    prev_url = (
        url_for("main.files", page=files.prev_num, **filters)
        if files.has_prev
        else None
    )

    filter_form = QualityFilterForm()

    # This page lists every file: movies, TV episodes, and extras. Thus,
    # the choices are the qualities of all files.

    qualities = (
        db.session.query(RefQuality.id, RefQuality.quality_title)
        .join(File, (File.quality_id == RefQuality.id))
        .distinct()
        .order_by(RefQuality.preference.asc())
        .all()
    )
    filter_form.quality.choices = [("0", "All")] + [
        (str(id), title) for (id, title) in qualities
    ]

    filter_form.quality.default = quality

    if filter_form.validate_on_submit():
        return redirect(
            url_for("main.files", q=search_text, quality=filter_form.quality.data)
        )

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
