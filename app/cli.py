import os

import click

from app import enqueue_import_scan
from app.models import Movie, TVSeries


def register(app):
    """Attach the CLI commands of the application to the Flask app."""

    @app.cli.group()
    def refresh():
        """Refresh data from external services."""
        pass

    @refresh.command()
    def criterion():
        """Refresh the Criterion Collection info from Wikidata."""

        app.sql_queue.enqueue(
            "app.videos.refresh_criterion_collection_info",
            args=None,
            job_timeout=app.config["SQL_TASK_TIMEOUT"],
            description="Refreshing Criterion Collection information for all movies in library",
        )
        app.logger.info("Refreshing Criterion Collection information from Wikipedia")

    @refresh.command()
    @click.argument("library", required=False)
    @click.argument("tmdb_id", required=False)
    def tmdb(library=None, tmdb_id=None):
        """Refresh the library information from TMDB."""

        movies = []
        tv_shows = []

        if library in ["movie", "tv"] and tmdb_id:

            if library == "movie":
                movies = (
                    Movie.query.filter(Movie.tmdb_id == tmdb_id)
                    .order_by(Movie.title.asc(), Movie.year.asc())
                    .all()
                )

            elif library == "tv":
                tv_shows = (
                    TVSeries.query.filter(TVSeries.tmdb_id == tmdb_id)
                    .order_by(TVSeries.title.asc())
                    .all()
                )

        else:
            movies = (
                Movie.query.filter(Movie.tmdb_id != None)
                .order_by(Movie.title.asc(), Movie.year.asc())
                .all()
            )
            tv_shows = (
                TVSeries.query.filter(TVSeries.tmdb_id != None)
                .order_by(TVSeries.title.asc())
                .all()
            )

        # The network phase of the refresh belongs on the request queue.
        # It gives its payload to the sql queue for the database writes.

        if movies:
            for movie in movies:
                app.request_queue.enqueue(
                    "app.videos.refresh_tmdb_info",
                    args=(
                        "Movies",
                        movie.id,
                        movie.tmdb_id,
                    ),
                    job_timeout=app.config["SQL_TASK_TIMEOUT"],
                    description=f"Refreshing TMDB data for '{movie.title} ({movie.year})'",
                )
                app.logger.info(
                    f"Queueing TMDB refresh for '{movie.title} ({movie.year})'"
                )

        if tv_shows:
            for tv in tv_shows:
                app.request_queue.enqueue(
                    "app.videos.refresh_tmdb_info",
                    args=(
                        "TV Shows",
                        tv.id,
                        tv.tmdb_id,
                    ),
                    job_timeout=app.config["SQL_TASK_TIMEOUT"],
                    description=f"Refreshing TMDB data for '{tv.title}'",
                )
                app.logger.info(f"Queueing TMDB refresh for '{tv.title}'")

    @refresh.command()
    @click.argument("file_id")
    def file(file_id):
        """Refresh the metadata for the file with the specified file ID."""

        app.file_queue.enqueue(
            "app.videos.track_metadata_scan_task",
            args=(int(file_id),),
            job_timeout=app.config["MKVPROPEDIT_TASK_TIMEOUT"],
            description=f"Refreshing metadata for file ID {file_id}",
        )
        app.logger.info(f"Refreshing metadata for file ID {file_id}")

    @app.cli.command()
    def sync():
        """Sync the library with the AWS storage."""

        app.request_queue.enqueue(
            "app.videos.sync_aws_s3_storage_task",
            args=None,
            job_timeout="24h",
            description="Pruning extra files from AWS S3 storage",
            at_front=True,
        )
        app.logger.info("Pruning extra files from AWS S3 storage")

    @app.cli.command()
    def scan():
        """Scan the import directory for files to import."""

        enqueue_import_scan(app.request_queue, at_front=True)
        app.logger.info("Scanning import directory for files")

    @app.cli.command()
    def sqs():
        """Check for restored files at AWS S3."""

        app.request_queue.enqueue(
            "app.videos.sqs_retrieve_task",
            job_timeout="2h",
            description="Polling AWS SQS for files to download",
        )
        app.logger.info("Polling AWS SQS for files to download")

    @app.cli.group()
    def aws():
        """Manage the AWS infrastructure that Fitzflix depends on."""
        pass

    @aws.command()
    @click.option(
        "--force",
        is_flag=True,
        help="Proceed even if the bucket reports fewer lifecycle rules "
        "than the newest local snapshot (i.e. the reduction is intended).",
    )
    def provision(force):
        """Create the AWS components that the README describes, idempotently.

        These are the S3 bucket, the lifecycle rules, the SQS queue, and
        the restore-notification connections. It is safe to run this
        again. It keeps and reports the existing configuration. It takes
        a snapshot of the found configuration before any change."""

        from app.aws_setup import StaleReadSuspected
        from app.aws_setup import provision as provision_aws
        from app.videos import aws_s3_client, aws_sqs_client

        if not app.config["AWS_BUCKET"]:
            raise click.ClickException(
                "AWS_BUCKET (plus AWS_ACCESS_KEY / AWS_SECRET_KEY) must be "
                "set in .env before provisioning"
            )

        try:
            results = provision_aws(
                app.config,
                aws_s3_client(),
                aws_sqs_client(),
                echo=click.echo,
                force=force,
            )
        except StaleReadSuspected as e:
            raise click.ClickException(str(e))
        created = sum(1 for _, status in results if status != "present")
        click.echo(
            f"\n{len(results)} components checked, "
            f"{created} created or updated"
            + ("" if created else " — everything was already in place")
        )

    @aws.command("cdn-url")
    @click.argument("key")
    @click.option(
        "--expires",
        default=600,
        show_default=True,
        help="Seconds until the signed URL expires.",
    )
    def cdn_url(key, expires):
        """Print a signed CloudFront URL for one object key.

        Use it with curl to test the CloudFront download path (refer to
        infra/README.md). KEY is the full object key, for example
        "untouched/Film (2021) - [Bluray-1080p].mkv". The settings
        CDN_DOMAIN, CDN_KEY_PAIR_ID, and CDN_PRIVATE_KEY must be set."""

        from app.aws_storage import cdn_signed_url, missing_cdn_settings

        missing = missing_cdn_settings()
        if missing:
            raise click.ClickException(f"{', '.join(missing)} must be set in .env")
        click.echo(cdn_signed_url(key, expires))

    @app.cli.group()
    def recs():
        """Manage the film recommendation engine."""
        pass

    @recs.command()
    def recompute():
        """Recompute and store the recommendations of every reviewer now.

        Do not wait for the nightly run."""

        from app.recommendations import recompute_recommendations

        recompute_recommendations()
        click.echo("Recommendations recomputed")

    @recs.command()
    def streaming():
        """Recompute and store the streaming rail of every eligible user now.

        Do not wait for the nightly run."""

        from app.streaming_rail import recompute_streaming_rail

        recompute_streaming_rail()
        click.echo("Streaming rail recomputed")

    @recs.command()
    def leaving():
        """Fetch and store the leaving-Criterion film set now.

        Do not wait for the monthly run."""

        from app.leaving_criterion import refresh_leaving_criterion

        refresh_leaving_criterion()
        click.echo("Leaving-Criterion set refreshed")

    @recs.command("newly-added")
    def newly_added():
        """Scrape and compare the newly-added feed of every provider now.

        Do not wait for the nightly run."""

        from app.newly_added import refresh_newly_added

        refresh_newly_added()
        click.echo("Newly-added feeds refreshed")

    @recs.command("catalog")
    def provider_catalogs():
        """List the catalogs of the subscribed providers and process the
        pending discoveries now.

        Do not wait for the nightly run."""

        from app.provider_catalog import refresh_provider_catalogs

        refresh_provider_catalogs()
        click.echo("Provider catalogs refreshed")

    @recs.command()
    def awards():
        """Refresh the Wikidata award records of every film now.

        Do not wait for the weekly run. The film-item pass runs first.
        Then the person-item craft backfill adds to it."""

        from app.awards import refresh_movie_awards, refresh_person_awards

        click.echo(refresh_movie_awards())
        click.echo(refresh_person_awards())

    @recs.command()
    @click.argument("dataset", type=click.Path(exists=True, file_okay=False))
    def copref(dataset):
        """Rebuild the MovieLens co-preference similarity table.

        The source is an extracted ml-32m dataset directory. This needs
        numpy and scipy installed ad hoc. They are build-time tools, not
        runtime dependencies. See app/copref.py for the dataset source."""

        from app.copref import build_copref_table

        click.echo(build_copref_table(dataset))

    @app.cli.group()
    def alerts():
        """Manage the watchlist availability alerts."""
        pass

    @alerts.command()
    def availability():
        """Compare the availability of the watchlisted films with the
        stored snapshot and send the digests now.

        Do not wait for the nightly run. The first run only writes the
        snapshots."""

        from app.availability_alerts import notify_watchlist_availability

        notify_watchlist_availability()
        click.echo("Watchlist availability checked")

    @app.cli.group()
    def transcodes():
        """Manage the derived transcoded copies."""
        pass

    @transcodes.command()
    def adopt():
        """Adopt the untracked transcodes.

        Walk TRANSCODES_DIR and create DerivedFile rows for every copy
        whose source is identifiable."""

        from flask import current_app

        job = current_app.file_queue.enqueue(
            "app.transcodes.adopt_transcodes_task",
            job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
            description="Adopting untracked transcoded copies",
        )
        click.echo(f"Enqueued transcode adoption as {job.id}")

    @app.cli.group()
    def frames():
        """Manage the Name That Frame pool."""
        pass

    @frames.command()
    def refresh():
        """Prune and fill the frame pool now.

        Do not wait for the nightly run. The extractions queue on the
        transcode lane."""

        from flask import current_app

        job = current_app.maintenance_queue.enqueue(
            "app.frames.refresh_frame_pool_task",
            job_timeout=3600,
            description="Refreshing the Name That Frame pool",
        )
        click.echo(f"Enqueued frame-pool refresh as {job.id}")

    @app.cli.group()
    def tv():
        """Manage the TV metadata."""
        pass

    @tv.command()
    @click.argument("series_id", type=int)
    @click.argument("new_title")
    def rename(series_id, new_title):
        """Rename a TV series on the disk and in the database.

        This is the Plex-disambiguation fix. The S3 keys deliberately
        stay the same."""

        from flask import current_app

        job = current_app.file_queue.enqueue(
            "app.series_rename.rename_tv_series_task",
            args=(series_id, new_title),
            job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
            description=f"Renaming TV series {series_id} to '{new_title}'",
        )
        click.echo(f"Enqueued series rename as {job.id}")

    @app.cli.group()
    def catalog():
        """Manage the exclusion list of the film catalog."""
        pass

    @catalog.command()
    @click.argument("movie_id", type=int)
    def exclude(movie_id):
        """Delete a false catalog record and block its TMDB id from
        automatic creation in the future.

        Use this for Wikidata junk, such as an unfinished film with a
        stale TMDB id. This refuses records with files or diary rows.
        Those are real library data, not catalog junk."""

        from app import db
        from app.models import CatalogExclusion, Movie, UserMovieReview

        movie = db.session.get(Movie, movie_id)
        if movie is None:
            click.echo(f"No movie record with id {movie_id}")
            return
        title = f"{movie.tmdb_title or movie.title} ({movie.year})"
        if movie.files.count():
            click.echo(f"'{title}' has files in the library — not catalog junk")
            return
        if UserMovieReview.query.filter_by(movie_id=movie.id).count():
            click.echo(f"'{title}' has diary entries — not catalog junk")
            return
        if movie.tmdb_id is None:
            click.echo(
                f"'{title}' has no TMDB id, so the catalog loaders can't "
                f"recreate it; deleting the record only"
            )
        elif not CatalogExclusion.query.filter_by(tmdb_id=movie.tmdb_id).first():
            db.session.add(CatalogExclusion(tmdb_id=movie.tmdb_id, title=title))
        db.session.delete(movie)
        db.session.commit()
        click.echo(
            f"Deleted '{title}'"
            + (
                f" and excluded TMDB id {movie.tmdb_id} from catalog loads"
                if movie.tmdb_id
                else ""
            )
        )

    @app.cli.group()
    def triage():
        """Manage the subtitle-triage inspection aids."""
        pass

    @triage.command()
    def backfill():
        """Queue snapshot generation for every existing candidate file
        that has no aids yet.

        The serial transcode queue is the throttle. Thus, this is safe
        to run against a large backlog."""

        from app.triage import forced_subtitle_candidates, triage_snapshot_dir

        queued = 0
        for entry in forced_subtitle_candidates():
            file = entry["file"]
            if os.path.isdir(triage_snapshot_dir(file.id)):
                continue
            if not os.path.isfile(
                os.path.join(app.config["LIBRARY_DIR"], file.file_path)
            ):
                continue
            app.transcode_queue.enqueue(
                "app.triage.generate_triage_snapshots",
                args=(file.id,),
                job_timeout="2h",
                description=f"Subtitle snapshots for '{file.basename}'",
            )
            queued += 1
        click.echo(f"Queued snapshot generation for {queued} file(s)")

    @triage.command()
    def backfill_audio():
        """Queue lossy-audio comparison clips (#223) for every existing
        candidate file (#212) that has none yet.

        This has the same serial-queue throttle as the subtitle
        backfill."""

        from app.triage import audio_comparison_dir, lossy_audio_candidates

        queued = 0
        for entry in lossy_audio_candidates():
            file = entry["file"]
            if os.path.isfile(
                os.path.join(audio_comparison_dir(file.id), "comparison.json")
            ):
                continue
            if not os.path.isfile(
                os.path.join(app.config["LIBRARY_DIR"], file.file_path)
            ):
                continue
            app.transcode_queue.enqueue(
                "app.triage.generate_audio_comparison",
                args=(file.id,),
                job_timeout="2h",
                description=f"Audio comparison for '{file.basename}'",
            )
            queued += 1
        click.echo(f"Queued audio comparisons for {queued} file(s)")

    @app.cli.group()
    def audio():
        """Manage the audio-track supplement pipelines."""
        pass

    @audio.command()
    @click.argument("file_id", required=False, type=int)
    def atmos(file_id):
        """Queue E-AC-3 Atmos twins for TrueHD Atmos files.

        With FILE_ID, this queues that one file. Without it, this sweeps
        the whole library for TrueHD Atmos tracks that have no twin. The
        serial transcode queue converts 1 film at a time. Each film costs
        approximately 1 dollar of MediaConvert time.
        """

        from app import db
        from app.atmos import TRUEHD_ATMOS_CODEC, maybe_enqueue_atmos_supplement
        from app.models import File, FileAudioTrack

        if file_id is not None:
            candidates = [file_id]
        else:
            candidates = sorted(
                fid
                for (fid,) in db.session.query(FileAudioTrack.file_id)
                .filter(FileAudioTrack.codec == TRUEHD_ATMOS_CODEC)
                .distinct()
            )

        queued = 0
        for fid in candidates:
            file = db.session.get(File, fid)
            if file is None:
                click.echo(f"{fid}: no such file record")
                continue
            if maybe_enqueue_atmos_supplement(fid):
                queued += 1
                click.echo(f"{fid}: queued '{file.basename}'")
            else:
                click.echo(
                    f"{fid}: skipped '{file.basename}' (twin present or already queued)"
                )
        click.echo(f"Queued {queued} of {len(candidates)} candidate file(s)")

    @recs.command()
    @click.option(
        "--weights",
        default=None,
        help="Trial class weights as a comma list, e.g. "
        "'genre=1.2,director=2.0'; unlisted classes keep their "
        "current weight.",
    )
    def evaluate(weights):
        """Show the leave-one-out ranking metrics per user.

        The metrics say how highly Fitzflix would have recommended the
        films that each user clearly liked. Use this to compare trial
        feature-class weights with the current weights."""

        from app import db
        from app.models import UserMovieReview
        from app.recommendations import FEATURE_CLASS_WEIGHTS, evaluate_user

        class_weights = dict(FEATURE_CLASS_WEIGHTS)
        if weights:
            for pair in weights.split(","):
                cls, _, value = pair.partition("=")
                if cls.strip() not in class_weights:
                    raise click.ClickException(f"Unknown feature class '{cls}'")
                class_weights[cls.strip()] = float(value)
        click.echo(f"Class weights: {class_weights}")

        user_ids = [
            user_id
            for (user_id,) in db.session.query(UserMovieReview.user_id)
            .filter(UserMovieReview.user_id.isnot(None))
            .distinct()
        ]
        for user_id in user_ids:
            metrics = evaluate_user(user_id, class_weights=class_weights)
            if metrics is None:
                click.echo(f"user {user_id}: not enough positive films to measure")
                continue
            click.echo(
                f"user {user_id}: {metrics['positives']} positives, "
                f"mean percentile {metrics['mean_percentile']:.3f} "
                f"(0 = always ranked first), "
                f"hit@10 {metrics['hit_at_10']:.1%}, "
                f"hit@25 {metrics['hit_at_25']:.1%}"
            )

    @app.cli.group()
    def smb():
        """Probe the library files for the SMB lost-handle state."""
        pass

    @smb.command()
    @click.option(
        "--file-id",
        "file_ids",
        multiple=True,
        type=int,
        help="Probe these file ids; repeatable.",
    )
    @click.option(
        "--since",
        default=None,
        type=int,
        help="Probe every file written in the last N minutes — the way to "
        "ask a finished batch which files it broke.",
    )
    @click.option("--all", "everything", is_flag=True, help="Probe the whole library.")
    def probe(file_ids, since, everything):
        """Open and close files to find the ones whose handle the NAS lost.

        Reads still succeed on such a file. Thus, nothing else sees the
        problem until the final close of an upload fails. Each file costs
        1 open and 1 close."""

        from datetime import datetime, timedelta, timezone

        from app.models import File
        from app.smb_probe import (
            absent,
            library_path,
            lost_handle,
            probe_path,
            record_result,
            share_root,
            unmounted,
        )

        query = File.query
        if file_ids:
            query = query.filter(File.id.in_(file_ids))
        elif everything:
            pass
        else:
            minutes = since if since is not None else 60
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
            query = query.filter(File.date_updated >= cutoff)
            click.echo(f"Probing files written in the last {minutes} minute(s)")

        files = query.order_by(File.file_path).all()
        if not files:
            click.echo("No files matched")
            return

        # A row whose local copy is gone is the normal state for every
        # superseded edition. Thus, count those separately. Otherwise,
        # thousands of them would hide the few that are important. List
        # them one by one only when the user asked for the file by id.

        broken = []
        other = []
        not_local = []
        offline_shares = set()
        for file in files:
            path = library_path(file)
            result = probe_path(path)
            record_result(result, context="cli probe")
            if lost_handle(result):
                broken.append(file)
                click.echo(f"  LOST HANDLE  {file.file_path}")
            elif unmounted(result):
                offline_shares.add(share_root(path))
            elif absent(result):
                not_local.append(file)
                if file_ids:
                    click.echo(f"  not on the local volume  {file.file_path}")
            elif not result["ok"]:
                other.append(file)
                click.echo(f"  {result['message']}  {file.file_path}")

        # An unmounted share is not a probe result at all. Every file on
        # it reports missing at one time. That says nothing about handles.

        for share in sorted(offline_shares):
            click.echo(f"  SHARE NOT MOUNTED  {share} — its files were not probed")

        summary = (
            f"{len(files)} file(s) probed, {len(broken)} in the lost-handle "
            f"state, {len(other)} otherwise unreadable"
        )
        if not_local:
            summary += f", {len(not_local)} not on the local volume (not a finding)"
        if offline_shares:
            summary += f", {len(offline_shares)} share(s) not mounted"
        click.echo(summary)

    @smb.command()
    def status():
        """List the files that are recorded as failing their probe now."""

        from app.models import File
        from app.smb_probe import failing_state, healed_state

        failing = failing_state()
        pending = healed_state()

        if not failing:
            click.echo("No files are recorded as failing")

        for path in sorted(failing):
            entry = failing[path]
            click.echo(
                f"  {entry['message']} since {entry['first_seen']} "
                f"(after {entry.get('context') or 'unknown'})  {path}"
            )

        if failing:
            click.echo(f"{len(failing)} file(s) failing")

        # Recoveries wait here for a report. The clean probe of a task
        # records one. Before, it erased the duration instead.

        if pending:
            click.echo(
                f"{len(pending)} recovery(ies) recorded and not yet reported; "
                f"run 'flask smb recheck' to see how long they were stuck"
            )

        stale = File.query.filter_by(aws_untouched_stale=True).count()
        if stale:
            click.echo(
                f"{stale} file(s) have a stale S3 archive; "
                f"run 'flask smb repair' to re-archive them"
            )

    @smb.command()
    @click.option(
        "--enqueue",
        is_flag=True,
        help="Actually queue the repairs, instead of only reporting them.",
    )
    def repair(enqueue):
        """Archive again the files whose S3 copy is older than the local one.

        A lost archive update is invisible to everything else. The key
        is still there and its date is that of the old upload. Thus,
        without this command, these files would keep a pre-edit archive
        forever. A retry while the handle is still lost fails in the same
        way. Thus, this probes each file first and queues only the
        readable ones."""

        from flask import current_app

        from app.models import File
        from app.smb_probe import library_path, lost_handle, probe_path, unmounted

        files = (
            File.query.filter_by(aws_untouched_stale=True)
            .order_by(File.file_path)
            .all()
        )
        if not files:
            click.echo("No archives are marked stale")
            return

        ready = []
        blocked = []
        offline = []
        for file in files:
            result = probe_path(library_path(file))
            if lost_handle(result):
                blocked.append(file)
                click.echo(f"  BLOCKED, handle still lost  {file.file_path}")
            elif not result["ok"]:
                offline.append(file)
                reason = "share not mounted" if unmounted(result) else result["message"]
                click.echo(f"  UNREADABLE ({reason})  {file.file_path}")
            else:
                ready.append(file)
                click.echo(f"  {'QUEUED' if enqueue else 'READY'}  {file.file_path}")

                if enqueue:
                    current_app.file_queue.enqueue(
                        "app.videos.upload_task",
                        args=(
                            file.id,
                            current_app.config["AWS_UNTOUCHED_PREFIX"],
                            True,
                        ),
                        job_timeout=current_app.config["LOCALIZATION_TASK_TIMEOUT"],
                        description=f"'{file.basename}'",
                    )

        click.echo(
            f"{len(files)} stale archive(s): {len(ready)} "
            f"{'queued' if enqueue else 'ready to repair'}, "
            f"{len(blocked)} blocked by a lost handle, {len(offline)} unreadable"
        )
        if ready and not enqueue:
            click.echo("Re-run with --enqueue to queue the repairs")

    @smb.command(name="history")
    def show_history():
        """Show every recovery ever recorded, with how long each one lasted.

        recheck reports a recovery 1 time and then removes it. Thus, this
        is the only place where a duration stays. How long the state
        lasts is the number that the whole investigation wants. One
        episode never answers it. The answer accumulates here."""

        from statistics import median

        from app.smb_probe import history

        episodes = history()
        if not episodes:
            click.echo("No recoveries recorded yet")
            return

        for episode in episodes:
            held = episode.get("held_for_seconds")
            duration = f"{held / 60:6.0f} min" if held else "      ?    "
            broke = episode.get("context") or "unknown"
            click.echo(
                f"  {duration}  {episode.get('first_seen')} -> "
                f"{episode.get('healed_at')}  (broke after {broke})  "
                f"{episode['path']}"
            )

        # Every duration is a minimum. first_seen is the time when
        # something first asked. The file was already stuck at that time.

        durations = [
            e["held_for_seconds"] for e in episodes if e.get("held_for_seconds")
        ]
        click.echo(f"{len(episodes)} recovery(ies) recorded")
        if durations:
            click.echo(
                f"held for at least: min {min(durations) / 60:.0f} min, "
                f"median {median(durations) / 60:.0f} min, "
                f"max {max(durations) / 60:.0f} min"
            )

    @smb.command()
    def recheck():
        """Report every recovery, and probe the files that still fail again.

        Run this on a schedule during an investigation. How long a file
        stays in the state is the number that nothing has measured
        before. This reports a recovery 1 time and then drops it. Thus,
        run it before you need the numbers, not after."""

        from app.smb_probe import recheck as recheck_state

        report = recheck_state()
        healed, still_failing = report.healed, report.still_failing
        gone, skipped = report.gone, report.skipped
        for result in healed:
            held = result.get("held_for_seconds")

            # "at least": first_seen is the time when something first
            # asked. The file was already in the state at that time.

            duration = f" after at least {held / 60:.0f} minute(s)" if held else ""
            found_by = result.get("healed_by")
            found = (
                f", found by {found_by}" if found_by and found_by != "recheck" else ""
            )
            click.echo(f"  RECOVERED{duration}{found}  {result['path']}")
        for result in still_failing:
            click.echo(
                f"  still failing since {result['first_seen']}  {result['path']}"
            )
        for result in gone:
            click.echo(f"  GONE from the volume, dropped  {result['path']}")
        for result in skipped:
            click.echo(
                f"  SKIPPED, share not mounted ({result.get('share')}); "
                f"record kept  {result['path']}"
            )

        summary = f"{len(healed)} recovered, {len(still_failing)} still failing"
        if gone:
            summary += f", {len(gone)} gone"
        if skipped:
            summary += f", {len(skipped)} skipped (share not mounted)"
        click.echo(summary)
