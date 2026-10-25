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
