import traceback
from functools import partial
from threading import Thread

from flask import current_app, has_request_context, render_template
from flask_mail import Message

from app import mail


def send_async_email(app, msg):
    """Send an email asynchronously in an application thread.

    An error in the thread goes to the app logger (#451). Without this,
    it went only to the standard error of the process."""

    with app.app_context():
        try:
            mail.send(msg)
        except Exception:
            app.logger.error(
                f"Could not send the email '{msg.subject}':\n"
                f"{traceback.format_exc()}"
            )


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
    """Send an email. In a request, do not wait for the mail server.

    Outside a request, the email goes synchronously (#451). The workers
    do not fork. Thus, the caller gets the error and logs it. A thread
    would hide the error from the task."""

    if not has_request_context():
        task_send_email(subject, sender, recipients, text_body, html_body, attachments)
        return
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


def admin_email_text(subject, text_body, html_body):
    """Return a callable that sends the given bodies to the first admin, or None.

    The subject gets the "Fitzflix - " prefix. Without an admin, there
    is nobody to tell: None comes back."""

    recipient = admin_address()
    if not recipient:
        return None
    return partial(
        send_email,
        f"Fitzflix - {subject}",
        sender=("Fitzflix", current_app.config["SERVER_EMAIL"]),
        recipients=[recipient],
        text_body=text_body,
        html_body=html_body,
    )


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
    return admin_email_text(
        subject,
        render_template(f"email/{template}.txt", **context),
        render_template(f"email/{template}.html", **context),
    )


def _send_or_log(build):
    """Build an admin email and send it. Log an error instead of raising it.

    A failed email must not stop the task that sends it. Without an
    admin, build returns None and nothing is sent."""

    try:
        send = build()
        if send is not None:
            send()
    except Exception:
        current_app.logger.error(traceback.format_exc())


def notify_admin(subject, template, **context):
    """Send an email from a template pair in templates/email to the first admin."""

    _send_or_log(lambda: admin_email(subject, template, **context))


def notify_admin_text(subject, text_body, html_body):
    """Send an email with the given bodies to the first admin."""

    _send_or_log(lambda: admin_email_text(subject, text_body, html_body))
