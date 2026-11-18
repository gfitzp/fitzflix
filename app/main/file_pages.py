"""Serve the page of 1 file: its tracks, its archive, and its delete."""

import os
import traceback
from flask import current_app, render_template, flash, redirect, url_for, request
from flask_login import current_user, login_required
from app.tasks import enqueue_task, free_job_id, requeue_job_id
from app import db
from app.archive.restores import (
    refuse_while_restore_pending,
    request_restore,
    restore_would_replace_newer,
)
from app.main.forms import (
    FileDeleteForm,
    MKVMergeForm,
    MKVPropEditForm,
    S3DownloadForm,
    S3UploadForm,
    TrackMetadataScanForm,
)
from app.models import File, FileAudioTrack, FileSubtitleTrack, Movie, TVSeries
from app.auth.passkeys import identity_confirmed
from app.main import bp
from app.media.file_ranking import ranked_movie_files, tv_best_holder_ids
from app.main.helpers import admin_required
from app.media.custom_artwork import remove_custom_artwork
from app.media.triage import forced_subtitle_candidates, lossy_audio_candidates
from app.media.tracks.rewrite_guard import pop_stopped_edit
from app.archive.archive_keys import untouched_key_still_claimed
from app.media.tracks.languages import (
    language_names,
    file_language_choices,
    resolve_language_code,
)
from app.media import videos
from app.main import tv_pages
from app.operations.maintenance import clear_leftover_directory
from app.plex.plex_library import enqueue_plex_film_refresh


def _is_best_copy(file):
    """Return True if the file is the best copy of its title, or of an episode.

    Invariant: each page and each job gives the same answer (#738). A
    movie file has movie_file_rank 1. An episode file is the best holder
    of an episode number, by the rule of the S3 sync and the season
    grades (tv_best_holder_ids). That rule counts each episode of a file
    with 2 or more episodes. The file page offers the upload and the
    restore of a best copy only."""

    if file.movie_id:
        ranked = ranked_movie_files()
        return (
            db.session.query(ranked.c.id)
            .filter(ranked.c.id == file.id, ranked.c.rank == 1)
            .first()
            is not None
        )
    if file.series_id:
        return file.id in tv_best_holder_ids([file.series_id], file.season)
    return False


def changed_track_languages(posted, audio_tracks, subtitle_tracks):
    """Return (changes, unresolved) for the language boxes of the file page.

    posted maps a box name ("language_" and the track UID) to its value.
    changes maps a track UID to the new code of each track whose
    language changed (#611). unresolved holds the values that are not a
    language. A blank box is not a change. The comparison uses resolved
    codes. Thus, a box can still hold a stored ISO 639-2/T spelling
    ("deu"). That box does not read as a request to rewrite the track to
    the bibliographic spelling."""

    changes = {}
    unresolved = []
    for track in list(audio_tracks) + list(subtitle_tracks):
        submitted = posted.get(f"language_{track.uid}", "")
        if not submitted.strip():
            continue
        language = resolve_language_code(submitted)
        if not language:
            unresolved.append(submitted.strip())
        elif language != (resolve_language_code(track.language) or track.language):
            changes[track.uid] = language
    return changes, unresolved


def _propedit_form(audio_tracks, subtitle_tracks):
    """Return the form of the track flag edits, with the choices of the file.

    The values are the track UIDs (#611). They are strings, because
    WTForms coerces the submitted values to strings. An int here would
    fail the "valid choice" validation."""

    form = MKVPropEditForm()
    if audio_tracks:
        form.default_audio.choices = [
            (str(track.uid), track.label) for track in audio_tracks
        ]
        form.default_audio.default = next(
            (str(track.uid) for track in reversed(audio_tracks) if track.default),
            str(audio_tracks[0].uid),
        )
    else:
        # No audio tracks. Remove the field completely. Thus, its empty
        # radio group cannot fail validation and block subtitle-only
        # property edits. The template already does not render it,
        # through {% if audio_tracks %}.

        del form.default_audio

    subtitle_choices = [(str(track.uid), track.label) for track in subtitle_tracks]
    form.default_subtitle.choices = [("0", "None")] + subtitle_choices
    form.default_subtitle.default = next(
        (str(track.uid) for track in reversed(subtitle_tracks) if track.default),
        "0",
    )
    form.forced_subtitles.choices = subtitle_choices
    form.forced_subtitles.default = [
        str(track.uid) for track in subtitle_tracks if track.forced
    ]
    return form


def _remux_form(audio_tracks, subtitle_tracks):
    """Return the form of a remux without some tracks. Each track starts kept.

    The values are the track UIDs (#611)."""

    form = MKVMergeForm()
    form.audio_tracks.choices = [
        (str(track.uid), track.label) for track in audio_tracks
    ]
    form.audio_tracks.default = [str(track.uid) for track in audio_tracks]
    form.subtitle_tracks.choices = [
        (str(track.uid), track.label) for track in subtitle_tracks
    ]
    form.subtitle_tracks.default = [str(track.uid) for track in subtitle_tracks]
    return form


def _scan_file_post(file, form):
    """Queue a scan of the track metadata of the file (#695).

    The scan can remux the file to remove empty subtitle tracks. No web
    request rewrites a library file. Thus, a worker does the scan. The
    job has the id of the lock-busy retry of the scan. Thus, a second
    click while a scan waits or runs queues nothing new."""

    name = "app.media.tracks.scan.track_metadata_scan_task"
    job_id = requeue_job_id(name, int(file.id))
    if not free_job_id(name, job_id):
        flash(
            f"A scan of the track metadata of '{file.basename}' is already queued.",
            "info",
        )
        return
    enqueue_task(name, file.id, description=f"'{file.basename}'", job_id=job_id)
    flash(f"Queued a scan of the track metadata of '{file.basename}'", "info")


def _propedit_post(audio_tracks, subtitle_tracks):
    """Return the handler of the track flag and language edits of the file."""

    def handler(file, form):
        # The default_audio field is deleted from the form when the file
        # has no audio tracks. None tells the task that there is no
        # default to set.

        default_audio_track = form.default_audio.data if audio_tracks else None

        # The per-track language boxes go with the flag edits. Thus, 1
        # mkvpropedit pass and 1 scan cover both. They are rendered by
        # hand, not as form fields (there is 1 per track, like the
        # subtitle triage checkboxes). Only the tracks whose code changed
        # are sent. An untouched form leaves them as they are.

        track_languages, unresolved = changed_track_languages(
            request.form, audio_tracks, subtitle_tracks
        )
        if unresolved:
            flash(
                f"Unrecognized language "
                f"{'entries' if len(unresolved) > 1 else 'entry'} "
                f"{', '.join(repr(entry) for entry in unresolved)}. "
                f"No properties were changed.",
                "danger",
            )
            return None

        current_app.logger.debug(f"Default audio: {default_audio_track}")
        current_app.logger.debug(f"Default subtitle: {form.default_subtitle.data}")
        current_app.logger.debug(f"Forced subtitles: {form.forced_subtitles.data}")

        if file.container != "Matroska":
            flash(
                f"Unable to update MKV properties for '{file.basename}' because it is not an MKV file!",
                "danger",
            )
            return None
        mkvpropedit_job = enqueue_task(
            "app.media.tracks.edits.mkvpropedit_task",
            file.id,
            default_audio_track,
            form.default_subtitle.data,
            form.forced_subtitles.data,
            track_languages or None,
            description=f"'{file.basename}'",
        )
        if mkvpropedit_job:
            current_app.logger.info(f"Queued '{file.basename}' for MKV property edits")
        flash(f"Updating MKV properties for '{file.basename}'", "info")

    return handler


def _remux_post(file, form):
    """Queue a remux of the file with the kept tracks."""

    current_app.logger.info(f"Audio tracks: {form.audio_tracks.data}")
    current_app.logger.info(f"Subtitle tracks: {form.subtitle_tracks.data}")
    if file.container != "Matroska":
        flash(
            f"Unable to remux '{file.basename}' because it is not an MKV file!",
            "danger",
        )
        return
    mkvmerge_job = enqueue_task(
        "app.media.tracks.edits.mkvmerge_task",
        file.id,
        form.audio_tracks.data,
        form.subtitle_tracks.data,
        description=f"'{file.basename}'",
        at_front=True,
    )
    if mkvmerge_job:
        current_app.logger.info(f"Queued '{file.basename}' for MKV remuxing")
    flash(f"Remuxing MKV file '{file.basename}'", "info")


def _upload_post(file, form):
    """Queue an upload of the file to AWS S3 storage."""

    enqueue_task(
        "app.archive.uploads.upload_task",
        file.id,
        description=f"'{file.basename}'",
        at_front=True,
    )
    flash(f"Uploading '{file.basename}' to AWS S3 storage", "info")


def _restore_post(estimate):
    """Return the handler of a Glacier restore of the file at the estimated cost."""

    def handler(file, form):
        if not identity_confirmed(form):
            return
        if not file.restore_key:
            flash(f"'{file.basename}' has no archive in AWS to restore.", "warning")
            return
        if restore_would_replace_newer(file):
            flash(
                f"'{file.basename}' is newer than its archive in AWS. A restore "
                f"would replace it with the older copy. Archive the file again "
                f"first.",
                "warning",
            )
            return
        request_restore(file.restore_key, description=f"'{file.untouched_basename}'")
        flash(
            f"Requesting '{file.untouched_basename}' to be restored from AWS "
            f"Glacier (≈ ${estimate['cost']:.2f})",
            "info",
        )

    return handler


def _claimed_archive_keys(file):
    """Return each S3 key that the record of a file claims (#515).

    The untouched key, a #292 conversion key and a pending #369 move
    target count as claimed only while the row exists.
    A purge that deletes only the untouched key left the other 2 for
    the weekly prune."""

    keys = (file.aws_key, file.aws_transcoded_key, file.aws_key_target)
    return [key for key in dict.fromkeys(keys) if key]


# A delete holds the title lock of each file for this long, plus a
# time for each file of a series.

DELETE_LOCK_SECONDS = 120
DELETE_LOCK_SECONDS_PER_FILE = 5

DELETE_REFUSED = (
    "The title lock of this file is not free. A task works on this title. "
    "Delete the file when the task is done."
)


def _delete_locks(identifiers):
    """Take the title lock of each file of a delete. Return the locks, or None (#718).

    Invariant: a delete never runs while a task rewrites the file. A
    track edit holds the title lock of its file for its whole run. At
    its end, it puts its result at the path of the file. A delete in
    that time lets the task put the file back with no record. Thus, a
    lock that a task holds refuses the delete, and the locks that this
    call took are free again."""

    identifiers = list(identifiers)
    ttl_ms = 1000 * (
        DELETE_LOCK_SECONDS + DELETE_LOCK_SECONDS_PER_FILE * len(identifiers)
    )
    locks = []
    for identifier in identifiers:
        lock = current_app.lock_manager.lock(identifier, ttl_ms)
        if not lock:
            _release_delete_locks(locks)
            return None
        locks.append(lock)
    return locks


def _release_delete_locks(locks):
    """Free the title locks of a delete. A fault only logs. The locks expire."""

    for lock in locks:
        try:
            current_app.lock_manager.unlock(lock)
        except Exception:
            current_app.logger.warning(traceback.format_exc())


def _after_commit(step, *args, **kwargs):
    """Run 1 cleanup step of a committed delete. Log a failure and go on (follow-up #513).

    The rows are gone. A wedged share that raised in 1 step stopped the
    loop, and the AWS deletes and the folder cleanup never ran. Return
    the result of the step, or None after a failure."""

    try:
        return step(*args, **kwargs)
    except Exception:
        current_app.logger.warning(
            f"Delete cleanup step {getattr(step, '__name__', step)} failed: "
            + traceback.format_exc()
        )


def _files_left_message(what, file_paths):
    """Return the message of a delete whose library files did not all go (#745).

    The records are gone. The weekly sync imports a library file that
    has no record. Thus, the message names each file to remove by hand."""

    library = current_app.config["LIBRARY_DIR"]
    names = ", ".join(f"'{os.path.join(library, path)}'" for path in file_paths[:5])
    more = f" and {len(file_paths) - 5} more" if len(file_paths) > 5 else ""
    return (
        f"Removed {what} from the database, but the share did not delete "
        f"{names}{more}. Delete the files by hand. If they stay, the weekly "
        f"S3 sync imports them again."
    )


def _queue_archive_deletes(aws_keys):
    """Queue the S3 delete of each archive key that no surviving row claims.

    The delete reads the head first. A pending move target has no
    object yet. A plain delete wrote a delete marker there (follow-up
    #515). The task asks for the claim again when it runs (#686). The
    check here only keeps useless jobs out of the queue."""

    for aws_key in dict.fromkeys(aws_keys):
        if untouched_key_still_claimed(aws_key):
            continue
        enqueue_task(
            "app.archive.moves.aws_delete",
            aws_key,
            only_if_present=True,
            only_if_unclaimed=True,
            # A delete from a page uses the request workers. The handoff
            # queue can hold long library copies.
            queue="request",
        )


def _delete_refused_for_volumes(file_paths):
    """Return the text that refuses a delete while a library volume is away, or None (#752).

    Invariant: a delete page changes nothing while a volume of its
    library files is not mounted. A share that is not mounted leaves an
    empty folder at its mount point. The removal of a file then answers
    "no such file", and the file is still on the NAS. The records and
    the archives went, and the weekly sync imported each file again."""

    library = current_app.config["LIBRARY_DIR"]
    dead = videos._dead_volumes([os.path.join(library, path) for path in file_paths])
    if not dead:
        return None
    return (
        f"The volume {', '.join(dead)} is not mounted. Nothing was deleted. "
        f"Do the delete again when the volume is back."
    )


def _delete_file_post(file, form):
    """Delete the file and its record. Return the page to go to next.

    The delete holds the title lock of the file. A task that holds it
    refuses the delete (#718)."""

    refused = _delete_refused_for_volumes([file.file_path])
    if refused:
        flash(refused, "warning")
        return None
    locks = _delete_locks([file.file_identifier()])
    if locks is None:
        flash(DELETE_REFUSED, "warning")
        return None
    try:
        return _delete_file(file)
    finally:
        _release_delete_locks(locks)


def _delete_file(file):
    """Delete the file and its record under its title lock. Return the next page."""

    aws_keys = _claimed_archive_keys(file)
    file_id = file.id
    leftover_dir = os.path.join(current_app.config["LIBRARY_DIR"], file.dirname)

    file_path = file.file_path
    try:
        db.session.delete(file)
        db.session.commit()

    except Exception:
        db.session.rollback()
        flash(f"Unable to delete '{file.basename}'!", "danger")
        return None

    # The library file goes only after the commit, as in tv_delete (#513).

    removed = _after_commit(
        File.delete_library_copy, file_path, file_id, delete_directory_tree=True
    )

    # Delete the AWS copy only after the database delete is committed.
    # Thus, a failed commit cannot leave a database record whose backup is
    # gone.

    _queue_archive_deletes(aws_keys)

    # Clear the folder if only placed poster art and OS metadata
    # remain. A delete of the last file of a movie used to leave its
    # poster.jpg husk for the weekly sweep.

    _after_commit(clear_leftover_directory, leftover_dir)
    _after_commit(remove_custom_artwork, "file", file_id)

    # Invariant (#776): an extra that the delete removes also goes from
    # Plex. The scan of Plex does not see a file that goes from an extras
    # folder that stays. A refresh of the film does. A film folder that
    # went with the delete needs no refresh.

    if file.movie_id and file.feature_type_id:
        film_folder = os.path.dirname(leftover_dir)
        if os.path.isdir(film_folder):

            enqueue_plex_film_refresh(film_folder)

    if removed is True:
        flash(f"Deleted '{file.basename}' and removed from database.", "success")
    else:
        flash(_files_left_message(f"'{file.basename}'", [file_path]), "warning")

    if file.movie_id:
        return redirect(url_for("main.movie_files", movie_id=file.movie_id))

    # Season 0 holds the specials. It is a real season page.

    if file.series_id and file.season is not None:
        return redirect(
            url_for("main.season", series_id=file.series_id, season=file.season)
        )
    return redirect(url_for("main.index"))


@bp.route("/file/<int:file_id>", methods=["GET", "POST"])
@login_required
@admin_required
def file(file_id):
    """Show the details for a particular video file.

    This is an admin page (#186 follow-up). All on it (track edits,
    remux, upload/download, delete) is library management.
    Thus, the whole page is gated, and not each form.

    Each form of the page has a handler. A valid post of a form runs its
    handler. Then the page shows again, unless the handler gives another
    page. A track form that does not validate gets a message, unless
    only its csrf token failed (#763). A form with needs_local_file refuses a file that is not in the local
    library. Only a restore from AWS or a delete are useful then.
    The template disables the other forms."""

    file = File.query.filter_by(id=file_id).first_or_404()
    file_exists_locally = os.path.isfile(
        os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
    )

    # The video file can belong to a movie or to a TV show.

    movie = (
        Movie.query.filter_by(id=int(file.movie_id)).first_or_404()
        if file.movie_id
        else None
    )
    tv = (
        TVSeries.query.filter_by(id=int(file.series_id)).first_or_404()
        if file.series_id and not file.movie_id
        else None
    )
    best_file = _is_best_copy(file)

    audio_tracks = FileAudioTrack.query.filter_by(file_id=file.id).all()
    subtitle_tracks = FileSubtitleTrack.query.filter_by(file_id=file.id).all()

    metadata_scan_form = TrackMetadataScanForm()
    mkvpropedit_form = _propedit_form(audio_tracks, subtitle_tracks)
    mkvmerge_form = _remux_form(audio_tracks, subtitle_tracks)
    upload_form = S3UploadForm()
    file_restore_estimate = tv_pages.restore_cost_estimate(
        [file] if file.aws_key else []
    )
    download_form = S3DownloadForm()
    delete_form = FileDeleteForm()

    # (form, submit field, handler, needs_local_file)

    handlers = (
        (metadata_scan_form, metadata_scan_form.scan_submit, _scan_file_post, True),
        (
            mkvpropedit_form,
            mkvpropedit_form.mkvpropedit_submit,
            _propedit_post(audio_tracks, subtitle_tracks),
            True,
        ),
        (mkvmerge_form, mkvmerge_form.mkvmerge_submit, _remux_post, True),
        (upload_form, upload_form.s3_upload_submit, _upload_post, True),
        (
            download_form,
            download_form.s3_download_submit,
            _restore_post(file_restore_estimate),
            False,
        ),
        (delete_form, delete_form.delete_submit, _delete_file_post, False),
    )
    # A pending restore refuses each form that edits, uploads or restores
    # the file (#610). Only the delete stays. The refusal comes before
    # the validation.

    restore_pending = refuse_while_restore_pending(file)
    if restore_pending and any(
        submit.data
        for _, submit, _, _ in handlers
        if submit is not delete_form.delete_submit
    ):
        flash(f"'{file.basename}' {restore_pending}", "warning")
        return redirect(url_for("main.file", file_id=file.id))

    for form, submit, handler, needs_local_file in handlers:
        if submit.data and form.validate_on_submit():
            if needs_local_file and not file_exists_locally:
                flash(f"'{file.basename}' is not present locally.", "warning")
                return redirect(url_for("main.file", file_id=file.id))
            return handler(file, form) or redirect(
                url_for("main.file", file_id=file.id)
            )

    # Invariant: a track form that does not validate says why (#763).
    # Each field of the 2 track forms is an Optional choice field. Thus,
    # the only error other than csrf is a posted value that is not a
    # track of the file now. That post came from a page that was loaded
    # before a job or an arr changed the tracks. The choice error of
    # WTForms does not say that. The 4 other forms have no field that can
    # fail, except the csrf token. A csrf error stays quiet, as in
    # flash_form_errors. The errors are empty unless the post validated
    # the form above.

    for form in (mkvpropedit_form, mkvmerge_form):
        if set(form.errors) - {"csrf_token"}:
            flash(
                f"The tracks of '{file.basename}' changed after the page was "
                f"loaded. Nothing was queued. The page shows the tracks as they "
                f"are now.",
                "warning",
            )

    # A track edit that stopped on a changed file left a note (#606).
    # The route said that the edit was queued. Thus, the page says that
    # it did not run.

    stopped_edit = pop_stopped_edit(file.id)
    if stopped_edit:
        flash(f"'{file.basename}' {stopped_edit}", "warning")

    # A form that was not posted or that was refused shows its defaults.
    # The defaults come from the tracks of the file now.

    mkvpropedit_form.process()
    mkvmerge_form.process()

    # The per-file triage link exists only while this file has pending
    # possibly-forced tracks (and only admins can act on them). The
    # candidate track ids go with it. Thus, the Tracks table can badge
    # the rows that the triage page asks about. The import-time marker
    # (forced left unknown) covers only the older, cruder heuristic.
    # Without the ids, a candidate recorded as plainly unforced would
    # have no badge and no way in.

    triage_candidates = forced_subtitle_candidates(file_id=file.id)
    possibly_forced_track_ids = {
        candidate["track"].id
        for entry in triage_candidates
        for candidate in entry["tracks"]
    }
    pending_subtitle_triage = bool(current_user.admin and triage_candidates)
    pending_lossy_triage = bool(
        current_user.admin and lossy_audio_candidates(file_id=file.id)
    )

    return render_template(
        "file.html",
        file=file,
        title=file.basename,
        movie=movie,
        tv=tv,
        audio_tracks=audio_tracks,
        subtitle_tracks=subtitle_tracks,
        pending_subtitle_triage=pending_subtitle_triage,
        pending_lossy_triage=pending_lossy_triage,
        possibly_forced_track_ids=possibly_forced_track_ids,
        metadata_scan_form=metadata_scan_form,
        mkvpropedit_form=mkvpropedit_form,
        # The dropdowns offer the own languages of the collection.
        # language_names covers the stored spellings too. Thus, a track
        # recorded as "deu" still reads as German.
        # The dropdowns compare by the resolved code. The own languages
        # of the file are always choices (#545).
        languages=(
            file_language_choices(
                [track.language for track in list(audio_tracks) + list(subtitle_tracks)]
            )
            if file.container == "Matroska" and (audio_tracks or subtitle_tracks)
            else ()
        ),
        language_names=language_names(),
        language_code=resolve_language_code,
        mkvmerge_form=mkvmerge_form,
        upload_form=upload_form,
        download_form=download_form,
        file_restore_estimate=file_restore_estimate,
        delete_form=delete_form,
        best_file=best_file,
        file_exists_locally=file_exists_locally,
    )
