from flask import Blueprint

bp = Blueprint("main", __name__)

# Every route module registers on the one "main" blueprint (#17's
# slice f), so endpoint names — and every url_for("main.X") — are
# exactly what they were when routes.py held them all

from app.main import (  # noqa: E402
    account,
    admin,
    discover,
    game,
    library,
    posters,
    routes,
    search,
    shopping,
)
