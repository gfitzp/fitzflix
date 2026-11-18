from flask import Blueprint

bp = Blueprint("main", __name__)

# Every route module registers on the one "main" blueprint. Thus, the
# name of an endpoint is "main." and the name of the function, in each
# module.

from app.main import (  # noqa: E402
    account,
    admin,
    criterion_pages,
    discover,
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
