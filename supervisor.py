import sys
from rq import Connection, SimpleWorker

# Libraries to preload

from app import db, get_app, videos

qs = sys.argv[1:] or ["default"]

# Build the worker's app at startup rather than at first task. Only the
# import-program workers (their primary queue is first on the command line)
# watch the import directory; the other programs that merely drain the
# import queue don't need their own filesystem observer.

get_app(watch_import_dir=qs[0] == "fitzflix-import")

with Connection():
    db.configure_mappers()
    w = SimpleWorker(qs)
    w.work()
