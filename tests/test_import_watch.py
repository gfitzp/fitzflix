"""Test the event handler of the import watch (app/import_watch.py) directly."""

import os


def test_the_handler_queues_new_and_renamed_files_only(app, incoming_dir):
    from watchdog.events import FileCreatedEvent, FileMovedEvent

    from app import safe_job_id
    from app.import_watch import ImportEventHandler

    handler = ImportEventHandler(app)
    visible = os.path.join(incoming_dir, "Handler Test (2021) - [DVD].mkv")
    hidden = os.path.join(incoming_dir, ".Handler Hidden (2021) - [DVD].mkv")
    renamed = os.path.join(incoming_dir, "Handler Moved (2021) - [DVD].mkv")
    for path in (visible, hidden, renamed):
        with open(path, "wb") as f:
            f.write(b"video")
    try:
        handler.on_created(FileCreatedEvent(visible))
        handler.on_created(FileCreatedEvent(hidden))
        handler.on_moved(FileMovedEvent(renamed + ".partial", renamed))
        handler.on_moved(FileMovedEvent(visible, renamed))
        ids = app.import_queue.job_ids
        assert safe_job_id(os.path.basename(visible)) in ids
        assert safe_job_id(os.path.basename(renamed)) in ids
        assert safe_job_id(os.path.basename(hidden)) not in ids
        assert len(ids) == 2
    finally:
        for path in (visible, hidden, renamed):
            os.remove(path)


def test_the_keeper_sweep_works_without_an_app_context(app, monkeypatch):
    """The keeper thread enqueues the sweep with no app context (#394).

    A task spec with a config-named timeout reads current_app.config."""

    import threading

    from flask import has_app_context

    import app.import_watch as import_watch
    from app.tasks import TASKS, TaskSpec

    class Observer:
        emitters = ()

        def schedule(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def is_alive(self):
            return True

    class Thread:
        def __init__(self, target=None, args=(), **kwargs):
            self.target = target

        def start(self):
            pass

    name = "app.videos.manual_import_task"
    monkeypatch.setitem(TASKS, name, TaskSpec("maintenance", "SQL_TASK_TIMEOUT"))
    monkeypatch.setattr(import_watch, "PollingObserver", Observer)
    monkeypatch.setattr(threading, "Thread", Thread)
    assert not has_app_context()

    import_watch.start_import_watch(app)
    (job,) = [job for job in app.import_queue.jobs if job.func_name == name]
    assert job.timeout == app.config["SQL_TASK_TIMEOUT"]


def test_a_blind_start_writes_no_heartbeat(app, monkeypatch):
    """A process whose observer did not start is not counted (#523)."""

    import threading

    import app.import_watch as import_watch
    from app.maintenance import OBSERVER_KEY_PREFIX

    class Broken:
        emitters = ()

        def schedule(self, *args, **kwargs):
            raise OSError("no import directory")

        def is_alive(self):
            return False

    class Thread:
        def __init__(self, target=None, args=(), **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(import_watch, "PollingObserver", Broken)
    monkeypatch.setattr(threading, "Thread", Thread)
    monkeypatch.setattr(import_watch, "enqueue_import_scan", lambda queue: None)
    import_watch.start_import_watch(app)
    assert not app.redis.exists(f"{OBSERVER_KEY_PREFIX}{os.getpid()}")


def test_a_failed_enqueue_releases_the_basename_lock(app, incoming_dir, monkeypatch):
    """An enqueue that raises frees the lock and does not raise (#523).

    The exception killed the dispatch thread of watchdog."""

    import app.importing as importing
    from app.import_watch import ImportEventHandler

    def broken(path, **kwargs):
        raise ConnectionError("Redis went away")

    monkeypatch.setattr(importing, "enqueue_localization", broken)
    path = os.path.join(incoming_dir, "Broken Enqueue (2021) - [DVD].mkv")
    ImportEventHandler(app).process_new_file(path)
    lock = app.lock_manager.lock(os.path.basename(path), 1000)
    assert lock
    app.lock_manager.unlock(lock)


def test_a_failed_basename_lock_does_not_raise(app, incoming_dir, monkeypatch):
    """A Redis fault at the lock call escaped the handler of #523 (#568)."""

    from app.import_watch import ImportEventHandler

    def broken(resource, ttl):
        raise ConnectionError("Redis went away")

    monkeypatch.setattr(app.lock_manager, "lock", broken)
    path = os.path.join(incoming_dir, "Broken Lock (2021) - [DVD].mkv")
    ImportEventHandler(app).process_new_file(path)
