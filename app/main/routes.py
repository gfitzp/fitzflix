import os


from flask import (
    abort,
    current_app,
    request,
    send_from_directory,
)
from flask_login import current_user, login_required

# Flask 2.4 removed flask.Markup. Import it from its real module.

from app.main import bp

# The static subtrees that hold library material. The triage aids moved
# out of the static tree (#468). The gate also covers a copy left at the
# old place. The custom posters stay static, but only a signed-in user
# gets them. The gate answers 401, not a redirect. The service worker
# keeps each successful static response, and a followed redirect to the
# sign-in page is a successful response.

PRIVATE_STATIC_PREFIXES = ("/static/custom/", "/static/triage/")


@bp.before_app_request
def gate_private_static():
    """Refuse an anonymous request for a private static subtree."""

    if request.path.startswith(PRIVATE_STATIC_PREFIXES) and not (
        current_user.is_authenticated
    ):
        abort(401)


@bp.route("/triage/<int:file_id>/<path:name>")
@login_required
def triage_aid(file_id, name):
    """Serve 1 subtitle-triage snapshot or audio sample of a file (#468).

    The aids show frames and sound of library films. Thus, this route
    needs authentication, as the frames of the game do. A shared cache
    must not keep the response."""

    response = send_from_directory(
        os.path.join(current_app.config["TRIAGE_SNAPSHOT_DIR"], str(file_id)), name
    )
    response.cache_control.public = False
    response.cache_control.private = True
    return response


@bp.route("/apple-touch-icon-precomposed.png")
@bp.route("/apple-touch-icon.png")
def androidPng():
    """Serve the touch icon at the fixed paths that Apple devices request."""

    return send_from_directory(
        os.path.join(current_app.root_path, "static"),
        "apple-touch-icon.png",
        mimetype="image/png",
    )


@bp.route("/favicon.ico")
def favicon():
    """Serve the classic favicon at its fixed root path."""

    return send_from_directory(
        os.path.join(current_app.root_path, "static"),
        "favicon.ico",
        mimetype="image/vnd.microsoft.icon",
    )


@bp.route("/sw.js")
def service_worker():
    # Fitzflix serves this file from the root, not from /static/. Thus, the
    # scope of the service worker covers the whole application.

    """Serve the PWA service worker from the site root.

    Thus, the scope of the service worker covers the whole application.
    """

    return send_from_directory(
        os.path.join(current_app.root_path, "static"),
        "sw.js",
        mimetype="application/javascript",
    )
