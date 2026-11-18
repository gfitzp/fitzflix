"""Serve the TV library and the pages of 1 series."""

import os
from flask import current_app, render_template, flash, redirect, url_for, request
from flask_login import current_user, login_required
from app.tasks import enqueue_task
from app import db
from app.archive.restores import request_restore
from app.main.forms import (
    LibrarySearchForm,
    SeasonRestoreForm,
    SeriesRestoreForm,
    SeriesDeleteForm,
    TMDBLookupForm,
    TMDBRemoveForm,
)
from app.models import (
    ArchivedOriginal,
    File,
    RefQuality,
    TVCast,
    TVSeries,
    title_sort,
    TV_SHOWS,
    title_lock_key,
)
from app.auth.passkeys import identity_confirmed
from app.main import bp
from app.media.file_ranking import (
    TV_BEST_ORDER,
    best_episode_holders,
    copy_upgradable,
    season_grades,
)
from app.main.helpers import (
    admin_required,
    flash_form_errors,
    _upgrade_threshold,
    series_upgradable,
    tv_meta_line,
)
from app.media.custom_artwork import remove_custom_artwork
from app.main import file_pages, movie_pages
from app.operations.maintenance import clear_leftover_directory
from app.metadata.tmdb_refresh import tmdb_refresh_status


@bp.route("/library/tv", methods=["GET", "POST"])
@login_required
def tv_library():
    """Show the worst quality in each season for each TV show in the library.

    ?q= narrows the listing inside the TV library only. The
    list holds only the series whose title or TMDB name matches. The
    search does not read the titles of episodes. An episode title must
    never pull its series into the list (Glenn: a search for "venture"
    must not show Bob's Burgers)."""

    # The grade of each season is its worst episode. Each episode number
    # of a multi-episode file counts. The In-library badge and the
    # TV shopping list read the same grade.

    upgrade_threshold = _upgrade_threshold()
    grades = season_grades()

    seasons_by_series = {}
    for (series_id, season), grade in sorted(
        grades.items(),
        key=lambda item: (item[0][0], item[0][1] == 0, item[0][1]),
    ):
        seasons_by_series.setdefault(series_id, []).append(
            {
                "season": season,
                "episode_count": grade.episodes,
                "min_quality": grade.quality_title,
                # A physical-media season (DVD, SD/720p Blu-ray) is often the
                # only release that will ever exist. Thus, it does not count as
                # upgradable.
                "upgradable": copy_upgradable(
                    False,
                    grade.preference,
                    upgrade_threshold,
                    physical=grade.physical_media,
                ),
            }
        )

    q = request.args.get("q", None, type=str)

    # The search box posts and redirects into ?q=, the grammar of the
    # movie library. Thus, a search is a URL that the user can bookmark.

    library_search_form = LibrarySearchForm()
    if library_search_form.validate_on_submit():
        return redirect(
            url_for("main.tv_library", q=library_search_form.search_query.data or None)
        )

    series_query = TVSeries.query.join(File, (File.series_id == TVSeries.id)).distinct()
    if q:
        # Spaces become wildcards, like in the search of the movie library

        wildcard = q.replace(" ", "%")
        series_query = series_query.filter(
            db.or_(
                TVSeries.title.ilike(f"%{wildcard}%"),
                TVSeries.tmdb_name.ilike(f"%{wildcard}%"),
            )
        )

    tv = []
    for series in series_query.order_by(title_sort(TVSeries, tmdb=False).asc()).all():
        tv.append(
            {
                "id": series.id,
                "title": series.title,
                "tmdb_id": series.tmdb_id,
                "tmdb_name": series.tmdb_name,
                "first_air_year": (
                    series.tmdb_first_air_date.year
                    if series.tmdb_first_air_date
                    else None
                ),
                "tmdb_poster_path": series.tmdb_poster_path,
                "seasons": seasons_by_series.get(series.id, []),
            }
        )

    return render_template(
        "library_tv.html",
        title=f"TV library matches for '{q}'" if q else "TV Library",
        series=tv,
        q=q,
        library_search_form=library_search_form,
    )


def restore_cost_estimate(files, bulk=False):
    """Estimate the AWS cost to restore and download the archived files.

    This function uses the exact size of the archived object when it is
    recorded. If not, it uses the size of the localized copy with a
    1.25x factor, because the archived original is usually larger than
    the localized copy.
    """

    if bulk:
        restore_request_cost = (
            current_app.config["AWS_RESTORE_PER_1K_REQUEST_BULK_COST"] / 1000
        )
        restore_per_gb_cost = current_app.config["AWS_RESTORE_PER_GB_BULK_COST"]
    else:
        restore_request_cost = (
            current_app.config["AWS_RESTORE_PER_1K_REQUEST_COST"] / 1000
        )
        restore_per_gb_cost = current_app.config["AWS_RESTORE_PER_GB_COST"]
    # A conversion restores its smaller transcoded/ copy.

    def restore_bytes(file):
        transcoded_key = getattr(file, "aws_transcoded_key", None)
        if transcoded_key and getattr(file, "restore_key", None) == transcoded_key:
            return file.aws_transcoded_filesize_bytes or file.filesize_bytes or 0
        if file.aws_filesize_bytes:
            return file.aws_filesize_bytes
        return file.filesize_bytes * 1.25 if file.filesize_bytes else 0

    gigabytes = sum(restore_bytes(file) for file in files) / 1024**3
    cost = (len(files) * restore_request_cost) + (
        gigabytes
        * (restore_per_gb_cost + current_app.config["AWS_DOWNLOAD_PER_GB_COST"])
    )
    return {"count": len(files), "gigabytes": gigabytes, "cost": cost}


@bp.route("/tv/<int:series_id>")
@login_required
def tv(series_id):
    """Show details for a particular TV series."""

    tv = TVSeries.query.filter_by(id=series_id).first_or_404()
    title = tv.display_title
    seasons = []
    for file in tv.files:
        seasons.append(file.season)

    # A set has no order. Thus, the sort comes after the dedup.

    seasons = sorted(set(seasons))

    # The series management forms (restore, delete, TMDB) are admin
    # tools, like the Movie Data section of the movie page.
    # The template hides them. They post to tv_restore,
    # tv_delete, and tv_tmdb, which only admins can reach.

    # A restore costs real money. Thus, the page shows an estimate.

    series_restore_estimate = restore_cost_estimate(
        _series_restorable(series_id), bulk=True
    )

    series_restore_form = SeriesRestoreForm()
    series_delete_form = SeriesDeleteForm()
    tmdb_remove_form = TMDBRemoveForm()
    tmdb_lookup_form = TMDBLookupForm()

    # The billed cast for the scroller, in aggregate billing order. The
    # list has a maximum. The aggregate cast of a long-running series
    # can have hundreds of one-episode guest roles. They would bloat the
    # page for no gain.

    cast = [
        {
            "id": role.credit_id,
            "name": role.starring.name,
            "profile_path": role.starring.tmdb_profile_path,
            "character": role.character,
        }
        for role in tv.cast.order_by(
            TVCast.billing_order.asc(), TVCast.episode_count.desc()
        )
        .limit(100)
        .all()
    ]

    # The meta line of the movie page, in TV terms. It shows the run of
    # years, the size when the run is complete (the apply stores counts
    # only for Ended shows), and the genres. The shared helper that the
    # popover card reads too builds it.

    tmdb_refresh_state = None
    if current_user.admin:

        tmdb_refresh_state = tmdb_refresh_status("TV Shows", tv.id)

    return render_template(
        "tv.html",
        title=title,
        tv=tv,
        tmdb_refresh_state=tmdb_refresh_state,
        seasons=seasons,
        # The In-library badge of the page, in the colors of the TV card:
        # amber when a season is worth an upgrade, green when
        # every season is settled. A series with no files shows none.
        in_library=bool(seasons),
        upgradable=series_upgradable([tv.id]).get(tv.id, False),
        cast=cast,
        meta_line=tv_meta_line(
            tv.tmdb_first_air_date.year if tv.tmdb_first_air_date else None,
            tv.tmdb_last_air_date.year if tv.tmdb_last_air_date else None,
            tv.tmdb_number_of_seasons,
            tv.tmdb_number_of_episodes,
            [genre.name for genre in tv.genres],
        ),
        series_restore_form=series_restore_form,
        series_restore_estimate=series_restore_estimate,
        tmdb_lookup_form=tmdb_lookup_form,
        tmdb_remove_form=tmdb_remove_form,
        series_delete_form=series_delete_form,
    )


@bp.route("/tv/<int:series_id>/<int:season>")
@login_required
def season(series_id, season):
    """Show all files for a season of a TV show, regardless of ranking.

    The int converters make a non-numeric series or season in the URL
    a 404, and not a ValueError further down.
    """

    tv = TVSeries.query.filter_by(id=series_id).first_or_404()

    if season == 0:
        title = f'Files for "{tv.display_title}" special episodes'

    else:
        title = f'Files for "{tv.display_title}", season {season}'

    files = _season_files(series_id, season)

    # The restore of the best archived files of this season is an admin
    # tool, like on the series page. The season page
    # itself stays open to all users for its episode listing. A restore
    # costs real money. Thus, the page shows an estimate.

    season_restore_estimate = restore_cost_estimate(
        _season_restorable(files), bulk=True
    )

    season_restore_form = SeasonRestoreForm()

    return render_template(
        "season.html",
        title=title,
        tv=tv,
        season=season,
        files=files,
        season_restore_form=season_restore_form,
        season_restore_estimate=season_restore_estimate,
    )


def _archived_best_holders(pairs):
    """Return the archived files that are the best holder of an episode number.

    pairs holds (File, RefQuality) pairs. A span that is the only holder
    of E02 to E06 beside a better E01 single is restored too."""

    holders = {file.id: file for file, _ in best_episode_holders(pairs).values()}
    return sorted(
        (file for file in holders.values() if file.aws_key),
        key=lambda file: (file.season, file.episode or 0, file.id),
    )


def _series_restorable(series_id):
    """Return the best archived file of each episode of a series."""

    return _archived_best_holders(
        db.session.query(File, RefQuality)
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .filter(File.series_id == series_id)
        .all()
    )


def _season_files(series_id, season):
    """Return (File, TVSeries, RefQuality, rank) for each file of a season.

    rank is 1 for a file that is the best holder of at least 1 episode
    number. A span competes with the singles of each episode that
    it holds. The other files get rank 2 or more, in TV_BEST_ORDER."""

    rows = (
        db.session.query(File, TVSeries, RefQuality)
        .join(TVSeries, (TVSeries.id == File.series_id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .filter(TVSeries.id == series_id)
        .filter(File.season == season)
        .order_by(File.episode.asc(), *TV_BEST_ORDER)
        .all()
    )
    holders = {
        file.id
        for file, _ in best_episode_holders(
            (file, quality) for file, _, quality in rows
        ).values()
    }
    return [
        (file, series, quality, 1 if file.id in holders else 2)
        for file, series, quality in rows
    ]


def _season_restorable(files):
    """Return the best archived file of each episode in the rows of _season_files."""

    return _archived_best_holders([(file, quality) for file, _, quality, _ in files])


def _request_restores(files):
    """Queue a bulk Glacier restore of each file.

    The hourly SQS poll downloads each file when it is ready."""

    for file in files:
        request_restore(
            file.restore_key,
            tier="Bulk",
            description=f"'{file.untouched_basename}'",
        )


@bp.route("/tv/<int:series_id>/restore", methods=["POST"])
@login_required
@admin_required
def tv_restore(series_id):
    """Request a restore of each archived episode of a series, for an admin.

    A restore costs real money. Thus, the form needs a proof of identity,
    the password or a passkey."""

    tv = TVSeries.query.filter_by(id=series_id).first_or_404()
    series_restore_form = SeriesRestoreForm()
    if not (
        series_restore_form.series_restore_submit.data
        and series_restore_form.validate_on_submit()
    ):
        flash_form_errors(series_restore_form)
        return redirect(url_for("main.tv", series_id=tv.id))
    if identity_confirmed(series_restore_form):
        series_restorable = _series_restorable(series_id)
        series_restore_estimate = restore_cost_estimate(series_restorable, bulk=True)
        _request_restores(series_restorable)
        flash(
            f"Requesting {len(series_restorable)} file(s) for '{tv.display_title}' "
            f"to be restored from AWS Glacier "
            f"(≈ ${series_restore_estimate['cost']:.2f})",
            "info",
        )

    return redirect(url_for("main.tv", series_id=tv.id))


@bp.route("/tv/<int:series_id>/delete", methods=["POST"])
@login_required
@admin_required
def tv_delete(series_id):
    """Delete a TV series and its files from the database, for an admin."""

    tv = TVSeries.query.filter_by(id=series_id).first_or_404()
    title = tv.display_title
    series_delete_form = SeriesDeleteForm()
    if not (
        series_delete_form.delete_submit.data
        and series_delete_form.validate_on_submit()
    ):
        flash_form_errors(series_delete_form)
        return redirect(url_for("main.tv", series_id=tv.id))

    refused = file_pages._delete_refused_for_volumes(
        file.file_path for file in File.query.filter(File.series_id == series_id)
    )
    if refused:
        flash(refused, "warning")
        return redirect(url_for("main.tv", series_id=tv.id))

    # The delete holds the title lock of each episode. A task that holds
    # one of them refuses the delete.

    episodes = sorted(
        {
            (file.season, file.episode)
            for file in File.query.filter(File.series_id == series_id)
        },
        key=str,
    )
    locks = file_pages._delete_locks(
        title_lock_key(TV_SHOWS, tv.title, season=season, episode=episode)
        for season, episode in episodes
    )
    if locks is None:
        flash(
            f"A title lock of '{title}' is not free. A task works on 1 of "
            f"its files. Delete the series when the task is done.",
            "warning",
        )
        return redirect(url_for("main.tv", series_id=tv.id))
    try:
        return _delete_series(tv, series_id, title)
    finally:
        file_pages._release_delete_locks(locks)


def _delete_series(tv, series_id, title):
    """Delete the series and its files under the title locks. Return the next page."""

    aws_keys = []
    leftover_dirs = set()
    file_ids = []
    local_copies = []

    try:
        files = File.query.filter(File.series_id == series_id).all()
        for file in files:
            file_ids.append(file.id)
            aws_keys.extend(file_pages._claimed_archive_keys(file))
            leftover_dirs.add(
                os.path.join(current_app.config["LIBRARY_DIR"], file.dirname)
            )
            local_copies.append((file.file_path, file.id))
            db.session.delete(file)

        # The archive-only originals of the series go with it. With
        # ON DELETE SET NULL, they stayed with no owner and kept claiming
        # their keys. Their S3 objects go after the commit, as above.

        for original in ArchivedOriginal.query.filter_by(series_id=series_id).all():
            aws_keys.append(original.aws_key)
            db.session.delete(original)

        db.session.delete(tv)
        db.session.commit()

    except Exception:
        db.session.rollback()
        flash(f"Unable to delete TV series '{title}'!", "danger")
        return redirect(url_for("main.tv", series_id=series_id))

    # Delete the library files only after the commit. A failed
    # commit keeps every row. Thus, it must also keep every file. A row
    # with no file is a ghost. A file that the share does not remove
    # stays with no record. No sweep removes it, and the weekly sync
    # imports it again. Thus, the page names each such file.

    left = [
        file_path
        for file_path, file_id in local_copies
        if file_pages._after_commit(
            File.delete_library_copy, file_path, file_id, delete_directory_tree=True
        )
        is not True
    ]

    # Delete the AWS copies only after the database delete is committed.
    # Thus, a failed commit cannot leave database records whose backups
    # are gone.

    file_pages._queue_archive_deletes(aws_keys)

    # The folders themselves. delete_library_copy removes only EMPTY
    # directories. Poster art sits beside the episodes (placed by Fitzflix
    # for movies, by Sonarr or by hand for TV). Thus, that art used to
    # leave husks for the weekly sweep. An explicit delete clears the
    # junk-only folders immediately.

    for leftover_dir in sorted(leftover_dirs):
        file_pages._after_commit(clear_leftover_directory, leftover_dir)

    # The custom poster tree of each deleted file goes too.

    for file_id in file_ids:
        file_pages._after_commit(remove_custom_artwork, "file", file_id)

    if left:
        flash(file_pages._files_left_message(f"TV series '{title}'", left), "warning")
        return redirect(url_for("main.tv_library"))
    flash(
        f"Deleted TV series '{title}', its records and its library files. "
        f"The delete of each archive in S3 that no other record claims is "
        f"in the queue.",
        "success",
    )
    return redirect(url_for("main.tv_library"))


@bp.route("/tv/<int:series_id>/tmdb", methods=["POST"])
@login_required
@admin_required
def tv_tmdb(series_id):
    """Detach a series from TMDB, or refresh its TMDB data, for an admin."""

    tv = TVSeries.query.filter_by(id=series_id).first_or_404()

    # The form to detach a series from TMDB completely. It is for a
    # series that TMDB has no entry for, or an id that TMDB deleted later.

    tmdb_remove_form = TMDBRemoveForm()
    if tmdb_remove_form.remove_submit.data and tmdb_remove_form.validate_on_submit():
        tv.tmdb_tv_clear()
        db.session.commit()
        flash(
            f"Removed the TMDB ID from '{tv.title}'. Fitzflix will not look it "
            f"up again until you enter one by hand",
            "success",
        )
        return redirect(url_for("main.tv", series_id=tv.id))

    # The form to update the information of a TV series with the latest TMDB data

    tmdb_lookup_form = TMDBLookupForm()
    if not (
        tmdb_lookup_form.lookup_submit.data and tmdb_lookup_form.validate_on_submit()
    ):
        flash_form_errors(tmdb_lookup_form)
        return redirect(url_for("main.tv", series_id=tv.id))

    # A blank id asks TMDB to search by title, and takes the first hit. On
    # a series that is already matched, this silently points the series at
    # a different show, and the 2 series merge. A detached
    # series is guarded too. A blank refresh must not silently undo the
    # detachment with a title search.

    if tmdb_lookup_form.tmdb_id.data is None and (
        tv.tmdb_id is not None or tv.tmdb_ignored
    ):
        flash(
            "Enter a TMDB ID to refresh this series, or use "
            "'Remove TMDB ID' to detach it from TMDB",
            "warning",
        )
        return redirect(url_for("main.tv", series_id=tv.id))

    # An id entered by hand is an intentional new match. It undoes a
    # previous removal. Only an actual id clears the flag. A blank submit
    # cannot get here on a detached record, because of the guard. The
    # apply clears the flag when TMDB sends data for the id. A wrong id
    # leaves the record detached.

    # Add a task to the fitzflix-sql queue to check TMDB and update the
    # database. Add it to the front of the queue, because the user added
    # it interactively.

    refresh_job = enqueue_task(
        "app.metadata.tmdb_refresh.refresh_tmdb_info",
        "TV Shows",
        tv.id,
        tmdb_lookup_form.tmdb_id.data,
        apply_at_front=True,
        rematch=tmdb_lookup_form.tmdb_id.data is not None,
        # The page waits for this refresh. Thus, it goes to the front of
        # the sql queue, not behind the bulk refreshes of the request
        # queue.
        queue="sql",
        description=f"Refreshing TMDB data for '{tv.title}'",
        at_front=True,
    )

    # The requested TMDB ID can belong to a show in the database. Then go
    # to the page of that show. This series gets its TMDB data. A blank ID
    # refreshes this series. filter_by(tmdb_id=None) would
    # match any unmatched series.

    existing_tmdb_tv = (
        TVSeries.query.filter_by(tmdb_id=tmdb_lookup_form.tmdb_id.data).first()
        if tmdb_lookup_form.tmdb_id.data is not None
        else None
    )
    tv_id = existing_tmdb_tv.id if existing_tmdb_tv else tv.id
    movie_pages._flash_tmdb_refresh(
        movie_pages._wait_for_refresh(refresh_job),
        tv.title,
        url_for("main.tv", series_id=tv_id),
    )
    return redirect(url_for("main.tv", series_id=tv_id))


@bp.route("/tv/<int:series_id>/<int:season>/restore", methods=["POST"])
@login_required
@admin_required
def season_restore(series_id, season):
    """Request a restore of the best archived files of a season, for an admin."""

    TVSeries.query.filter_by(id=series_id).first_or_404()
    season_restore_form = SeasonRestoreForm()
    back = url_for("main.season", series_id=series_id, season=season)
    if not (
        season_restore_form.season_restore_submit.data
        and season_restore_form.validate_on_submit()
    ):
        flash_form_errors(season_restore_form)
        return redirect(back)
    if identity_confirmed(season_restore_form):
        restorable = _season_restorable(_season_files(series_id, season))
        season_restore_estimate = restore_cost_estimate(restorable, bulk=True)
        _request_restores(restorable)
        season_name = "specials" if season == 0 else f"season {season}"
        flash(
            f"Requesting {len(restorable)} file(s) for {season_name} to be "
            f"restored from AWS Glacier "
            f"(≈ ${season_restore_estimate['cost']:.2f})",
            "info",
        )

    return redirect(back)
