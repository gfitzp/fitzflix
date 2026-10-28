"""Watch the import directory and queue a localization for each new file.

supervisor.py asks for the watch on the import-program workers only
(create_app with watch_import_dir). start_import_watch starts the
observer, the keeper thread that rebuilds it, and the heartbeat that the
health card counts.
"""

import os
import threading
import time
import traceback

from watchdog.events import (
    FileCreatedEvent,
    FileMovedEvent,
    FileSystemEventHandler,
)
from watchdog.observers.polling import PollingObserver

from app import enqueue_import_scan, importable_basename


class ImportEventHandler(FileSystemEventHandler):
    """Hold the handlers that watchdog runs when filesystem events occur."""

    def __init__(self, app):
        super().__init__()
        self.app = app

    def process_new_file(self, path):
        """Queue a localization for a file that appeared in the import directory."""

        # Create a Redis lock with the filename. This prevents multiple
        # workers from getting the same file at one time.

        app = self.app
        lock = app.lock_manager.lock(os.path.basename(path), 30000)
        if lock:
            # The job id is the file basename. enqueue_localization adds
            # the file only if no localization of it waits or runs, and
            # no space wait holds it (#262 review, #287).

            from app.importing import enqueue_localization

            # A failed enqueue must not keep the lock or stop the dispatch
            # thread of watchdog (#523). The sweep of the keeper finds
            # the file again.

            try:
                with app.app_context():
                    if enqueue_localization(path, quiet=True):
                        app.logger.info(
                            f"'{os.path.basename(path)}' Found in import directory"
                        )
            except Exception:
                app.logger.error(traceback.format_exc())
            finally:
                app.lock_manager.unlock(lock)

    def on_moved(self, event):
        """Process a file when it is moved in the watched directory."""

        # Process only the moved files that were invisible before.

        if (
            not importable_basename(os.path.basename(event.src_path))
            and importable_basename(os.path.basename(event.dest_path))
            and os.path.isfile(event.dest_path)
        ):
            self.process_new_file(event.dest_path)

    def on_created(self, event):
        """Process a file when it appears in the watched directory."""

        # Process only the files that are not invisible or transient.

        if importable_basename(os.path.basename(event.src_path)) and os.path.isfile(
            event.src_path
        ):
            self.process_new_file(event.src_path)

    def on_any_event(self, event):
        """Log any filesystem event."""

        self.app.logger.debug(event)


def start_import_watch(app):
    """Watch the import directory of app for new files.

    The polling emitter stops itself permanently on any OSError from the
    mount. Thus, a keeper thread rebuilds the observer each time it
    dies. Then the thread sweeps the directory for the files that
    arrived while the observer was blind."""

    event_handler = ImportEventHandler(app)

    def start_observer():
        observer = PollingObserver()
        # Only the created and moved file events are important to
        # the handler. Thus, the emitter filters all other events
        # before dispatch.
        observer.schedule(
            event_handler,
            path=app.config["IMPORT_DIR"],
            recursive=False,
            event_filter=[FileCreatedEvent, FileMovedEvent],
        )
        observer.start()
        return observer

    def enqueue_import_sweep():
        # The keeper thread has no app context of its own (#394). The
        # enqueue reads the task spec, and a timeout can be the name of
        # a config setting.
        with app.app_context():
            enqueue_import_scan(app.import_queue)

    def write_observer_heartbeat():
        # This is a per-process heartbeat that expires. The admin
        # health card counts these keys to show how many processes
        # watch the import directory. A dead or wedged process stops
        # the refresh.

        from app.maintenance import OBSERVER_HEARTBEAT_SECONDS, OBSERVER_KEY_PREFIX

        try:
            app.redis.set(
                f"{OBSERVER_KEY_PREFIX}{os.getpid()}",
                int(time.time()),
                ex=OBSERVER_HEARTBEAT_SECONDS,
            )
        except Exception:
            pass

    def observer_healthy(observer):
        # The failed emitter thread stays in the emitters set after it
        # dies. Thus, check if it is alive, not if it is present.

        return bool(
            observer.is_alive()
            and observer.emitters
            and all(emitter.is_alive() for emitter in observer.emitters)
        )

    def keep_observer_alive(observer):
        while True:
            time.sleep(60)
            try:
                if not observer_healthy(observer):
                    app.logger.warning(
                        "Import directory observer died; "
                        "rebuilding it and sweeping for missed files"
                    )
                    try:
                        observer.stop()
                    except Exception:
                        pass
                    try:
                        observer = start_observer()
                    except OSError as e:
                        # The import directory is still missing. Try
                        # again at the next check.
                        app.logger.warning(
                            f"Import directory observer did not start: {e}"
                        )
                        observer = PollingObserver()
                        continue
                    enqueue_import_sweep()
                if observer_healthy(observer):
                    write_observer_heartbeat()
            except Exception:
                app.logger.error(traceback.format_exc())

    # A missing import directory (the SSD is not mounted) makes the
    # first start fail. The worker must still start and serve its
    # queues. Thus, the keeper thread starts the observer later.

    try:
        observer = start_observer()
    except Exception:
        app.logger.warning(
            f"Import directory observer did not start "
            f"({app.config['IMPORT_DIR']}); trying again every minute"
        )
        observer = PollingObserver()

    # A process whose observer did not start is blind. The health card
    # must not count it (#523).

    if observer_healthy(observer):
        write_observer_heartbeat()
    threading.Thread(
        target=keep_observer_alive,
        args=(observer,),
        daemon=True,
        name="import-observer-keeper",
    ).start()

    # Process the files that arrived while the application did not
    # watch.

    enqueue_import_sweep()
