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
        help="Continue when the bucket reports fewer lifecycle rules "
        "than the newest local snapshot. Use it when the reduction is "
        "intentional.",
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
            + ("" if created else ". Everything was already in place")
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

    @aws.command("converge-keys")
    @click.option(
        "--apply",
        "apply_moves",
        is_flag=True,
        help="Request the moves. Without this flag, the command only lists them.",
    )
    @click.option(
        "--limit",
        type=int,
        default=None,
        help=(
            "Request no more than this number of moves. The moves that are "
            "pending already do not count."
        ),
    )
    @click.option(
        "--again",
        "again",
        is_flag=True,
        help=(
            "Request the pending moves again too. Use it for a move whose "
            "job failed."
        ),
    )
    def converge_keys(apply_moves, limit, again):
        """List the archives whose key differs from the key of their untouched name (#369).

        The key of an archive is its prefix and its sanitized untouched
        name. A rename before #369 kept the old key. With --apply, the
        command requests a move for each listed archive. The move jobs
        copy the objects on the server side. A Deep Archive object first
        gets a Bulk restore. Without --apply, nothing changes. A move
        that is pending already is listed, but not requested again
        (#384). Thus, --limit takes the next batch on each run. With
        --again, the pending moves are requested again too."""

        from app import db
        from app.aws_storage import archive_drifts, key_claims, request_archive_move

        claims = key_claims()
        drifted = archive_drifts(claims)
        total = sum(size for _, _, _, size in drifted)
        by_prefix = {}
        for _, source, _, _ in drifted:
            prefix = source.split("/", 1)[0]
            by_prefix[prefix] = by_prefix.get(prefix, 0) + 1
        click.echo(
            f"{len(drifted)} archive key(s) differ from the key of their "
            f"untouched name: {total} bytes ({total / 1024**3:.1f} GiB)"
        )
        for prefix, count in sorted(by_prefix.items()):
            click.echo(f"  {prefix}/: {count}")
        waiting = [row for row in drifted if again or row[0].aws_key_target != row[2]]
        pending = len(drifted) - len(waiting)
        if pending:
            click.echo(f"  {pending} of them are pending already")
        selected = waiting if limit is None else waiting[: max(limit, 0)]
        shown = selected if apply_moves else drifted[:5]
        for file, source, target, size in shown:
            mark = " (pending)" if file.aws_key_target == target else ""
            click.echo(f"  [{file.id}] {source} -> {target} ({size} bytes){mark}")
        if not apply_moves:
            click.echo(
                f"Dry run: nothing changed. --apply requests {len(selected)} move(s)."
            )
            return

        requested = 0
        try:
            for file, _, _, _ in selected:
                if request_archive_move(file, claims):
                    requested += 1
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        click.echo(f"Requested {requested} archive move(s).")
