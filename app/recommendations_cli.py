"""Hold the `flask recs` commands: the film recommendation engine and its feeds.

app/cli.py calls register_cli. The commands import the engine modules
when they run. Thus, the CLI registration imports no task module.
"""

import click


def register_cli(app):
    """Attach the `flask recs` group to the Flask app."""

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
