"""Hold the `flask triage` commands: the backfills of the triage inspection aids.

app/cli.py calls register_cli. The commands import app.triage when
they run. Thus, the CLI registration imports no task module.
"""

import os

import click

from app.tasks import enqueue_task


def _has_subtitle_aids(snapshot_dir):
    """Return True if the triage directory of a file holds subtitle aids (#550).

    The lossy-audio comparison is the "audio" child of the same
    directory. A file with only that child has no subtitle aids yet."""

    try:
        return any(name != "audio" for name in os.listdir(snapshot_dir))
    except FileNotFoundError:
        return False


def register_cli(app):
    """Attach the `flask triage` group to the Flask app."""

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
            if _has_subtitle_aids(triage_snapshot_dir(file.id)):
                continue
            if not os.path.isfile(
                os.path.join(app.config["LIBRARY_DIR"], file.file_path)
            ):
                continue
            enqueue_task(
                "app.triage.generate_triage_snapshots",
                file.id,
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
            enqueue_task(
                "app.triage.generate_audio_comparison",
                file.id,
                description=f"Audio comparison for '{file.basename}'",
            )
            queued += 1
        click.echo(f"Queued audio comparisons for {queued} file(s)")
