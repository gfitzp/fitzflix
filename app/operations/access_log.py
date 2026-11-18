"""The access log of the web process.

gunicorn reads gunicorn.conf.py from the project directory at startup.
That file takes its settings from here. Each line has eight fields.
They are the time, worker pid, client address, request line, status,
response size, request seconds, and user agent. Thus, a frozen page is
attributable. Slow lines point at a slow request. No lines point at a
stalled process.

The log is access.log, next to the application log. The nightly
rotate_logs task archives both files the same way.

A token in a request path is a secret. The filter replaces it with
"<token>" before the line reaches the file. The DVR, the Plex webhook,
the password reset, and the game share routes carry one.
"""

import copy
import logging
import os
import re

ACCESS_LOG_NAME = "access.log"

# The atoms are from gunicorn. L is the request time in seconds.

ACCESS_LOG_FORMAT = '%(t)s %(p)s %(h)s "%(r)s" %(s)s %(b)s %(L)s "%(a)s"'

TOKEN_SEGMENT = re.compile(
    r"(/(?:reset-password|plex/webhook|dvr|game/brag|game/frame)/)[^/\s?\"]+"
)


def access_log_path(log_file):
    """Return the path of the access log, next to the application log."""

    return os.path.join(os.path.dirname(os.path.abspath(log_file)), ACCESS_LOG_NAME)


def redact_tokens(text):
    """Return the text with each route token replaced by "<token>"."""

    return TOKEN_SEGMENT.sub(r"\1<token>", text)


class TokenFilter(logging.Filter):
    """Redact the route tokens in the atoms of a gunicorn access record."""

    def filter(self, record):
        """Rewrite the request line and the path in the record. Keep the record."""

        atoms = record.args
        if isinstance(atoms, dict):
            for key in ("r", "U"):
                value = atoms.get(key)
                if isinstance(value, str):
                    atoms[key] = redact_tokens(value)
        return True


def logconfig(access_log):
    """Return the logging configuration of gunicorn with the access log file.

    The configuration starts from the defaults of gunicorn. Thus, the
    error log still goes to stderr, where supervisor collects it. The
    access log goes to a WatchedFileHandler. The rotate_logs task
    renames the file, and the handler opens the new file on the next
    write. Only the web process imports gunicorn. Thus, the import is
    here and not at the top of the module."""

    from gunicorn.glogging import CONFIG_DEFAULTS

    os.makedirs(os.path.dirname(access_log), exist_ok=True)
    config = copy.deepcopy(CONFIG_DEFAULTS)
    config.setdefault("filters", {})["tokens"] = {"()": TokenFilter}
    config["formatters"]["access"] = {"format": "%(message)s"}
    config["handlers"]["access_file"] = {
        "class": "logging.handlers.WatchedFileHandler",
        "filename": access_log,
        "formatter": "access",
        "filters": ["tokens"],
    }
    config["loggers"]["gunicorn.access"] = {
        "level": "INFO",
        "handlers": ["access_file"],
        "propagate": False,
        "qualname": "gunicorn.access",
    }

    # The defaults let the error log propagate to the root logger, which
    # writes to stdout. Then supervisor collects each line two times.

    config["loggers"]["gunicorn.error"]["propagate"] = False
    return config
