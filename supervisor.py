import sys

from redis import Redis
from rq import SimpleWorker

# Libraries to preload

from app import db, get_app, videos
from config import Config

qs = sys.argv[1:] or ["default"]

# Build the worker's app at startup rather than at first task. Only the
# import-program workers (their primary queue is first on the command line)
# watch the import directory; the other programs that merely drain the
# import queue don't need their own filesystem observer.

get_app(watch_import_dir=qs[0] == "fitzflix-import")

db.configure_mappers()

# rq 2 removed the Connection context manager, so the worker takes its
# connection explicitly

w = SimpleWorker(qs, connection=Redis.from_url(Config.REDIS_URL))
w.work()
