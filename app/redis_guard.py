"""Let a page degrade quietly while Redis is down (#476).

The queue badge, the running-task banners and the tile hydration read
Redis on each page. A Redis restart or a short outage must not make
each page fail. The callers catch REDIS_DOWN_ERRORS, call
warn_redis_down, and return an empty result. A reader that the
discovery pages share has the nothing_stored decorator (#699)."""

import copy
import functools
import time

import redis
from flask import current_app, has_request_context

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


def nothing_stored(where, default=None):
    """Return a decorator that reads a Redis fault in a reader as "nothing stored" (#699).

    The reader then calls warn_redis_down and returns a copy of default.
    A callable default gets the arguments of the reader and gives the
    value. Each other exception goes to the caller.

    The rule holds only in a web request. A page shows nothing for 1
    render, and the next render reads again. A task goes on and writes
    what it read: the DVR build deletes the lineup of a channel that got
    no films, and the catalog check decides a film for good. Thus,
    outside a request the fault goes to the caller, and the task fails."""

    def decorate(reader):
        """Wrap reader in the Redis fault rule."""

        @functools.wraps(reader)
        def guarded(*args, **kwargs):
            """Call reader. Return the default after a Redis fault."""

            try:
                return reader(*args, **kwargs)
            except REDIS_DOWN_ERRORS as error:
                if not has_request_context():
                    raise
                warn_redis_down(where, error)
                if callable(default):
                    return default(*args, **kwargs)
                return copy.deepcopy(default)

        return guarded

    return decorate
