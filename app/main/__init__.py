from flask import Blueprint

bp = Blueprint("main", __name__)

# Every route module registers on the one "main" blueprint (slice f of the
# routes.py split). Thus, the endpoint names and every url_for("main.X")
# stay exactly as they were when routes.py held all the routes.

from app.main import (  # noqa: E402
    account,
    admin,
    criterion_pages,
    discover,
    dvr,
    dvr_admin,
    file_pages,
    game,
    movie_pages,
    people_pages,
    posters,
    routes,
    search,
    shopping,
    tv_pages,
)
