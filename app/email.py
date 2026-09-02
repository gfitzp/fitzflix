from threading import Thread

from flask import current_app
from flask_mail import Message

from app import mail


def send_async_email(app, msg):
    """Send an email asynchronously in an application thread."""

    with app.app_context():
        mail.send(msg)


def build_message(subject, sender, recipients, text_body, html_body, attachments=None):
    """Assemble a mail message with optional attachments."""

    msg = Message(subject, sender=sender, recipients=recipients)
    msg.body = text_body
    msg.html = html_body
    if attachments:
        for attachment in attachments:
            msg.attach(*attachment)
    return msg


def send_email(subject, sender, recipients, text_body, html_body, attachments=None):
    """Send an email from a request. Do not wait for the mail server."""

    msg = build_message(subject, sender, recipients, text_body, html_body, attachments)
    Thread(
        target=send_async_email, args=(current_app._get_current_object(), msg)
    ).start()


def task_send_email(
    subject, sender, recipients, text_body, html_body, attachments=None
):
    """Send an email synchronously from a background task."""

    mail.send(
        build_message(subject, sender, recipients, text_body, html_body, attachments)
    )
