import sys
import traceback

from redis import Redis

# The libraries to preload

from app import db, get_app, videos
from app.pipeline import PipelineWorker
from app.queues import QUEUE_NAME
from app.worker_recovery import reclaim_dead_workers
from config import Config

qs = sys.argv[1:] or ["default"]

# Build the app of the worker at startup, not at the first task. Only the
# import-program workers watch the import directory. Their primary queue
# is first on the command line, after the handoff queue that each
# pipeline worker takes from first. The other programs only drain the
# import queue. Thus, they do not need their own filesystem observer.

primary = next((q for q in qs if q != QUEUE_NAME["handoff"]), qs[0])
flask_app = get_app(watch_import_dir=primary == QUEUE_NAME["import"])

db.configure_mappers()

# rq 2 removed the Connection context manager. Thus, the worker receives
# its connection explicitly. PipelineWorker is a SimpleWorker that also
# writes the per-file trail entries before and after each job.

w = PipelineWorker(qs, connection=Redis.from_url(Config.REDIS_URL))

# A worker of this host that died with no cleanup (a SIGKILL) left its
# job, its title lock and its staging reservation behind (#498). Reclaim
# them before this worker takes a job. A failure here must not stop the
# worker.

with flask_app.app_context():
    try:
        reclaim_dead_workers(w.connection, PipelineWorker)
    except Exception:
        flask_app.logger.error(traceback.format_exc())

    # An Atmos run that broke left its workspace on staging (#505).

    try:
        from app.atmos import sweep_stale_atmos_workspaces

        sweep_stale_atmos_workspaces()
    except Exception:
        flask_app.logger.error(traceback.format_exc())

w.work()
