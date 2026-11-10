import os


from flask import (
    abort,
    current_app,
    request,
    send_from_directory,
)
from flask_login import current_user, login_required
from werkzeug.security import safe_join

# Flask 2.4 removed flask.Markup. Import it from its real module.

from app.main import bp

# The static subtrees that hold library material. The triage aids moved
# out of the static tree (#468). The custom posters stay static, but only
# a signed-in user gets them. The gate answers 401, not a redirect. A
# redirect to the sign-in page would reach the browser cache as the
# image.

PRIVATE_STATIC_DIRECTORIES = ("custom",)


def _in_private_static(filename):
    """Return True if a static file name resolves into a private subtree.

    The test is on the file system, not on the text of the path. The
    library disk ignores letter case, so "Custom" is "custom" there, and
    a dot segment resolves into the tree too. safe_join gives the path
    that the static route sends. The function walks up from that file
    to the root. A directory on that walk that is a private directory
    makes the file private. A private directory can be a link to a
    different volume.

    A name that the static route refuses, or that the file system
    cannot read (a NUL byte), is not private. The static route answers
    it with 404."""

    static = current_app.static_folder
    path = safe_join(static, filename)
    if path is None:
        return False
    private = [os.path.join(static, name) for name in PRIVATE_STATIC_DIRECTORIES]
    try:
        path = os.path.realpath(path)
    except ValueError:
        return False
    while True:
        for directory in private:
            try:
                if os.path.samefile(path, directory):
                    return True
            except (OSError, ValueError):
                pass  # The path or the directory is not on disk.
        parent = os.path.dirname(path)
        if parent == path:
            return False
        path = parent


@bp.before_app_request
def gate_private_static():
    """Refuse an anonymous request for a file of a private static subtree.

    The path test comes first. Thus, a public static file costs no
    lookup of the user."""

    if (
        request.endpoint == "static"
        and _in_private_static((request.view_args or {}).get("filename", ""))
        and not current_user.is_authenticated
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
