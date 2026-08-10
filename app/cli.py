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
