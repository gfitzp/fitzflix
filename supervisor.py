import sys
from rq import Connection, SimpleWorker

# Libraries to preload

from app import db, get_app, videos

# Build the worker's app at startup rather than at first task, so the
# import-directory observer begins watching immediately

get_app()

with Connection():
    db.configure_mappers()
    qs = sys.argv[1:] or ["default"]
    w = SimpleWorker(qs)
    w.work()
