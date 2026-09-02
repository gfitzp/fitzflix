import sys

from redis import Redis

# The libraries to preload

from app import db, get_app, videos
from app.pipeline import PipelineWorker
from config import Config

qs = sys.argv[1:] or ["default"]

# Build the app of the worker at startup, not at the first task. Only the
# import-program workers watch the import directory. Their primary queue
# is first on the command line. The other programs only drain the import
# queue. Thus, they do not need their own filesystem observer.

get_app(watch_import_dir=qs[0] == "fitzflix-import")

db.configure_mappers()

# rq 2 removed the Connection context manager. Thus, the worker receives
# its connection explicitly. PipelineWorker is a SimpleWorker that also
# writes the per-file trail entries before and after each job.

w = PipelineWorker(qs, connection=Redis.from_url(Config.REDIS_URL))
w.work()
