"""Register the Jinja filters and globals of the application.

create_app calls register_template_helpers right after it loads the
configuration.
"""


def file_size(size):
    """Return a byte count as the size text of the pages (#772).

    Invariant: 1 file has 1 size on every page, and "GB" has 1 meaning.
    The pages divide by 1024. Below 1024 MB, the text is MB with 1
    decimal. Then it is GB with 1 decimal. The GB step tests the ROUNDED
    MB figure, as the dropped filesize_megabytes column did. Thus, a file
    at 1023.95 MiB shows as "1.0 GB", not as "1024.0 MB" (#241). Below
    1024 rounded KB, the text is whole KB. Thus, a sample or an .nfo on
    Rejects does not show as "0.0 MB". A file that is not empty shows at
    least "1 KB". Thus, "0 KB" is an empty file. None gives no text."""

    if size is None:
        return ""
    kilobytes = round(size / 1024)
    if kilobytes < 1024:
        return f"{max(kilobytes, 1 if size else 0)} KB"
    megabytes = size / 1024**2
    if round(megabytes, 1) >= 1024:
        return f"{size / 1024**3:.1f} GB"
    return f"{megabytes:.1f} MB"


def register_template_helpers(app):
    """Add the template filters and the csrf_token global to the Jinja environment of app."""

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

        from app.models import display_year_of

        return display_year_of(
            movie.year,
            getattr(movie, "tmdb_title", None),
            getattr(movie, "tmdb_release_date", None),
        )

    app.jinja_env.filters["display_title"] = display_title
    app.jinja_env.filters["display_year"] = display_year

    # The bibliographic key of a language code (#443). "deu" and "ger"
    # give the same key. The import is lazy. Thus, the app factory does
    # not load the track layer.

    def language_key_filter(code):
        """Return the language key of a track language for a template."""

        from app.tracks import language_key

        return language_key(code)

    app.jinja_env.filters["language_key"] = language_key_filter

    # No template uses filesizeformat of Jinja. That filter divides by
    # 1000 (#772).

    app.jinja_env.filters["file_size"] = file_size

    # The action forms of the poster tiles render their own csrf inputs.
    # They do not need a form object passed through every gallery route.
    # This is the same name that CSRFProtect would register, without its
    # enforcement.

    from flask_wtf.csrf import generate_csrf

    app.jinja_env.globals["csrf_token"] = generate_csrf
