"""Register the Jinja filters and globals of the application.

create_app calls register_template_helpers right after it loads the
configuration.
"""

from urllib.parse import quote_plus


def register_template_helpers(app):
    """Add the template filters and the csrf_token global to the Jinja environment of app."""

    app.jinja_env.filters["quote_plus"] = lambda u: quote_plus(u)

    from app.richtext import review_html

    app.jinja_env.filters["review_html"] = review_html

    # The display name of a film, and its year. TMDB wins when the film
    # has a TMDB match. The year falls back to the local year when the
    # match has no release date. Thus, no template calls strftime on
    # None. Every template uses these two filters for the title line.

    def display_title(movie):
        """Return the TMDB title of a film, or its local title.

        The review page passes a plain namespace with no TMDB fields.
        Thus, the lookups tolerate a missing attribute.
        """

        return getattr(movie, "tmdb_title", None) or movie.title

    def display_year(movie):
        """Return the TMDB release year of a film, or its local year."""

        release_date = getattr(movie, "tmdb_release_date", None)
        if getattr(movie, "tmdb_title", None) and release_date:
            return release_date.strftime("%Y")
        return movie.year

    app.jinja_env.filters["display_title"] = display_title
    app.jinja_env.filters["display_year"] = display_year

    # The action forms of the poster tiles render their own csrf inputs.
    # They do not need a form object passed through every gallery route.
    # This is the same name that CSRFProtect would register, without its
    # enforcement.

    from flask_wtf.csrf import generate_csrf

    app.jinja_env.globals["csrf_token"] = generate_csrf
