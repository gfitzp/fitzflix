import secrets

from urllib.parse import urlparse
from flask import render_template, flash, redirect, url_for, request, current_app
from flask_login import current_user, login_user, logout_user

from app import db
from app.auth import bp
from app.auth.email import send_password_reset_email
from app.auth.forms import (
    LoginForm,
    RegistrationForm,
    ResetPasswordForm,
    ResetPasswordRequestForm,
)
from app.models import User


@bp.route("/login", methods=["GET", "POST"])
def login():
    """Log a user in."""

    # If there is no user record, redirect to the registration page.

    if User.query.first() is None:
        return redirect(url_for("auth.register"))

    # If the user is already authenticated, redirect to the home page.

    if current_user.is_authenticated:
        return redirect(url_for("main.index"))

    # The form to log a user in.

    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data).first()
        if user is None or not user.check_password(form.password.data):
            flash("Invalid username or password")
            return redirect(url_for("auth.login"))

        login_user(user, remember=form.remember_me.data)
        next_page = request.args.get("next")
        if not next_page or urlparse(next_page).netloc != "":
            next_page = url_for("main.index")

        return redirect(next_page)

    return render_template(
        "auth/login.html",
        title="Sign In",
        form=form,
        prevent_creation=current_app.config["PREVENT_ACCOUNT_CREATION"],
    )


@bp.route("/logout")
def logout():
    """Log a user out."""

    logout_user()
    return redirect(url_for("main.index"))


@bp.route("/register", methods=["GET", "POST"])
def register():
    """Register a new user."""

    # If the user is already logged in, redirect to the home page.

    if current_user.is_authenticated:
        return redirect(url_for("main.index"))

    # Find out if an admin user exists.

    admin = User.query.filter_by(admin=True).first()

    # If the config does not permit new accounts, and an admin user
    # exists, do not show the registration page. Redirect to the login
    # page instead.

    if current_app.config["PREVENT_ACCOUNT_CREATION"] and admin:
        return redirect(url_for("auth.login"))

    # The form to register a user.

    form = RegistrationForm()
    if form.validate_on_submit():
        # If there is no admin user yet, set the admin flag to true.
        # Thus, the first account is the admin account.

        if not admin:
            user = User(email=form.email.data, admin=True)

        else:
            user = User(email=form.email.data)

        user.set_password(form.password.data)
        user.api_key = secrets.token_hex(16)
        db.session.add(user)
        db.session.commit()
        flash("You are now a registered user.")
        return redirect(url_for("auth.login"))

    return render_template("auth/register.html", title="Register", form=form)


@bp.route("/reset-password-request", methods=["GET", "POST"])
def reset_password_request():
    """Start the process to set a new password for a user."""

    if current_user.is_authenticated:
        return redirect(url_for("main.index"))

    # The form to request a new password.

    form = ResetPasswordRequestForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data).first()
        if user:
            send_password_reset_email(user)

        flash("Check your email for instructions to reset your password")
        return redirect(url_for("auth.login"))

    return render_template(
        "auth/reset_password_request.html", title="Reset Password", form=form
    )


@bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    """Let the user set a new password.

    An email with a time-limited token sends the user to this page.
    """

    if current_user.is_authenticated:
        return redirect(url_for("main.index"))

    user = User.verify_reset_password_token(token)
    if not user:
        return redirect(url_for("main.index"))

    form = ResetPasswordForm()
    if form.validate_on_submit():
        user.set_password(form.password.data)
        db.session.commit()
        flash("Fitzflix reset your password.")
        return redirect(url_for("auth.login"))

    return render_template("auth/reset_password.html", form=form)
