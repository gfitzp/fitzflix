import os


from flask import (
    current_app,
    send_from_directory,
)

# flask.Markup was removed in Flask 2.4; import from its actual home

from app.main import bp


@bp.route("/apple-touch-icon-precomposed.png")
@bp.route("/apple-touch-icon.png")
def androidPng():
    """Serve the touch icon at the fixed paths Apple devices request."""

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
    # Served from the root (rather than /static/) so the service worker's
    # scope covers the whole application

    """Serve the PWA service worker from the site root, so its scope
    covers the whole application.
    """

    return send_from_directory(
        os.path.join(current_app.root_path, "static"),
        "sw.js",
        mimetype="application/javascript",
    )
