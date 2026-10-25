import traceback
from functools import partial
from threading import Thread

from flask import current_app, render_template
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


def admin_address():
    """Return the email address of the first admin, or None."""

    from app.models import User

    admin_user = User.query.filter(User.admin == True).first()
    return admin_user.email if admin_user else None


def admin_email(subject, template, **context):
    """Return a callable that sends an email to the first admin, or None.

    The templates are the pair templates/email/<template>.txt and .html.
    They render now, with the records as they are at this moment. The
    returned send can wait, for example until after a commit. The
    context gets user, the address of the admin, if the caller did not
    set it. Without an admin, there is nobody to tell: None comes back."""

    recipient = admin_address()
    if not recipient:
        return None
    context.setdefault("user", recipient)
    return partial(
        send_email,
        f"Fitzflix - {subject}",
        sender=("Fitzflix", current_app.config["SERVER_EMAIL"]),
        recipients=[recipient],
        text_body=render_template(f"email/{template}.txt", **context),
        html_body=render_template(f"email/{template}.html", **context),
    )


def notify_admin(subject, template, **context):
    """Send an email from a template pair in templates/email to the first admin.

    A failed email must not stop the import step that sends it. Thus,
    an error is logged and swallowed. Without an admin, nothing is
    sent."""

    try:
        send = admin_email(subject, template, **context)
        if send is not None:
            send()
    except Exception:
        current_app.logger.error(traceback.format_exc())
