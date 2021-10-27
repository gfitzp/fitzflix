from app.models import Movie, TVSeries


def register(app):
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
    def tmdb():
        """Refresh library information from TMDB."""

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

        for movie in movies:
            refresh_job = app.sql_queue.enqueue(
                "app.videos.refresh_tmdb_info",
                args=("Movies", movie.id, movie.tmdb_id),
                job_timeout=app.config["SQL_TASK_TIMEOUT"],
                description=f"Refreshing TMDB data for '{movie.title} ({movie.year})'",
            )
            app.logger.info(f"Queueing TMDB refresh for '{movie.title} ({movie.year})'")

        for tv in tv_shows:
            refresh_job = app.sql_queue.enqueue(
                "app.videos.refresh_tmdb_info",
                args=("TV Shows", tv.id, tv.tmdb_id),
                job_timeout=app.config["SQL_TASK_TIMEOUT"],
                description=f"Refreshing TMDB data for '{tv.title}'",
            )
            app.logger.info(f"Queueing TMDB refresh for '{tv.title}'")

    @app.cli.command()
    def prune():
        """Remove unreferenced files from AWS storage."""

        app.task_queue.enqueue(
            "app.videos.prune_aws_s3_storage_task",
            args=None,
            job_timeout="24h",
            description=f"Pruning extra files from AWS S3 storage",
            at_front=True,
        )
        app.logger.info("Pruning extra files from AWS S3 storage")

    @app.cli.command()
    def scan():
        """Scan import directory for files to be imported."""

        app.task_queue.enqueue(
            "app.videos.manual_import_task",
            args=(),
            job_timeout="1h",
            description="Scanning import directory for files",
            atfront=True,
        )
        app.logger.info("Scanning import directory for files")
