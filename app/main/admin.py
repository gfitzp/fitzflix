"""Operations pages (#17's slice f): system health, scheduled
tasks, library maintenance, triage surfaces, the queue, and the
pipeline trails."""

import os
import shutil


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
    SubtitleTriageForm,
    ImportForm,
    LibrarySearchForm,
    MovieMergeForm,
    RejectActionForm,
    SyncAWSStorageForm,
    QualityFilterForm,
    TMDBRefreshForm,
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
    remove_triage_snapshots,
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

            # rq 2's stored Result carries the structured error (#23);
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

    cron_descriptions = {
        "0 0 * * *": "Daily at midnight",
        "30 0 * * *": "Daily at 12:30 AM",
        "0 1 * * 0": "Weekly on Sunday at 1:00 AM",
        "15 4 * * 1": "Weekly on Monday at 4:15 AM",
        "45 1 * * *": "Daily at 1:45 AM",
        "15 2 * * *": "Daily at 2:15 AM",
        "0 3 18 * *": "Monthly on the 18th at 3:00 AM",
        "0 4 1 * *": "Monthly on the 1st at 4:00 AM",
        "30 3 1 * *": "Monthly on the 1st at 3:30 AM",
        "0 * * * *": "Hourly",
        "30 * * * *": "Hourly at :30",
        "20,50 * * * *": "Twice hourly at :20 and :50",
        "*/10 * * * *": "Every 10 minutes",
        "*/15 * * * *": "Every 15 minutes",
        "* * * * *": "Every minute",
    }
    scheduled_tasks = []
    for scheduler in CronScheduler.all(current_app.redis):
        for cron_job in scheduler.get_jobs():
            meta = cron_job.job_options.get("meta") or {}
            cron_string = cron_job.cron or meta.get("cron_string", "")
            next_run = cron_job.next_enqueue_time or cron_job.get_next_enqueue_time()
            scheduled_tasks.append(
                {
                    "name": meta.get("description") or cron_job.func_name,
                    "schedule": cron_descriptions.get(cron_string, cron_string),
                    "cron_string": cron_string,
                    # rq.cron records enqueue times, so "last ran" means
                    # "last started" now, not "last finished"
                    "last_run": _naive_utc(cron_job.latest_enqueue_time),
                    "next_run": _naive_utc(next_run),
                    "next_run_text": _next_run_text(_naive_utc(next_run)),
                }
            )

    # Most-frequent first (#22, Glenn's ordering): every-X-minutes by X,
    # hourly by minute, daily by time, weekly by day and time, monthly by
    # day-of-month and time

    scheduled_tasks.sort(key=lambda task: _cron_frequency_key(task["cron_string"]))
    return scheduled_tasks


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

    # Form to update the TMDb data for the entire library, both movies and TV shows

    tmdb_refresh_form = TMDBRefreshForm()
    if tmdb_refresh_form.tmdb_refresh.data and tmdb_refresh_form.validate_on_submit():
        movies = Movie.query.order_by(Movie.title.asc(), Movie.year.asc()).all()
        tv_shows = TVSeries.query.order_by(TVSeries.title.asc()).all()

        # On the user-request queue: each job is a TMDb API call plus
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

        flash("Refreshing TMDb information for entire library", "info")
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

    # Form to merge a group of movies that share a TMDb id: each duplicate
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
            flash("No duplicates found for that TMDb id.", "danger")

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

    return render_template(
        "maintenance.html",
        title="Library Maintenance",
        rejected_count=len(_rejected_files()),
        subtitle_triage_count=len(forced_subtitle_candidates()),
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

    With a file_id the page shows ONE file's candidates (#72) — the
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
    # expensive part, and only the per-file view renders them (#75) —
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


def _duplicate_movie_groups():
    """Movies sharing a TMDb id, each group oldest-first.

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
                    # option (#71, the Army of Darkness report)
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


@bp.route("/maintenance/pipeline")
@login_required
@admin_required
def pipeline_activity():
    """The per-file pipeline trails (#18) on their own page — each
    recent file's journey through import as stage chips, filled by the
    same five-second poll as the queue page's tables."""

    return render_template("pipeline.html", title="Pipeline Activity")


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

    lossy_files = (
        db.session.query(FileAudioTrack.file_id)
        .filter(FileAudioTrack.track == 1)
        .filter(FileAudioTrack.compression_mode != "Lossless")
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
