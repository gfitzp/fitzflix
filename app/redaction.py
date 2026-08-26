"""Credential redaction for the application log.

TMDB, Plex, Sonarr and Radarr all take their credentials as a query
parameter, so any logged URL — most often a requests HTTPError's message
inside a traceback — carries the key with it. A single logging.Filter on
the app logger scrubs every record before any handler (file, mail,
pytest's caplog) formats it, by two complementary rules:

* ``?api_key=…``-style query parameters are blanked by name, whatever
  the value — this catches keys the config doesn't know about.
* every configured secret value (settings named *_KEY, *_TOKEN,
  *_SECRET, *_PASSWORD, plus the database URI's password) is replaced
  wherever it appears — this catches a key logged outside a URL.

Records that carry an exception get their traceback text rendered and
scrubbed here too, so a handler's formatter uses the clean cached copy
instead of re-rendering the raw exception.
"""

import logging
import re

from sqlalchemy.engine import make_url

REDACTED = "[redacted]"

# Settings whose values are credentials; matched against config names
SECRET_SETTING = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD)$")

# Query parameters that carry credentials, blanked by name
SECRET_PARAM = re.compile(
    r"(?i)\b((?:api_?key|x-plex-token|token|password|secret)=)[^&\s'\"]+"
)

# Values shorter than this aren't treated as secrets to replace: a tiny
# value would redact every unrelated occurrence of those characters
MIN_SECRET_LENGTH = 6


def secret_values(config):
    """The credential strings found in a config mapping, longest first so
    a secret that contains another is replaced whole."""

    secrets = set()
    for name, value in config.items():
        if SECRET_SETTING.search(str(name)) and isinstance(value, str):
            secrets.add(value)

    uri = config.get("SQLALCHEMY_DATABASE_URI")
    if isinstance(uri, str) and uri:
        try:
            password = make_url(uri).password
        except Exception:
            password = None
        if password:
            secrets.add(password)

    return sorted(
        (s for s in secrets if len(s) >= MIN_SECRET_LENGTH), key=len, reverse=True
    )


class SecretRedactor(logging.Filter):
    """Scrub credentials out of every record logged through a logger."""

    def __init__(self, config):
        super().__init__()
        self.secrets = secret_values(config)

    def redact(self, text):
        """The text with credential parameters and known secret values
        blanked."""

        text = SECRET_PARAM.sub(rf"\g<1>{REDACTED}", text)
        for secret in self.secrets:
            text = text.replace(secret, REDACTED)
        return text

    def filter(self, record):
        """Scrub the record's message (pre-rendering its args) and its
        traceback in place; always lets the record through."""

        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        record.msg = self.redact(message)
        record.args = ()

        if record.exc_info and not record.exc_text:
            record.exc_text = self.redact(
                logging.Formatter().formatException(record.exc_info)
            )
        return True


def install(logger, config):
    """Attach a SecretRedactor to the logger, once: both Flask instances in
    this package share the "app" logger."""

    if not any(isinstance(f, SecretRedactor) for f in logger.filters):
        logger.addFilter(SecretRedactor(config))
