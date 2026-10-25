"""Hold the `flask audio` commands: the audio-track supplement pipelines.

app/cli.py calls register_cli. The commands import app.atmos when they
run. Thus, the CLI registration imports no task module.
"""

import click


def register_cli(app):
    """Attach the `flask audio` group to the Flask app."""

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
        jobs go on the import queue. Thus, a sweep does not delay the
        files that are already in the pipeline. Each film costs
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
