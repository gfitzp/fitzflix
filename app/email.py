from threading import Thread

from flask import current_app
from flask_mail import Message

from app import mail


def send_async_email(app, msg):
    """Function to send email asynchronously using an application thread."""

    with app.app_context():
        app.logger.info(msg)
        mail.send(msg)


def send_email(subject, sender, recipients, text_body, html_body, attachments=None):
    """Email sending framework."""

    msg = Message(subject, sender=sender, recipients=recipients)
    msg.body = text_body
    msg.html = html_body
    if attachments:
        for attachment in attachments:
            msg.attach(*attachment)
    Thread(
        target=send_async_email, args=(current_app._get_current_object(), msg)
    ).start()


def task_send_email(subject, sender, recipients, text_body, html_body, attachments=None):
    """Email sending framework."""

    msg = Message(subject, sender=sender, recipients=recipients)
    msg.body = text_body
    msg.html = html_body
    if attachments:
        for attachment in attachments:
            msg.attach(*attachment)
    mail.send(msg)