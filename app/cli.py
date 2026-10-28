import click

from app.tasks import enqueue_task
from app import enqueue_import_scan
from app.models import Movie, TVSeries


def register(app):
    """Attach the CLI commands of the application to the Flask app.

    The refresh group and the one-line enqueue commands live here. The
    aws, recs, triage, audio and smb groups live next to the modules
    that they drive, each with its own register_cli."""

    @app.cli.group()
    def refresh():
        """Refresh data from external services."""
        pass

    @refresh.command()
    def criterion():
        """Refresh the Criterion Collection info from Wikidata."""

        # The same queue and timeout as the monthly cron row (#343). The
        # task makes a Wikidata request for each movie. On the single sql
        # worker it blocked every database job, and 10 minutes was too
        # short for it.

        enqueue_task(
            "app.videos.refresh_criterion_collection_info",
            description="Refreshing Criterion Collection information for all movies in library",
        )
        app.logger.info("Refreshing Criterion Collection information from Wikipedia")

    @refresh.command()
    @click.argument("library", required=False, type=click.Choice(["movie", "tv"]))
    @click.argument("tmdb_id", required=False, type=int)
    def tmdb(library=None, tmdb_id=None):
        """Refresh the library information from TMDB.

        With no library, this refreshes each movie and each TV series.
        With a library, it refreshes only that library. With a TMDB id
        too, it refreshes only that title."""

        # An id of 0 must not read as "no id". That refreshed the whole
        # library (#409).

        if tmdb_id is not None and tmdb_id < 1:
            raise click.ClickException(f"A TMDB id is 1 or more, not {tmdb_id}")

        movies = []
        tv_shows = []

        if library in (None, "movie"):
            query = Movie.query.filter(Movie.tmdb_id != None)
            if tmdb_id is not None:
                query = query.filter(Movie.tmdb_id == tmdb_id)
            movies = query.order_by(Movie.title.asc(), Movie.year.asc()).all()

        if library in (None, "tv"):
            query = TVSeries.query.filter(TVSeries.tmdb_id != None)
            if tmdb_id is not None:
                query = query.filter(TVSeries.tmdb_id == tmdb_id)
            tv_shows = query.order_by(TVSeries.title.asc()).all()

        # A TMDB id that matches nothing is a mistake of the user. Say so,
        # and exit with an error (#344).

        if tmdb_id is not None and not movies and not tv_shows:
            raise click.ClickException(f"No title in the library has TMDB id {tmdb_id}")

        # The network phase of the refresh belongs on the request queue.
        # It gives its payload to the sql queue for the database writes.

        if movies:
            for movie in movies:
                enqueue_task(
                    "app.videos.refresh_tmdb_info",
                    "Movies",
                    movie.id,
                    movie.tmdb_id,
                    description=f"Refreshing TMDB data for '{movie.title} ({movie.year})'",
                )
                app.logger.info(
                    f"Queueing TMDB refresh for '{movie.title} ({movie.year})'"
                )

        if tv_shows:
            for tv in tv_shows:
                enqueue_task(
                    "app.videos.refresh_tmdb_info",
                    "TV Shows",
                    tv.id,
                    tv.tmdb_id,
                    description=f"Refreshing TMDB data for '{tv.title}'",
                )
                app.logger.info(f"Queueing TMDB refresh for '{tv.title}'")

    @refresh.command()
    @click.argument("file_id", type=int)
    def file(file_id):
        """Refresh the metadata for the file with the specified file ID."""

        from app import db
        from app.models import File

        if db.session.get(File, file_id) is None:
            raise click.ClickException(f"No file has id {file_id}")

        enqueue_task(
            "app.videos.track_metadata_scan_task",
            int(file_id),
            description=f"Refreshing metadata for file ID {file_id}",
        )
        app.logger.info(f"Refreshing metadata for file ID {file_id}")

    @app.cli.command()
    def sync():
        """Sync the library with the AWS storage."""

        enqueue_task(
            "app.videos.sync_aws_s3_storage_task",
            description="Syncing files with AWS S3 storage",
        )
        app.logger.info("Queueing the AWS S3 storage sync")

    @app.cli.command()
    def scan():
        """Scan the import directory for files to import."""

        enqueue_import_scan(app.request_queue, at_front=True)
        app.logger.info("Scanning import directory for files")

    @app.cli.command()
    def sqs():
        """Check for restored files at AWS S3."""

        enqueue_task(
            "app.videos.sqs_retrieve_task",
            # The command asks for a poll now. Thus, it does not wait behind
            # the maintenance work of the hourly cron.
            queue="request",
            description="Polling AWS SQS for files to download",
        )
        app.logger.info("Polling AWS SQS for files to download")

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
    def frames():
        """Manage the Name That Frame pool."""
        pass

    @frames.command()
    def refresh():
        """Prune and fill the frame pool now.

        Do not wait for the nightly run. The extractions queue on the
        transcode lane."""

        job = enqueue_task(
            "app.frames.refresh_frame_pool_task",
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

        This is the Plex-disambiguation fix. The untouched names get the
        new title (#362), and the archive keys follow them (#369)."""

        from app import db
        from app.series_rename import rename_refusal, rename_timeout

        series = db.session.get(TVSeries, series_id)
        if series is None:
            raise click.ClickException(f"No TV series {series_id}")
        refusal = rename_refusal(series, new_title)
        if refusal:
            raise click.ClickException(f"Not renamed, because {refusal}")
        job = enqueue_task(
            "app.series_rename.rename_tv_series_task",
            series_id,
            new_title,
            description=f"Renaming TV series {series_id} to '{new_title}'",
            timeout=rename_timeout(series),
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
        from app.aws_storage import untouched_key_still_claimed
        from app.custom_artwork import remove_custom_artwork_after_commit
        from app.models import (
            ArchivedOriginal,
            CatalogExclusion,
            Movie,
            UserMovieReview,
        )

        movie = db.session.get(Movie, movie_id)
        if movie is None:
            click.echo(f"No movie record with id {movie_id}")
            return
        title = f"{movie.tmdb_title or movie.title} ({movie.year})"
        if movie.files.count():
            click.echo(f"'{title}' has files in the library. It is not catalog junk")
            return
        if UserMovieReview.query.filter_by(movie_id=movie.id).count():
            click.echo(f"'{title}' has diary entries. It is not catalog junk")
            return
        if movie.tmdb_id is None:
            click.echo(
                f"'{title}' has no TMDB id. Thus, the catalog loaders cannot "
                f"make it again. Only the record is deleted"
            )
        elif not CatalogExclusion.query.filter_by(tmdb_id=movie.tmdb_id).first():
            db.session.add(CatalogExclusion(tmdb_id=movie.tmdb_id, title=title))

        # The archive-only originals of the film go with it (#464). The
        # S3 objects go after the commit, unless a record still claims
        # the key.

        original_keys = []
        for original in ArchivedOriginal.query.filter_by(movie_id=movie.id).all():
            original_keys.append(original.aws_key)
            db.session.delete(original)
        # The custom poster tree of the film goes after the commit (#463).

        remove_custom_artwork_after_commit(db.session, "movie", movie.id)
        db.session.delete(movie)
        db.session.commit()
        for aws_key in original_keys:
            if not untouched_key_still_claimed(aws_key):
                enqueue_task(
                    "app.videos.aws_delete",
                    aws_key,
                    description=f"Deleting '{aws_key}' from AWS",
                )
        click.echo(
            f"Deleted '{title}'"
            + (
                f" and excluded TMDB id {movie.tmdb_id} from catalog loads"
                if movie.tmdb_id
                else ""
            )
        )

    @catalog.command()
    def reassign():
        """Assign the stored Criterion releases to each movie again.

        This reads the releases that Redis stores. It does not query
        Wikidata. It applies a change of the assignment rule to the
        existing records (#472)."""

        from app.criterion_catalog import reassign_stored_criterion_releases

        set_titles = reassign_stored_criterion_releases()
        if set_titles is None:
            click.echo("No stored Criterion releases. Nothing changed")
            return
        click.echo(f"Gave a box-set title to {set_titles} movie record(s)")

    # The groups that belong to 1 module each live next to that module.

    from app import (
        atmos_cli,
        aws_cli,
        recommendations_cli,
        smb_cli,
        triage_cli,
    )

    for module in (aws_cli, recommendations_cli, triage_cli, atmos_cli, smb_cli):
        module.register_cli(app)
