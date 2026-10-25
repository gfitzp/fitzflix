"""Hold the roster of the rq queues of Fitzflix in one place.

Each entry is a short key and the Redis name of the queue. The app
attribute of a queue is the key plus "_queue", for example
app.maintenance_queue. The order is the order of the scheduler and of
the System page. The handoff queue is first, because each pipeline
worker takes from it first. This module imports nothing from the app.
Thus, scheduler.py, the tests and the task registry can read it before
an app exists.
"""

from collections import namedtuple

QueueSpec = namedtuple("QueueSpec", "key name")

ROSTER = (
    QueueSpec("handoff", "fitzflix-handoff"),
    QueueSpec("import", "fitzflix-import"),
    QueueSpec("file", "fitzflix-file-operation"),
    QueueSpec("transcode", "fitzflix-transcode"),
    QueueSpec("sql", "fitzflix-sql"),
    QueueSpec("request", "fitzflix-user-request"),
    QueueSpec("maintenance", "fitzflix-maintenance"),
)

QUEUE_NAMES = tuple(spec.name for spec in ROSTER)

QUEUE_NAME = {spec.key: spec.name for spec in ROSTER}


def attach_queues(app, queue_class):
    """Make 1 queue object per roster entry and set it on the app.

    The app gets app.<key>_queue for each entry, and app.queues, which
    maps each key to its queue in roster order."""

    app.queues = {}
    for spec in ROSTER:
        queue = queue_class(spec.name, connection=app.redis)
        app.queues[spec.key] = queue
        setattr(app, f"{spec.key}_queue", queue)


def roster_queues(app):
    """Return the queue objects of the app in roster order.

    The function reads the app attributes, not app.queues. Thus, a test
    that replaces an attribute also replaces the queue here."""

    return [getattr(app, f"{spec.key}_queue") for spec in ROSTER]


def queue_names(*keys):
    """Return the Redis names of the given roster keys, in the given order."""

    return [QUEUE_NAME[key] for key in keys]
