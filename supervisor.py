import sys
from rq import Connection, SimpleWorker, Worker

# Libraries to preload

from app import db, videos

with Connection():
    db.configure_mappers()
    qs = sys.argv[1:] or ["default"]
    w = SimpleWorker(qs)
    w.work()