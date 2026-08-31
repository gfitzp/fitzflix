from flask import Blueprint

bp = Blueprint("main", __name__)

# Every route module registers on the one "main" blueprint (the routes.py split's
# slice f), so endpoint names — and every url_for("main.X") — are
# exactly what they were when routes.py held them all

from app.main import (  # noqa: E402
    account,
    admin,
    discover,
    dvr,
    dvr_admin,
    game,
    library,
    posters,
    routes,
    search,
    shopping,
)
