from flask import current_app, render_template

from app.email import send_email


def send_password_reset_email(user):
    """Send a password reset email to the user."""

    token = user.get_reset_password_token()
    send_email(
        "Fitzflix - Reset your password",
        sender="no-reply@fitzflix.com",
        recipients=[user.email],
        text_body=render_template("email/reset_password.txt", user=user, token=token),
        html_body=render_template("email/reset_password.html", user=user, token=token),
    )
