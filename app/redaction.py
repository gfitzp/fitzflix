"""Remove credentials from the application log.

TMDB, Plex, Sonarr and Radarr all take their credentials as a query
parameter. Thus, each logged URL carries the key with it. The most
frequent example is the message of a requests HTTPError inside a
traceback. One logging.Filter on the app logger cleans each record
before a handler (file, mail, the caplog of pytest) formats it. The
filter applies 2 rules:

* The filter blanks query parameters such as ``?api_key=...`` by name.
  The value is not important. This catches keys that the config does
  not know.
* The filter replaces each configured secret value (settings named
  *_KEY, *_TOKEN, *_SECRET, *_PASSWORD, and the password of the
  database URI) at each location. This catches a key logged outside a
  URL.

If a record carries an exception, the filter renders its traceback text
and cleans it here too. Thus, the formatter of a handler uses the clean
cached copy. It does not render the raw exception again.
"""

import logging
import os
import re

from sqlalchemy.engine import make_url

REDACTED = "[redacted]"

# The values of these settings are credentials. The pattern matches
# config names.
SECRET_SETTING = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD)$")

# Query parameters that carry credentials. The filter blanks them by name.
# A CloudFront signed URL is a bearer credential while its Signature
# parameter is intact. Thus, the filter blanks that parameter too.
SECRET_PARAM = re.compile(
    r"(?i)\b((?:api_?key|x-plex-token|token|password|secret|signature)=)[^&\s'\"]+"
)

# The filter does not replace values shorter than this as secrets. A
# very short value would redact each unrelated occurrence of those
# characters.
MIN_SECRET_LENGTH = 6


def secret_values(config):
    """Return the credential strings found in a config mapping.

    The longest string comes first. Thus, a secret that contains a
    different secret is replaced as a whole."""

    secrets = set()
    for name, value in config.items():
        if not (SECRET_SETTING.search(str(name)) and isinstance(value, str)):
            continue
        # A setting that names a file (CDN_PRIVATE_KEY) holds a path, not
        # a credential. A blanked path would hide which file is missing.
        if value.startswith(("/", "~")) or os.path.isfile(value):
            continue
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
    """Remove credentials from each record that a logger logs."""

    def __init__(self, config):
        super().__init__()
        self.secrets = secret_values(config)

    def redact(self, text):
        """Return the text with credential parameters and known secret
        values blanked."""

        text = SECRET_PARAM.sub(rf"\g<1>{REDACTED}", text)
        for secret in self.secrets:
            text = text.replace(secret, REDACTED)
        return text

    def filter(self, record):
        """Clean the message and the traceback of the record in place.

        This renders the args of the record into the message first. It
        always lets the record through."""

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
    """Attach a SecretRedactor to the logger one time only.

    Both Flask instances in this package share the "app" logger."""

    if not any(isinstance(f, SecretRedactor) for f in logger.filters):
        logger.addFilter(SecretRedactor(config))
