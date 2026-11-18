"""A Flask-Mail extension whose SMTP connection has a socket timeout (#452).

Flask-Mail opens smtplib.SMTP with no timeout. A mail server that drops
the packets, and does not refuse them, then holds the sender until the
job timeout. A gunicorn thread waits for good. MAIL_TIMEOUT is the limit
in seconds for the connect and for each later socket operation.
"""

import smtplib

from flask import current_app
from flask_mail import Connection, Mail

DEFAULT_MAIL_TIMEOUT = 30


class TimeoutConnection(Connection):
    """A Flask-Mail connection that opens its SMTP host with MAIL_TIMEOUT."""

    def configure_host(self):
        """Open the SMTP host with the timeout. Then do the steps of Flask-Mail."""

        timeout = current_app.config.get("MAIL_TIMEOUT") or DEFAULT_MAIL_TIMEOUT
        smtp = smtplib.SMTP_SSL if self.mail.use_ssl else smtplib.SMTP
        host = smtp(self.mail.server, self.mail.port, timeout=timeout)
        host.set_debuglevel(int(self.mail.debug))
        if self.mail.use_tls:
            host.starttls()
        if self.mail.username and self.mail.password:
            host.login(self.mail.username, self.mail.password)
        return host


class TimeoutMail(Mail):
    """The Flask-Mail extension with TimeoutConnection."""

    def connect(self):
        """Open a connection to the mail host, with the timeout."""

        app = getattr(self, "app", None) or current_app
        try:
            return TimeoutConnection(app.extensions["mail"])
        except KeyError as err:
            raise RuntimeError(
                "The current application was not configured with Flask-Mail"
            ) from err
