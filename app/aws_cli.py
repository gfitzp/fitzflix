"""Hold the `flask aws` commands: the AWS infrastructure that Fitzflix depends on.

app/cli.py calls register_cli. The commands import the AWS modules
when they run. Thus, the CLI registration imports no task module.
"""

import click


def register_cli(app):
    """Attach the `flask aws` group to the Flask app."""

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
