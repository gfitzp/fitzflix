"""Set up the log of the application: the redaction filter and the production handlers.

create_app calls configure_logging after it registers the blueprints.
The redaction filter is in app.redaction.
"""

import logging
import os

from logging.handlers import SMTPHandler, WatchedFileHandler


class ErrorMailHandler(SMTPHandler):
    """The SMTP handler of the ERROR mails, with the TLS rule of Flask-Mail (#531).

    The stdlib handler starts TLS only when it has a username. With
    MAIL_USE_TLS and no MAIL_USERNAME, each ERROR mail went in clear, or
    a relay that requires STARTTLS refused it. This handler starts TLS
    whenever secure is set, logs in only with a username, and uses
    SMTP_SSL for MAIL_USE_SSL."""

    def __init__(self, *args, use_ssl=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.use_ssl = use_ssl

    def emit(self, record):
        """Send 1 log record as an email."""

        try:
            import email.utils
            import smtplib
            from email.message import EmailMessage

            smtp_class = smtplib.SMTP_SSL if self.use_ssl else smtplib.SMTP
            port = self.mailport or (
                smtplib.SMTP_SSL_PORT if self.use_ssl else smtplib.SMTP_PORT
            )
            smtp = smtp_class(self.mailhost, port, timeout=self.timeout)
            message = EmailMessage()
            message["From"] = self.fromaddr
            message["To"] = ",".join(self.toaddrs)
            message["Subject"] = self.getSubject(record)
            message["Date"] = email.utils.localtime()
            message.set_content(self.format(record))
            if self.secure is not None and not self.use_ssl:
                smtp.ehlo()
                smtp.starttls(*self.secure)
                smtp.ehlo()
            if self.username:
                smtp.login(self.username, self.password)
            smtp.send_message(message)
            smtp.quit()
        except Exception:
            self.handleError(record)


def configure_logging(app):
    """Install the redaction filter and, outside debug mode, the mail and file handlers."""

    # Credentials never reach the log, with any handler that writes it.
    # See app.redaction. Fitzflix installs this in every mode. Thus, the
    # tests cover it too.

    from app.redaction import install as install_redaction

    install_redaction(app.logger, app.config)

    if not app.debug:
        # Configure the log handling for production mode.

        # Both Flask instances in this package share the "app" logger.
        # Thus, attach the mail and file handlers only if an earlier
        # create_app() did not.

        if app.config["MAIL_SERVER"] and not any(
            isinstance(handler, SMTPHandler) for handler in app.logger.handlers
        ):
            # Send an email for each exception.

            auth = None
            if app.config["MAIL_USERNAME"] or app.config["MAIL_PASSWORD"]:
                auth = (app.config["MAIL_USERNAME"], app.config["MAIL_PASSWORD"])

            secure = None
            if app.config["MAIL_USE_TLS"]:
                secure = ()

            mail_handler = ErrorMailHandler(
                mailhost=(app.config["MAIL_SERVER"], app.config["MAIL_PORT"]),
                fromaddr=app.config["SERVER_EMAIL"],
                toaddrs=app.config["ADMIN_EMAIL"],
                subject="Fitzflix Failure",
                credentials=auth,
                secure=secure,
                use_ssl=bool(app.config.get("MAIL_USE_SSL")),
            )
            mail_handler.setLevel(logging.ERROR)
            app.logger.addHandler(mail_handler)

        os.makedirs(os.path.dirname(app.config["LOG_FILE"]), exist_ok=True)

        if not any(
            isinstance(handler, logging.FileHandler) for handler in app.logger.handlers
        ):
            # WatchedFileHandler sees when the rotate_logs maintenance
            # task renames the log file. It opens the file again before
            # the next write.

            file_handler = WatchedFileHandler(app.config["LOG_FILE"])
            file_handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]"
                )
            )
            file_handler.setLevel(logging.INFO)
            app.logger.addHandler(file_handler)

        app.logger.setLevel(logging.INFO)
        app.logger.info("Fitzflix startup")
