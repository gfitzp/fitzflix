import os


from flask import (
    current_app,
    send_from_directory,
)

# Flask 2.4 removed flask.Markup. Import it from its real module.

from app.main import bp


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
