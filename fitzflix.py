from app import create_app, db, cli
from app.models import (
    File,
    FileAudioTrack,
    FileSubtitleTrack,
    Movie,
    MovieCast,
    MovieCrew,
    RefFeatureType,
    RefQuality,
    RefTMDBCertification,
    TMDBCredit,
    TMDBGenre,
    TMDBKeyword,
    TMDBMovieCollection,
    TMDBNetwork,
    TMDBProductionCompany,
    TMDBProductionCountry,
    TMDBSeason,
    TMDBSpokenLanguage,
    TVCast,
    TVCrew,
    TVSeries,
    User,
    UserMovieReview,
)

app = create_app()
cli.register(app)

# Build blueprints

from app.errors import bp as errors_bp
app.register_blueprint(errors_bp)

from app.auth import bp as auth_bp
app.register_blueprint(auth_bp, url_prefix="/auth")

from app.main import bp as main_bp
app.register_blueprint(main_bp)

from app.api import bp as api_bp
app.register_blueprint(api_bp, url_prefix="/api")

@app.shell_context_processor
def make_shell_context():
    """Create the flask shell context."""

    # Return these models so when `flask shell` is called these tables are available

    return {
        "db": db,
        "File": File,
        "FileAudioTrack": FileAudioTrack,
        "FileSubtitleTrack": FileSubtitleTrack,
        "Movie": Movie,
        "MovieCast": MovieCast,
        "MovieCrew": MovieCrew,
        "RefFeatureType": RefFeatureType,
        "RefQuality": RefQuality,
        "RefTMDBCertification": RefTMDBCertification,
        "TMDBCredit": TMDBCredit,
        "TMDBGenre": TMDBGenre,
        "TMDBKeyword": TMDBKeyword,
        "TMDBMovieCollection": TMDBMovieCollection,
        "TMDBNetwork": TMDBNetwork,
        "TMDBProductionCompany": TMDBProductionCompany,
        "TMDBProductionCountry": TMDBProductionCountry,
        "TMDBSeason": TMDBSeason,
        "TMDBSpokenLanguage": TMDBSpokenLanguage,
        "TVCast": TVCast,
        "TVCrew": TVCrew,
        "TVSeries": TVSeries,
        "User": User,
        "UserMovieReview": UserMovieReview,
    }
