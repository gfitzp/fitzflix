import sys

from redis import Redis

# The libraries to preload

from app import db, get_app, videos
from app.pipeline import PipelineWorker
from app.queues import QUEUE_NAME
from config import Config

qs = sys.argv[1:] or ["default"]

# Build the app of the worker at startup, not at the first task. Only the
# import-program workers watch the import directory. Their primary queue
# is first on the command line, after the handoff queue that each
# pipeline worker takes from first. The other programs only drain the
# import queue. Thus, they do not need their own filesystem observer.

primary = next((q for q in qs if q != QUEUE_NAME["handoff"]), qs[0])
get_app(watch_import_dir=primary == QUEUE_NAME["import"])

db.configure_mappers()

# rq 2 removed the Connection context manager. Thus, the worker receives
# its connection explicitly. PipelineWorker is a SimpleWorker that also
# writes the per-file trail entries before and after each job.

w = PipelineWorker(qs, connection=Redis.from_url(Config.REDIS_URL))
w.work()
