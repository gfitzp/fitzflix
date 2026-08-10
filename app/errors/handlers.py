from flask import render_template
from app import db
from app.errors import bp


@bp.app_errorhandler(404)
def not_found_error(error):
    """Render the 404 page."""

    return render_template("errors/404.html"), 404


@bp.app_errorhandler(500)
def internal_error(error):
    """Roll back the failed session and render the 500 page."""

    db.session.rollback()
    return render_template("errors/500.html"), 500
