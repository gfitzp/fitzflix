from flask import current_app, render_template
from app import db
from app.errors import bp


@bp.app_errorhandler(404)
def not_found_error(error):
    """Render the 404 page."""

    return render_template("errors/404.html"), 404


@bp.app_errorhandler(500)
def internal_error(error):
    """Roll back the failed session and render the 500 page.

    The full page extends base.html, which reads the user (the database)
    and the queue count (Redis). Many 500s come from an outage of one of
    the two. Then the full page fails too (#346). Thus, a failure gives
    the plain page, which needs neither."""

    try:
        db.session.rollback()
    except Exception:
        current_app.logger.warning("The rollback after an error failed too")
    try:
        return render_template("errors/500.html"), 500
    except Exception:
        current_app.logger.warning(
            "The 500 page could not render. Fitzflix sent the plain page"
        )
        return render_template("errors/500_plain.html"), 500
