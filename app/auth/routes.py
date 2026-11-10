import secrets

from sqlalchemy.exc import IntegrityError

from flask import (
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_wtf.csrf import validate_csrf
from wtforms.validators import ValidationError
from flask_login import current_user, login_required, login_user, logout_user

from app import db, local_path
from app.auth import bp
from app.auth.email import send_password_reset_email
from app.auth.passkeys import (
    PasskeyError,
    authentication_options,
    check_identity,
    finish_authentication,
    finish_registration,
    registration_options,
)
from app.auth.throttle import (
    allow_reset_request,
    login_lock_seconds,
    record_login_failure,
    record_login_success,
)
from app.auth.forms import (
    LoginForm,
    RegistrationForm,
    ResetPasswordForm,
    ResetPasswordRequestForm,
)
from app.models import User, sign_in_password_matches
from app.settings import get_setting


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

        # A locked account or address gets no password check. Thus, a
        # guess during the lock tells the attacker nothing (#263).

        locked = login_lock_seconds(form.email.data)
        if locked:
            minutes = -(-locked // 60)
            flash(
                f"Too many failed sign-in attempts. Try again in {minutes} "
                f"minute{'s' if minutes != 1 else ''}."
            )
            return (
                render_template(
                    "auth/login.html",
                    title="Sign In",
                    form=form,
                    prevent_creation=get_setting("PREVENT_ACCOUNT_CREATION"),
                ),
                429,
            )

        user = User.query.filter_by(email=form.email.data).first()
        if not sign_in_password_matches(user, form.password.data):
            record_login_failure(form.email.data)
            flash("Invalid username or password")

            # The next page stays for the second try (#422). Only a local
            # path goes into the redirect.

            next_page = request.args.get("next")
            if local_path(next_page):
                return redirect(url_for("auth.login", next=next_page))
            return redirect(url_for("auth.login"))

        record_login_success(form.email.data)
        if user.upgrade_password_hash(form.password.data):
            db.session.commit()
        login_user(user, remember=form.remember_me.data)
        next_page = request.args.get("next")
        if not local_path(next_page):
            next_page = url_for("main.index")

        return redirect(next_page)

    return render_template(
        "auth/login.html",
        title="Sign In",
        form=form,
        prevent_creation=get_setting("PREVENT_ACCOUNT_CREATION"),
    )


@bp.route("/logout", methods=["POST"])
def logout():
    """Log a user out.

    A logout is a POST with the csrf token (#401). A prefetch, a link
    preview, or an image tag makes a GET. That GET must not end the
    session. The route reads no FlaskForm. Thus, it checks the token by
    hand."""

    try:
        validate_csrf(request.form.get("csrf_token"))
    except ValidationError:
        flash("That page was out of date. Try again.", "warning")
        return redirect(url_for("main.index"))
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

    if get_setting("PREVENT_ACCOUNT_CREATION") and admin:
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
        try:
            db.session.commit()
        except IntegrityError:
            # A concurrent post with the same email passed the form check
            # first. The unique index refuses the second row (#507).
            db.session.rollback()
            flash("Email: Please use a different email address.", "danger")
            return render_template("auth/register.html", title="Register", form=form)
        flash("You are now a registered user.")
        return redirect(url_for("auth.login"))

    # The page does not show the field errors. Thus, a refused field (for
    # example, an email address that is too long, #329) gets a message.

    for field, messages in form.errors.items():
        if field != "csrf_token":
            flash(f"{getattr(form, field).label.text}: {messages[0]}", "danger")

    return render_template("auth/register.html", title="Register", form=form)


@bp.route("/reset-password-request", methods=["GET", "POST"])
def reset_password_request():
    """Start the process to set a new password for a user."""

    if current_user.is_authenticated:
        return redirect(url_for("main.index"))

    # The form to request a new password.

    form = ResetPasswordRequestForm()
    if form.validate_on_submit():
        # Over the limit, the page shows the same message but sends no
        # email. Thus, the form cannot flood an inbox (#263).

        if allow_reset_request(form.email.data):
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

    # A passkey-only account asks for this link from the Profile page
    # (#669). Thus, a signed-in user can follow a link for their own
    # account. A link for another account goes to the home page.

    user = User.verify_reset_password_token(token)
    if not user:
        return redirect(url_for("main.index"))
    if current_user.is_authenticated and current_user.id != user.id:
        return redirect(url_for("main.index"))

    form = ResetPasswordForm()
    if form.validate_on_submit():
        user.set_password(form.password.data)
        db.session.commit()
        flash("Fitzflix reset your password.")
        return redirect(url_for("auth.login"))

    return render_template("auth/reset_password.html", form=form)


# The passkey ceremonies (#669). Each route is a JSON post from
# passkeys.js. The post carries the csrf token of the page in the
# X-CSRFToken header. The routes read no FlaskForm. Thus, they check
# the token by hand, as the logout does.


def _json_body():
    """Return the JSON object of the post. A body that is no object counts as empty."""

    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def _csrf_header_valid():
    """Return True if the JSON post carries the csrf token of its page."""

    try:
        validate_csrf(request.headers.get("X-CSRFToken"))
    except ValidationError:
        return False
    return True


def _json_error(message, status):
    """Return a JSON error for passkeys.js to show next to its button."""

    return jsonify(error=message), status


def _json_options(options):
    """Return the options JSON of the webauthn library as a response."""

    return current_app.response_class(options, mimetype="application/json")


STALE_PAGE = "That page was out of date. Reload it and try again."


@bp.route("/passkey/register/options", methods=["POST"])
@login_required
def passkey_register_options():
    """Give the browser the options to make a passkey for the signed-in user.

    A new passkey is a new way into the account. Thus, the body must
    carry a proof of identity, as the Profile forms do (#675). It is
    the password, or a passkey confirmation that the Profile page asked
    for."""

    if not _csrf_header_valid():
        return _json_error(STALE_PAGE, 400)
    body = _json_body()
    try:
        check_identity(
            body.get("password"),
            body.get("passkey_assertion"),
            url_for("main.profile"),
        )
        return _json_options(registration_options(current_user))
    except PasskeyError as error:
        return _json_error(str(error), 400)


@bp.route("/passkey/register", methods=["POST"])
@login_required
def passkey_register():
    """Store the passkey that the browser made. Answer with the Profile address."""

    if not _csrf_header_valid():
        return _json_error(STALE_PAGE, 400)
    body = _json_body()
    try:
        passkey = finish_registration(
            current_user, body.get("credential") or {}, body.get("name")
        )
    except PasskeyError as error:
        return _json_error(str(error), 400)
    flash(f"Added the passkey '{passkey.display_name}'.", "success")
    return jsonify(redirect=url_for("main.profile"))


@bp.route("/passkey/login/options", methods=["POST"])
def passkey_login_options():
    """Give the browser the options for a sign-in with any passkey of this site."""

    if not _csrf_header_valid():
        return _json_error(STALE_PAGE, 400)
    try:
        return _json_options(authentication_options())
    except PasskeyError as error:
        return _json_error(str(error), 400)


@bp.route("/passkey/login", methods=["POST"])
def passkey_login():
    """Sign the owner of the passkey in. Answer with the address of the next page.

    The passkey names its account. Thus, the sign-in has no email
    address and no password, and the throttle of #263 does not apply."""

    if not _csrf_header_valid():
        return _json_error(STALE_PAGE, 400)
    body = _json_body()
    try:
        passkey = finish_authentication(body.get("credential") or {}, kind="login")
    except PasskeyError as error:
        return _json_error(str(error), 401)
    login_user(passkey.user, remember=bool(body.get("remember")))
    current_app.logger.info(
        f"Passkey sign-in: {passkey.user.email} with '{passkey.display_name}'"
    )
    next_page = body.get("next")
    if not local_path(next_page):
        next_page = url_for("main.index")
    return jsonify(redirect=next_page)


@bp.route("/passkey/confirm/options", methods=["POST"])
@login_required
def passkey_confirm_options():
    """Give the browser the options for a confirmation by the signed-in user.

    The body names the path that the form posts to. The answer is good
    only on that path (#676)."""

    if not _csrf_header_valid():
        return _json_error(STALE_PAGE, 400)
    body = _json_body()
    try:
        return _json_options(authentication_options(current_user, body.get("path")))
    except PasskeyError as error:
        return _json_error(str(error), 400)
