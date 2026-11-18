"""The work after the commit of an import: alerts, archive requests, and the jobs that follow."""

import os
import traceback
from datetime import timedelta
from functools import partial
from rq.timeouts import BaseTimeoutException
from flask import current_app
from app.media.custom_artwork import move_episode_thumb, remove_episode_thumb_of
from app.tasks import enqueue_task, requeue
from app import db
from app.archive.uploads import ensure_pending_upload
from app.archive.archive_keys import is_pending_archive, untouched_key_still_claimed
from app.archive.moves import request_archive_move
from app.settings import get_setting
from app.email import notify_admin
from app.media.tracks.remux import LibraryFileChanged, refuse_changed_library_file
from app.media.tracks.languages import same_language
from app.media.importing import cataloging, filenames, library_move


def _follow_up(output_file, name, step, *args, **kwargs):
    """Run 1 follow-up step of a committed import. Return its value, or None on a failure.

    The import itself succeeded. A failed step is logged, and the
    session rolls back. The file stays, and the next step runs. The
    time limit of rq and an interrupt are not caught (#376). The caller
    lets them pass too (#689)."""

    try:
        return step(*args, **kwargs)
    except (BaseTimeoutException, KeyboardInterrupt):
        raise
    except Exception:
        current_app.logger.error(traceback.format_exc())
        db.session.rollback()
        current_app.logger.warning(
            f"'{output_file}' Was imported, but a follow-up step failed ({name})"
        )
        return None


def _after_commit(
    file,
    file_path,
    file_details,
    output_file,
    output_directory,
    staged_output,
    worse_local_copies,
    worse_aws_keys,
    pending_emails,
    audio_tracks,
):
    """Do the follow-up work of a committed import. Each step is best effort.

    The new file is in place, and its record is committed. The steps
    delete the worse library copies and send the emails. They give the
    staged copy to the Atmos job and delete the replaced archives. They
    remove the source and tell Radarr. Then they queue the TMDB refresh,
    the Atmos twin and the triage aids. Return True if the conversion
    needs its transcoded/ archive. The caller queues that upload after
    the release of the title lock, because the upload takes that lock."""

    step = partial(_follow_up, output_file)

    step("worse copies", _delete_worse_copies, worse_local_copies, output_file)

    # The record took the spelling of the new file (#739). The thumb of
    # an episode has the name of its video. It moves, as in a rename.

    if file_details.get("respelled_from"):
        step("thumb", move_episode_thumb, file_details["respelled_from"], output_file)
    for send in pending_emails:
        step("email", send)

    # The staged copy can go to the Atmos job of this file. Then that job
    # does not read the file back from the NAS. The job is queued later,
    # after the codec check against the committed rows. Until then, keep
    # the staged copy only for a file with an untwinned TrueHD Atmos
    # track. Staging must also have space for it. With
    # CREATE_ATMOS_TWINS off, no Atmos job comes. Thus, keep nothing. A
    # failed step keeps nothing either.

    atmos_handoff = step(
        "atmos handoff", _atmos_handoff, staged_output, output_file, audio_tracks
    )
    if atmos_handoff is None:
        cataloging._remove_staged_output(staged_output)

    # The commit succeeded. Thus, delete the AWS archives of the replaced
    # files. Do it from the handoff queue. Then the sql worker does not
    # wait on the network. The delete is the last step of a file in the
    # pipeline. Thus, it does not wait in line behind long file work
    # (Glenn, 2026-10-15).

    # A run-scoped upload (#430) moves to its final key now. The target
    # claims that key first. Thus, the delete of a replaced archive with
    # the same key below skips it.

    # The move runs later, and the source is removed now. Thus, the
    # pending object must exist first (#494). A missing object gets a new
    # upload of the source. The check is its own step, and it makes the
    # first read of the row after the commit (#689). The source goes only
    # if that step returned True.

    # Invariant: a record whose archive is under pending-untouched/ has a
    # pending move (#690). If the move request fails before its commit,
    # a source that is not the library file goes to rejects/unarchived,
    # and the replaced archives stay. The weekly S3 sync requests the
    # move again. It does the same
    # for a move whose job was not queued or did not run (#707). Then the
    # source is gone already.

    # A source in the library that changed during the chain is a new
    # download (#742). The check comes first. The step below can upload
    # the source when the run-scoped upload is gone. A changed source is
    # not the file of this record, and it must not go up as its archive.

    changed = file_path != output_file and _source_changed(file_path, file_details)
    source_can_go = not changed and (
        step("pending upload", _pending_upload_ready, file, file_path, output_file)
        is True
    )
    move_requested = step("archive key", _request_final_archive_key, file) is True
    source_can_go = source_can_go and move_requested

    if move_requested:
        for worse_key in worse_aws_keys:
            step("archive delete", _delete_replaced_archive, worse_key)

    # Remove the imported file, unless the localized file replaced it.
    # Fitzflix must not remove the file that it just made.

    # The archive move runs later. If the pending upload is gone and the
    # new upload failed, the source is the only copy of the rip (#494).
    # Then it goes to rejects/unarchived, out of the import sweep. A
    # failed check or a failed move request does the same.

    # A changed source stays, and its own import is queued (#742). The
    # release data that Sonarr or Radarr gave for the path is the data
    # of the new download then. It stays for the import of that
    # download, and Radarr keeps its folder. Thus, the 2 arr steps do
    # not run for a changed source.

    if changed:
        step("changed source", _import_changed_source, file_path)
    elif file_path != output_file and not source_can_go:
        step("source hold", filenames.move_to_rejects, file_path, "unarchived")
    elif file_path != output_file:
        step("source removal", _remove_source, file_path)

    if not changed:
        step("radarr", _report_film_move, file, file_path, output_directory)
        step("arr record", _restore_arr_record, file_path, output_file)

    step("tmdb refresh", _enqueue_tmdb_enrichment, file, file_details)

    atmos_queued = step(
        "atmos twin", _enqueue_atmos_twin, file, atmos_handoff, staged_output
    )
    if not atmos_queued:
        cataloging._remove_staged_output(staged_output)

    # A conversion archives its SDR library file in transcoded/ (#299).
    # An Atmos job archives it when it finishes. Thus, the upload waits
    # for that job.

    archive_due = step(
        "transcoded archive", _transcoded_archive_due, file, atmos_queued
    )

    possibly_foreign_language, lossy_lead, atmos_not_lead = _audio_alerts(
        audio_tracks, current_app.config["NATIVE_LANGUAGE"]
    )
    if possibly_foreign_language and len(audio_tracks) > 1:
        step(
            "foreign audio email",
            notify_admin,
            "Foreign audio track added",
            "possibly_foreign_audio",
            file=file,
        )

    step("file id", lambda: current_app.logger.info(f"{file} File ID {file.id}"))

    step("triage snapshots", _enqueue_triage_snapshots, file)
    step("audio comparison", _enqueue_audio_comparison, file)

    if lossy_lead:
        step(
            "lossy audio email",
            notify_admin,
            "Added a file that has a lossless audio track",
            "lossy_audio",
            file=file,
        )

    # The Atmos job of this file can put its new twin in the first
    # position. Then the file gets an Atmos lead without the admin.
    # Thus, that file sends no email. The job puts each other twin
    # directly before its source, and that position is correct.

    if atmos_not_lead and not (atmos_queued and _atmos_twin_will_lead(audio_tracks)):
        step(
            "atmos order email",
            notify_admin,
            "Added a file that has a Dolby Atmos track behind the first audio track",
            "ddp_atmos_not_first",
            file=file,
            audio_tracks=audio_tracks,
        )
    return archive_due is True


def _audio_alerts(audio_tracks, native_language):
    """Return (possibly_foreign_language, lossy_lead, atmos_not_lead) for an import.

    The first track is possibly foreign if its language is not native,
    "und", or "zxx". The lead is lossy if the first track is lossy and
    another track is lossless. The third flag is the result of
    _atmos_not_lead."""

    first_language = audio_tracks[0].get("language") if audio_tracks else None
    possibly_foreign_language = bool(audio_tracks) and not (
        same_language(first_language, native_language)
        or first_language in ("und", "zxx")
    )
    lossless = [
        track.get("compression_mode", "Lossy") == "Lossless" for track in audio_tracks
    ]
    first_audio_track_lossy = not (lossless and lossless[0])
    return (
        possibly_foreign_language,
        first_audio_track_lossy and any(lossless),
        _atmos_not_lead(audio_tracks),
    )


def _atmos_not_lead(audio_tracks):
    """Return True if a Dolby Digital Plus Atmos track is out of place behind the first track.

    The first audio track of a library file is its default track. Thus,
    the decision uses the track order and not the default flag. The file
    is in order if its first track is Dolby Digital Plus Atmos. A
    lossless first track does not change the result: the Atmos pipeline
    also puts its twin before the lossless pair.

    A track in the position that the Atmos pipeline gives a twin is
    there on purpose (Glenn, 2026-08-24). That position is directly
    before its TrueHD Atmos source, or before the FLAC twin of the
    source. Thus, such a track does not count."""

    # This import is lazy. The atmos module resolves the worker app
    # singleton at module import time.

    from app.media.atmos import EAC3_ATMOS_CODEC

    codecs = [track.get("codec") or "" for track in audio_tracks]
    if not codecs or codecs[0] == EAC3_ATMOS_CODEC:
        return False
    return any(
        codec == EAC3_ATMOS_CODEC and not _is_placed_atmos_twin(audio_tracks, index)
        for index, codec in enumerate(codecs)
    )


def _is_placed_atmos_twin(audio_tracks, index):
    """Return True if the track at index is where the Atmos pipeline puts a twin.

    Without the track, the next track or the track after it must be a
    TrueHD Atmos track in the same language. The insertion point of that
    source must be the position of the track."""

    from app.media.atmos import TRUEHD_ATMOS_CODEC, insertion_point

    language = audio_tracks[index].get("language")
    others = audio_tracks[:index] + audio_tracks[index + 1 :]
    return any(
        (others[source].get("codec") or "") == TRUEHD_ATMOS_CODEC
        and others[source].get("language") == language
        and insertion_point(others, source) == index
        for source in range(index, min(index + 2, len(others)))
    )


def _atmos_twin_will_lead(audio_tracks):
    """Return True if the Atmos job puts a new twin in the first audio position."""

    from app.media.atmos import atmos_supplement_candidates, insertion_point

    return any(
        insertion_point(audio_tracks, source) == 0
        for source in atmos_supplement_candidates(audio_tracks)
    )


def _report_film_move(file, file_path, output_directory):
    """Tell Radarr about the move of a film import. An episode has nothing to report."""

    if file.movie_id:
        library_move._report_import_move_to_radarr(
            os.path.dirname(file_path), output_directory
        )


def _restore_arr_record(file_path, output_file):
    """Queue the arr record update of a download stored under a new name (#671).

    The webhook kept the release data of an arr download for its path.
    When the library file has a different path, arr drops its record at
    the next scan, and it downloads the same release again. Thus, a task
    writes the release data onto the new record. The data leaves Redis
    in each case. It goes on in the arguments of the task, and each try
    that the task books again carries it (#680)."""

    from app.arr.arr_client import ARRS, take_remembered_download

    remembered = take_remembered_download(file_path)
    if not remembered or os.path.realpath(file_path) == os.path.realpath(output_file):
        return
    arr = ARRS[remembered["service"]]
    enqueue_task(
        arr.restore_task,
        remembered["owner_id"],
        output_file,
        remembered["scene_name"],
        remembered["release_group"],
        description=f"'{os.path.basename(output_file)}' Updating the {arr.name} record",
        result_ttl=86400,
    )


def _transcoded_archive_due(file, atmos_queued):
    """Return True if a conversion needs its transcoded/ archive and no Atmos job does it."""

    return bool(
        file.converted_from_quality
        and get_setting("ARCHIVE_ORIGINAL_MEDIA")
        and not atmos_queued
    )


def _delete_worse_copies(worse_local_copies, output_file):
    """Delete the library copies of the worse files and their triage snapshots.

    The new file never has the path of a worse file. The check is a
    guard."""

    from app.media.triage import remove_triage_snapshots

    for worse_path, worse_id in worse_local_copies:
        if worse_path == output_file:
            continue
        try:
            os.remove(worse_path)
            current_app.logger.info(f"Deleted local file '{worse_path}'")
        except FileNotFoundError:
            pass
        except OSError as e:
            # A share that remounts must not stop the rest of the
            # follow-up work. The orphan is visible on the share.
            current_app.logger.warning(
                f"'{worse_path}' Couldn't delete the replaced file: {e}"
            )
        remove_triage_snapshots(worse_id)

        # The episode thumb of a deleted TV file has no video left (#469)

        tv_library = os.path.abspath(current_app.config["TV_LIBRARY"])
        if os.path.abspath(worse_path).startswith(tv_library + os.sep):
            remove_episode_thumb_of(worse_path)


def _same_file(path, other):
    """Return True if 2 paths name 1 file on the disk. A missing path gives False."""

    try:
        return os.path.samefile(path, other)
    except OSError:
        return False


def _atmos_handoff(staged_output, output_file, audio_tracks):
    """Return the staged copy for the Atmos job, or None if no job can come.

    No job comes with CREATE_ATMOS_TWINS off, or for a file without a
    TrueHD Atmos track that wants a twin. Then this makes no space check
    and writes no log line about the Atmos job."""

    from app.media.atmos import atmos_supplement_candidates, staged_handoff

    if not get_setting("CREATE_ATMOS_TWINS"):
        return None
    if not atmos_supplement_candidates(audio_tracks):
        return None
    return staged_handoff(staged_output, output_file)


def _pending_upload_ready(file, file_path, output_file):
    """Return True if the archive of a committed import does not need its source (#494).

    Only a run-scoped upload (#430) can need it. Its object must exist
    before the source goes. A read of the row that fails raises. Then
    the caller keeps the source, because the state of the archive is
    not known (#689)."""

    if file_path == output_file:
        return True
    if not (file.aws_key and is_pending_archive(file.aws_key)):
        return True
    return ensure_pending_upload(file, file_path)


def _request_final_archive_key(file):
    """Request the archive move of a run-scoped upload to its final key (#430).

    The records are committed. Thus, a replaced record no longer claims
    the key, and the move can take it. A record that still claims the
    key keeps it. Then the move gives this file the key with its id.
    Return True when no move is necessary or the request is committed."""

    if file.aws_key and is_pending_archive(file.aws_key):
        request_archive_move(file)
        db.session.commit()
    return True


def _delete_replaced_archive(worse_key):
    """Queue the AWS delete of a replaced archive that no row claims.

    The new file can claim the same key that its predecessor held (a
    repointed key, or a re-import on the same basename). The claim can
    also be the pending target of the new file. Then the archive move of
    that file puts the new rip at the key (#709). Never delete a key
    that a surviving row claims. The task asks for the claim again when
    it runs (#686)."""

    if untouched_key_still_claimed(worse_key):
        current_app.logger.info(
            f"Keeping '{worse_key}' in AWS. A file record claims it, or the "
            f"archive move of a record has it as its target"
        )
        return
    # The replaced row can name a key with no object. A plain delete
    # then writes a marker at a key that never existed (#588).

    enqueue_task(
        "app.archive.moves.aws_delete",
        worse_key,
        only_if_present=True,
        only_if_unclaimed=True,
        description=f"Deleting '{worse_key}' from AWS",
    )


def _in_library(path):
    """Return True if path is inside the movie library or the TV library.

    These are the folders that Radarr and Sonarr write. LIBRARY_DIR is
    their parent. On the server it also holds the import directory, the
    held directory and the rejects, and they are not library folders."""

    path = os.path.abspath(path)
    return any(
        path.startswith(os.path.abspath(current_app.config[root]) + os.sep)
        for root in ("MOVIE_LIBRARY", "TV_LIBRARY")
    )


def _source_changed(file_path, file_details):
    """Return True if an other program changed a library source during its import (#742).

    A source with no stamp is never "changed". A fault of the read
    counts as a change. The state of the file is not known then, and a
    source that stays is the safe direction. The weekly sync imports a
    library file with no record."""

    try:
        refuse_changed_library_file(file_details.get("source_stamp"), file_path)
    except LibraryFileChanged:
        return True
    except OSError:
        current_app.logger.warning(traceback.format_exc())
        return True
    return False


# The import of a changed source starts after this time. The job that
# found the change is done by then. While that job is live, a new
# localization of the same path is not queued (enqueue_localization).

CHANGED_SOURCE_DELAY = timedelta(minutes=1)


def _import_changed_source(file_path):
    """Book the import of a library file that an other program changed during an import (#742).

    A source that is gone gets no import, and a warning says so. This
    is best effort. A fault is a warning, and the weekly sync finds a
    file with no record."""

    if not os.path.isfile(file_path):
        current_app.logger.warning(
            f"'{file_path}' is not there now, or the share does not answer. "
            f"No import is booked. If the file is there later, the weekly "
            f"sync imports it"
        )
        return
    try:
        requeue(
            "app.media.importing.localization.localization_task",
            key=file_path,
            delay=CHANGED_SOURCE_DELAY,
            reason="An other program changed the file during its import",
            args=(file_path,),
            description=f"'{os.path.basename(file_path)}'",
            level=None,
        )
        current_app.logger.info(
            f"'{file_path}' The import of the new file at this path starts in "
            f"{int(CHANGED_SOURCE_DELAY.total_seconds() // 60)} minute"
        )
    except Exception:
        current_app.logger.warning(
            f"'{file_path}' The import of the changed file could not be booked: "
            + traceback.format_exc()
        )


def _remove_source(file_path):
    """Remove the source of an import. A source that is already gone is not an error."""

    try:
        os.remove(file_path)
    except FileNotFoundError:
        pass


def _enqueue_tmdb_enrichment(file, file_details):
    """Queue the TMDB refresh of a new film or series that has no match yet.

    TMDB enrichment runs as its own task after the commit. Thus, the
    cataloging never waits on the network. The enrichment sends an email
    if the movie still has no match. The fetch runs on the request
    queue. It gives its payload to the sql queue for the database writes.
    A filename id tag goes with the task. Then the enrichment fetches
    that exact title. It does not search by name (#155)."""

    movie = file.movie
    tv_series = file.tv_series
    if file.movie_id and movie.tmdb_id == None and not movie.tmdb_ignored:
        enqueue_task(
            "app.metadata.tmdb_refresh.refresh_tmdb_info",
            "Movies",
            movie.id,
            file_details.get("tmdb_id"),
            notify_if_missing=True,
            description=f"Refreshing TMDB data for '{movie.title} ({movie.year})'",
        )
    elif file.series_id and tv_series.tmdb_id == None and not tv_series.tmdb_ignored:
        enqueue_task(
            "app.metadata.tmdb_refresh.refresh_tmdb_info",
            "TV Shows",
            tv_series.id,
            file_details.get("tmdb_id"),
            description=f"Refreshing TMDB data for '{tv_series.title}'",
        )


def _enqueue_atmos_twin(file, atmos_handoff, staged_output):
    """Queue the E-AC-3 Atmos twin of a TrueHD Atmos track. Return True if a job is queued.

    A TrueHD Atmos track without its E-AC-3 Atmos twin gets the
    MediaConvert supplement (#55b). The job comes after the commit. Then
    it sees the complete records. It goes on the handoff queue. The file
    is already in the pipeline. Thus, its twin does not wait behind the
    new imports. CREATE_ATMOS_TWINS turns this step off for passthrough
    players."""

    from app.media.atmos import maybe_enqueue_atmos_supplement

    if not get_setting("CREATE_ATMOS_TWINS"):
        current_app.logger.info(
            f"'{file.basename}' CREATE_ATMOS_TWINS is off. No E-AC-3 Atmos twins"
        )
        return False
    return bool(
        maybe_enqueue_atmos_supplement(
            file.id, staged_source=atmos_handoff, queue=current_app.handoff_queue
        )
    )


def _enqueue_triage_snapshots(file):
    """Queue the triage snapshots of a new file, and tell the admin about a forced track.

    Make the inspection aids of the triage page now, while the file is
    new and certainly local. The gate is the SAME candidates query that
    the triage page uses. It is not the first-track-baseline heuristic.
    That heuristic misses a file whose suspicious track comes FIRST. The
    tracks of Baby Driver read [49, 3110, 4334] elements, and nothing was
    flagged."""

    from app.media.triage import maybe_enqueue_triage_snapshots

    if maybe_enqueue_triage_snapshots(file.id):
        notify_admin(
            "Possibly forced subtitle track",
            "possibly_forced_subtitle",
            file=file,
        )


def _enqueue_audio_comparison(file):
    """Queue the audio comparison clips of a new file.

    The candidates check inside maybe_enqueue prevents clips for the
    intentional E-AC-3 Atmos lead of the Atmos trio."""

    from app.media.triage import maybe_enqueue_audio_comparison

    maybe_enqueue_audio_comparison(file.id)
