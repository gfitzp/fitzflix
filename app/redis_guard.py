"""Let a page degrade quietly while Redis is down (#476).

The queue badge, the running-task banners and the tile hydration read
Redis on each page. A Redis restart or a short outage must not make
each page fail. The callers catch REDIS_DOWN_ERRORS, call
warn_redis_down, and return an empty result."""

import time

import redis
from flask import current_app

REDIS_DOWN_ERRORS = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)

# Each process logs 1 warning per minute at most. The polls of each open
# tab would otherwise fill the log during an outage.

WARNING_SECONDS = 60
_last_warning = {"at": None}


def warn_redis_down(where, error):
    """Log that Redis is not reachable, at most 1 time each minute."""

    now = time.monotonic()
    last = _last_warning["at"]
    if last is not None and now - last < WARNING_SECONDS:
        return
    _last_warning["at"] = now
    current_app.logger.warning(
        f"Redis is not reachable ({error}). {where} shows no data until it is back"
    )
