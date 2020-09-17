from app import create_app, db
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
