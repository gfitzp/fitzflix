"""Hold the `flask smb` commands: the SMB lost-handle probes of the library files.

app/cli.py calls register_cli. The commands import app.smb_probe when
they run. Thus, the CLI registration imports no task module. The body
of each command is a module function, and register_cli only attaches
the decorators. Thus, a test can call a body with no CliRunner.
"""

import functools

import click

from app.tasks import enqueue_task


def _decorated(function, *decorators):
    """Return a new command callback that calls function, with the decorators applied.

    The first decorator is the outermost one, as in the decorator syntax.
    The click options take their order from it. A click option writes
    to its function. Thus, each registration decorates a new wrapper,
    never the module function itself. The wrapper keeps the docstring,
    which is the help of the command."""

    @functools.wraps(function)
    def callback(*args, **kwargs):
        return function(*args, **kwargs)

    for decorator in reversed(decorators):
        callback = decorator(callback)
    return callback


def probe_files(file_ids, since, everything):
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
        share_responsive,
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
    responsive = {}
    for file in files:
        path = library_path(file)

        # A wedged share hangs the open of the probe. Thus, ask each
        # share 1 time, with a timeout, before its first file (#345).
        # The recheck task and the repair do the same.

        share = share_root(path)
        if share not in responsive:
            responsive[share] = share_responsive(path)
        if not responsive[share]:
            offline_shares.add(share)
            continue
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
        click.echo(
            f"  SHARE NOT MOUNTED OR NOT RESPONDING  {share} — its files "
            f"were not probed"
        )

    summary = (
        f"{len(files)} file(s) probed, {len(broken)} in the lost-handle "
        f"state, {len(other)} otherwise unreadable"
    )
    if not_local:
        summary += f", {len(not_local)} not on the local volume (not a finding)"
    if offline_shares:
        summary += f", {len(offline_shares)} share(s) not mounted"
    click.echo(summary)


def report_status():
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

    stale = File.query.filter_by(aws_stale=True).count()
    if stale:
        click.echo(
            f"{stale} file(s) have a stale S3 archive; "
            f"run 'flask smb repair' to re-archive them"
        )


def repair_stale_archives(enqueue):
    """Archive again the files whose S3 copy is older than the local one.

    A lost archive update is invisible to everything else. The key
    is still there and its date is that of the old upload. Thus,
    without this command, these files would keep a pre-edit archive
    forever. A retry while the handle is still lost fails in the same
    way. Thus, this probes each file first and queues only the
    readable ones."""

    from flask import current_app

    from app.models import File
    from app.smb_probe import (
        library_path,
        lost_handle,
        probe_path,
        share_responsive,
        share_root,
        unmounted,
    )

    files = File.query.filter_by(aws_stale=True).order_by(File.file_path).all()
    if not files:
        click.echo("No archives are marked stale")
        return

    ready = []
    blocked = []
    offline = []
    busy = []
    responsive = {}
    for file in files:
        path = library_path(file)

        # A wedged share hangs the open of the probe. Thus, ask each
        # share 1 time, with a timeout, before its first file (#489).

        share = share_root(path)
        if share not in responsive:
            responsive[share] = share_responsive(path)
        if not responsive[share]:
            offline.append(file)
            click.echo(f"  UNREADABLE (share not responding)  {file.file_path}")
            continue

        # upload_task takes no title lock. A remux or a rename of the
        # file can run now. Then the upload archives the old bytes and
        # clears the stale mark (#489). The file waits for a later run.

        if current_app.redis.exists(file.file_identifier()):
            busy.append(file)
            click.echo(f"  BUSY, a task holds the title lock  {file.file_path}")
            continue

        result = probe_path(path)
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
                enqueue_task(
                    "app.videos.upload_task",
                    file.id,
                    description=f"'{file.basename}'",
                )

    click.echo(
        f"{len(files)} stale archive(s): {len(ready)} "
        f"{'queued' if enqueue else 'ready to repair'}, "
        f"{len(blocked)} blocked by a lost handle, {len(offline)} unreadable, "
        f"{len(busy)} busy"
    )
    if ready and not enqueue:
        click.echo("Re-run with --enqueue to queue the repairs")


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

    durations = [e["held_for_seconds"] for e in episodes if e.get("held_for_seconds")]
    click.echo(f"{len(episodes)} recovery(ies) recorded")
    if durations:
        click.echo(
            f"held for at least: min {min(durations) / 60:.0f} min, "
            f"median {median(durations) / 60:.0f} min, "
            f"max {max(durations) / 60:.0f} min"
        )


def report_recheck():
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
        found = f", found by {found_by}" if found_by and found_by != "recheck" else ""
        click.echo(f"  RECOVERED{duration}{found}  {result['path']}")
    for result in still_failing:
        click.echo(f"  still failing since {result['first_seen']}  {result['path']}")
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


def register_cli(app):
    """Attach the `flask smb` group to the Flask app."""

    @app.cli.group()
    def smb():
        """Probe the library files for the SMB lost-handle state."""
        pass

    smb.command(name="probe")(
        _decorated(
            probe_files,
            click.option(
                "--file-id",
                "file_ids",
                multiple=True,
                type=int,
                help="Probe these file ids. You can give the option more than 1 time.",
            ),
            click.option(
                "--since",
                default=None,
                type=int,
                help="Probe every file written in the last N minutes. Use it "
                "to find the files that a finished batch broke.",
            ),
            click.option(
                "--all", "everything", is_flag=True, help="Probe the whole library."
            ),
        )
    )
    smb.command(name="status")(_decorated(report_status))
    smb.command(name="repair")(
        _decorated(
            repair_stale_archives,
            click.option(
                "--enqueue",
                is_flag=True,
                help="Actually queue the repairs, instead of only reporting them.",
            ),
        )
    )
    smb.command(name="history")(_decorated(show_history))
    smb.command(name="recheck")(_decorated(report_recheck))
