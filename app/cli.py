import os

import click

from app import enqueue_import_scan
from app.models import Movie, TVSeries


def register(app):
    """Attach the application's CLI commands to the Flask app."""

    @app.cli.group()
    def refresh():
        """Refresh data from various services."""
        pass

    @refresh.command()
    def criterion():
        """Refresh Criterion Collection info from Wikipedia."""

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
        """Refresh library information from TMDB."""

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

        # The refresh's network phase belongs on the request queue; it hands
        # its payload to the sql queue for the database writes

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
        """Refresh metadata for file having specified file ID."""

        app.file_queue.enqueue(
            "app.videos.track_metadata_scan_task",
            args=(int(file_id),),
            job_timeout=app.config["MKVPROPEDIT_TASK_TIMEOUT"],
            description=f"Refreshing metadata for file ID {file_id}",
        )
        app.logger.info(f"Refreshing metadata for file ID {file_id}")

    @app.cli.command()
    def sync():
        """Sync library with AWS storage."""

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
        """Scan import directory for files to be imported."""

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
        """Manage the AWS infrastructure Fitzflix depends on."""
        pass

    @aws.command()
    @click.option(
        "--force",
        is_flag=True,
        help="Proceed even if the bucket reports fewer lifecycle rules "
        "than the newest local snapshot (i.e. the reduction is intended).",
    )
    def provision(force):
        """Idempotently create the S3 bucket, lifecycle rules, SQS queue,
        and restore-notification wiring described in the README. Safe to
        re-run: existing configuration is preserved and reported, and the
        as-found configuration is snapshotted before any change."""

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

    @app.cli.group()
    def recs():
        """Manage the film recommendation engine."""
        pass

    @recs.command()
    def recompute():
        """Recompute and store every reviewer's recommendations now,
        instead of waiting for the nightly run."""

        from app.recommendations import recompute_recommendations

        recompute_recommendations()
        click.echo("Recommendations recomputed")

    @recs.command()
    def streaming():
        """Recompute and store every eligible user's streaming rail now,
        instead of waiting for the nightly run."""

        from app.streaming_rail import recompute_streaming_rail

        recompute_streaming_rail()
        click.echo("Streaming rail recomputed")

    @recs.command()
    def leaving():
        """Fetch and store the leaving-Criterion film set now, instead
        of waiting for the monthly run."""

        from app.leaving_criterion import refresh_leaving_criterion

        refresh_leaving_criterion()
        click.echo("Leaving-Criterion set refreshed")

    @recs.command()
    def awards():
        """Refresh every film's Wikidata award records now, instead of
        waiting for the weekly run — the film-item pass first, then the
        person-item craft backfill layered on top of it."""

        from app.awards import refresh_movie_awards, refresh_person_awards

        click.echo(refresh_movie_awards())
        click.echo(refresh_person_awards())

    @recs.command()
    @click.argument("dataset", type=click.Path(exists=True, file_okay=False))
    def copref(dataset):
        """Rebuild the MovieLens co-preference similarity table from an
        extracted ml-32m dataset directory. Needs numpy and scipy
        installed ad hoc — they're build-time tools, not runtime
        dependencies (see app/copref.py for the dataset source)."""

        from app.copref import build_copref_table

        click.echo(build_copref_table(dataset))

    @app.cli.group()
    def transcodes():
        """Manage the derived transcoded copies (#19)."""
        pass

    @transcodes.command()
    def adopt():
        """Adopt untracked transcodes: walk TRANSCODES_DIR and create
        DerivedFile rows for every copy whose source is identifiable."""

        from flask import current_app

        job = current_app.file_queue.enqueue(
            "app.transcodes.adopt_transcodes_task",
            job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
            description="Adopting untracked transcoded copies",
        )
        click.echo(f"Enqueued transcode adoption as {job.id}")

    @app.cli.group()
    def catalog():
        """Manage the film catalog's exclusion list."""
        pass

    @catalog.command()
    @click.argument("movie_id", type=int)
    def exclude(movie_id):
        """Delete a bogus catalog record and bar its TMDb id from ever
        being auto-created again — for Wikidata junk like an unfinished
        film carrying a stale TMDb id. Refuses records with files or
        diary rows: those are real library data, not catalog junk."""

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
                f"'{title}' has no TMDb id, so the catalog loaders can't "
                f"recreate it; deleting the record only"
            )
        elif not CatalogExclusion.query.filter_by(tmdb_id=movie.tmdb_id).first():
            db.session.add(CatalogExclusion(tmdb_id=movie.tmdb_id, title=title))
        db.session.delete(movie)
        db.session.commit()
        click.echo(
            f"Deleted '{title}'"
            + (
                f" and excluded TMDb id {movie.tmdb_id} from catalog loads"
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
        that has no aids yet — the serial transcode queue is the
        throttle, so this is safe to run against a large backlog."""

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

    @app.cli.group()
    def audio():
        """Manage the audio-track supplement pipelines."""
        pass

    @audio.command()
    @click.argument("file_id", required=False, type=int)
    def atmos(file_id):
        """Queue E-AC-3 Atmos twins for TrueHD Atmos files.

        With FILE_ID, queues that one file; without, sweeps the whole
        library for TrueHD Atmos tracks lacking their twin — the serial
        transcode queue converts one film at a time, and each film
        costs roughly a dollar of MediaConvert time.
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
        """Leave-one-out ranking metrics per user: how highly the films
        each user demonstrably liked would have been recommended. Use to
        compare trial feature-class weights against the current ones."""

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
