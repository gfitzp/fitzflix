"""Read and write the JSON values that Fitzflix caches in Redis.

A cached lookup has 3 states. MISS means that Fitzflix did not ask, or
that the entry expired. Thus, it must ask. JSON null means that it asked
and there is nothing, for example a film that TMDB deleted. Any other
value is the answer. A transient error caches nothing. Then the next
read asks again.

Before this module, 3 conventions for "nothing" existed side by side:
JSON null, an empty string, and an empty list or dict. A reader of one
convention could take the "nothing" of another for a real value.
"""

import json

from flask import current_app


class _Miss:
    """The type of MISS. A caller compares with "is MISS"."""

    def __repr__(self):
        return "MISS"


MISS = _Miss()


def cached_json(key):
    """Return the cached value of key, or MISS.

    A value that is not JSON is MISS too. An entry that an older format
    wrote is then fetched again and written as JSON."""

    raw = current_app.redis.get(key)
    if raw is None:
        return MISS
    try:
        return json.loads(raw)
    except ValueError:
        return MISS


def cache_json(key, value, ttl):
    """Cache value as JSON under key for ttl seconds. None caches "nothing"."""

    current_app.redis.set(key, json.dumps(value), ex=ttl)


def cached_fetch(key, ttl, fetch, miss_ttl=None):
    """Return the cached value of key. On a MISS, fetch it and cache it.

    fetch() returns (value, ok). ok=False is a transient error. Then
    nothing is cached, and the value goes back as it is. A value of None
    with ok=True is "nothing". It is cached for miss_ttl seconds, or for
    ttl seconds if miss_ttl is not given."""

    value = cached_json(key)
    if value is not MISS:
        return value
    value, ok = fetch()
    if ok:
        cache_json(key, value, miss_ttl if value is None and miss_ttl else ttl)
    return value
