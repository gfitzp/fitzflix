"""Run the rq scheduler against the application's configured Redis.

The bare rqscheduler CLI defaults to localhost db 0 and never reads .env
(it only honors its own RQ_REDIS_* variables, which nothing here sets), so
it matches the app's Redis only by coincidence. Resolving the connection
through Config keeps the scheduler on the same database as every other
process, whatever REDIS_URL says.
"""

from redis import Redis
from rq_scheduler import Scheduler
from rq_scheduler.utils import setup_loghandlers

from config import Config

setup_loghandlers("INFO")

Scheduler(connection=Redis.from_url(Config.REDIS_URL), interval=60.0).run()
