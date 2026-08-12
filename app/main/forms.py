from datetime import datetime

from flask_wtf import FlaskForm
from flask_wtf.file import FileField, FileRequired
from wtforms import (
    BooleanField,
    DateField,
    DecimalField,
    HiddenField,
    IntegerField,
    PasswordField,
    RadioField,
    SelectField,
    SelectMultipleField,
    StringField,
    SubmitField,
    TextAreaField,
    widgets,
)
from wtforms.validators import (
    DataRequired,
    Email,
    EqualTo,
    Optional,
    ValidationError,
)
from wtforms.widgets import HiddenInput
from app.models import User


class EditProfileForm(FlaskForm):
    """Change the account email, on the Profile page."""

    email = StringField("New Email Address", validators=[DataRequired(), Email()])
    email2 = StringField(
        "Confirm Email Address", validators=[DataRequired(), Email(), EqualTo("email")]
    )
    submit = SubmitField("Update")

    def __init__(self, original_email, *args, **kwargs):
        super(EditProfileForm, self).__init__(*args, **kwargs)
        self.original_email = original_email

    def validate_email(self, email):
        """Reject an address another account already uses."""

        if email.data != self.original_email:
            user = User.query.filter_by(email=self.email.data).first()
            if user is not None:
                raise ValidationError("Please use a different email address.")


class UpdateAPIKeyForm(FlaskForm):
    """Regenerate the API key, on the Profile page."""

    regenerate_key_submit = SubmitField("Regenerate API Key")


class PlexUsernameForm(FlaskForm):
    """Map this Fitzflix account to a Plex account for watch attribution."""

    plex_username = StringField("Plex Username", validators=[Optional()])
    plex_submit = SubmitField("Update Plex Mapping")


class ImportForm(FlaskForm):
    """Manually trigger an import-directory scan."""

    submit = SubmitField("Scan Import Directory")


class MovieReviewForm(FlaskForm):
    """Log or edit a viewing: rating, liked, review text, watch date."""

    rating = DecimalField("Rating (out of 5)", places=1, validators=[Optional()])
    liked = BooleanField("Liked")
    review = TextAreaField("Review")
    date_watched = DateField("Date Watched", format="%Y-%m-%d", validators=[Optional()])
    review_submit = SubmitField("Log Movie")

    def validate_rating(self, rating):
        """Keep ratings between 0 and 5 stars."""

        if rating.data is not None and (rating.data < 0 or rating.data > 5):
            raise ValidationError("Please enter a rating between 0 and 5 stars.")

    def validate_date_watched(self, date_watched):
        """Reject watch dates in the future."""

        if datetime.strptime(str(date_watched.data), "%Y-%m-%d") > datetime.now():
            raise ValidationError("Enter a date in the past.")


class TMDBLookupForm(FlaskForm):
    """Movie page: refresh or re-point a title's TMDb data."""

    tmdb_id = IntegerField("TMDB ID", validators=[Optional()])
    lookup_submit = SubmitField("Refresh TMDB Data")


class TMDBRefreshForm(FlaskForm):
    """Maintenance page: refresh TMDb data for the whole library."""

    tmdb_refresh = SubmitField("Refresh TMDb Info")


class CriterionForm(FlaskForm):
    """Movie page: edit a film's Criterion Collection details."""

    spine_number = IntegerField("Spine #", validators=[Optional()])
    set_title = StringField("Collector's Set Title", validators=[Optional()])
    in_print = BooleanField("In Print", validators=[Optional()])
    quality = SelectField("Released as")
    owned = BooleanField("Owned", validators=[Optional()])
    criterion_submit = SubmitField("Update Criterion Info")

    def validate_spine_number(self, spine_number):
        """Spine numbers are positive."""

        if spine_number.data < 1:
            raise ValidationError("Enter a positive spine number.")


class TranscodeForm(FlaskForm):
    """Queue transcodes, from the movie and file pages."""

    transcode_submit = SubmitField("Create Transcoded File")
    transcode_all = SubmitField("Transcode All")


class LibrarySearchForm(FlaskForm):
    """The library listings' search box."""

    search_query = StringField("Search...", validators=[Optional()])
    search_submit = SubmitField("Search")


class CriterionRefreshForm(FlaskForm):
    """Maintenance page: refresh Criterion data from Wikidata."""

    criterion_refresh = SubmitField("Refresh Criterion Collection Info")


class CriterionFilterForm(FlaskForm):
    """Criterion page: all releases versus owned discs."""

    filter_status = RadioField(
        "Library",
        choices=[
            ("all", "All films with a Criterion release"),
            ("owned", "Owned Criterion releases"),
        ],
    )
    filter_submit = SubmitField("Filter")


class MovieShoppingFilterForm(FlaskForm):
    """Movie shopping list: library, media, and quality-range filters."""

    filter_status = RadioField(
        "Library",
        choices=[
            ("all", "All films"),
            ("criterion", "Films with a Criterion release"),
        ],
    )
    media = RadioField(
        "Media Format",
        choices=[
            ("all", "All media formats"),
            ("digital", "Digital downloads only"),
        ],
    )
    min_quality = SelectField("Minimum quality")
    max_quality = SelectField("Maximum quality")
    filter_submit = SubmitField("Filter")


class QualityFilterForm(FlaskForm):
    """Filter a listing to one quality tier."""

    quality = SelectField("Quality")
    filter_submit = SubmitField("Filter")


class TVShoppingFilterForm(FlaskForm):
    """TV shopping list: maximum-quality filter."""

    quality = SelectField("Maximum quality")
    filter_submit = SubmitField("Filter")


class StreamingProvidersForm(FlaskForm):
    """Profile page: which streaming services this user subscribes to.

    Availability displays (movie pages, TMDb search) are customized to
    these picks per user — explicitly not a site-wide setting.
    """

    providers = SelectMultipleField(
        "Streaming services",
        coerce=int,
        option_widget=widgets.CheckboxInput(),
        widget=widgets.ListWidget(prefix_label=False),
    )
    providers_submit = SubmitField("Save Streaming Services")


class WatchlistForm(FlaskForm):
    """Watchlist toggles on film pages, and per-row removal on the
    watchlist page itself (movie_id rides in the hidden field there)."""

    movie_id = IntegerField(widget=HiddenInput(), validators=[Optional()], default=None)
    add_watchlist_submit = SubmitField("Add to Watchlist")
    remove_watchlist_submit = SubmitField("Remove from Watchlist")


class RateFilmForm(FlaskForm):
    """The rating drive's response card: rate the film, want it,
    haven't seen it, or skip it for now."""

    movie_id = IntegerField(widget=HiddenInput(), validators=[Optional()], default=None)
    rating = DecimalField("Your rating (out of 5)", places=1, validators=[Optional()])
    liked = BooleanField("Liked")
    rate_submit = SubmitField("Rate It")
    watchlist_submit = SubmitField("Add to Watchlist")
    unseen_submit = SubmitField("Haven't Seen It")
    skip_submit = SubmitField("Skip")
    # Adds a SUGGESTED film to the watchlist without touching the
    # drive's steering, unlike watchlist_submit on the featured card
    want_suggestion_submit = SubmitField("Add to Watchlist")

    def validate_rating(self, rating):
        """Keep ratings between 0 and 5 stars."""

        if rating.data is not None and not 0 <= rating.data <= 5:
            raise ValidationError("Ratings run from 0 to 5 stars.")


class ReviewExportForm(FlaskForm):
    """History page: email the Letterboxd-format review export. The default
    export covers only entries added or edited since the last export; the
    checkbox requests everything.
    """

    full_export = BooleanField("Full export")
    export_submit = SubmitField("Export Reviews")


class ReviewUploadForm(FlaskForm):
    """History page: import a Letterboxd zip or legacy ratings file."""

    file = FileField("Reviews File")
    upload_submit = SubmitField("Import Reviews")


class S3DownloadForm(FlaskForm):
    """File page: password-confirmed restore from AWS."""

    password = PasswordField("Password:", validators=[DataRequired()])
    s3_download_submit = SubmitField("Restore from AWS")


class SeasonRestoreForm(FlaskForm):
    """Season page: password-confirmed bulk restore from AWS."""

    password = PasswordField("Password:", validators=[DataRequired()])
    season_restore_submit = SubmitField("Bulk restore season from AWS")


class SeriesRestoreForm(FlaskForm):
    """Series page: password-confirmed bulk restore from AWS."""

    password = PasswordField("Password:", validators=[DataRequired()])
    series_restore_submit = SubmitField("Bulk restore series from AWS")


class S3UploadForm(FlaskForm):
    """File page: upload the original to AWS."""

    s3_upload_submit = SubmitField("Upload to AWS")


class MultiCheckboxField(SelectMultipleField):
    """A SelectMultipleField rendered as a list of checkboxes."""

    widget = widgets.ListWidget(prefix_label=False)
    option_widget = widgets.CheckboxInput()


class MKVPropEditForm(FlaskForm):
    """File page: set default audio/subtitle tracks and forced flags."""

    default_audio = RadioField("Default audio track", validators=[Optional()])
    default_subtitle = RadioField("Default subtitle track", validators=[Optional()])
    forced_subtitles = MultiCheckboxField(
        "Forced subtitle tracks", validators=[Optional()]
    )
    mkvpropedit_submit = SubmitField("Update MKV Properties")


class MKVMergeForm(FlaskForm):
    """File page: strip unwanted tracks via a remux."""

    audio_tracks = MultiCheckboxField("Audio tracks", validators=[Optional()])
    subtitle_tracks = MultiCheckboxField("Subtitle tracks", validators=[Optional()])
    mkvmerge_submit = SubmitField("Remux MKV File")


class SyncAWSStorageForm(FlaskForm):
    """Maintenance page: password-confirmed S3 sync and prune."""

    password = PasswordField("Password:", validators=[DataRequired()])
    sync_submit = SubmitField("Sync AWS S3 Storage")


class FileDeleteForm(FlaskForm):
    """File page: delete a file and purge its records."""

    delete_submit = SubmitField("Delete and Purge File")


class SeriesDeleteForm(FlaskForm):
    """Series page: delete the series."""

    delete_submit = SubmitField("Delete Series")


class TrackMetadataScanForm(FlaskForm):
    """Rescan track metadata, per file or library-wide."""

    scan_submit = SubmitField("Rescan Track Metadata")


class MovieShoppingExcludeForm(FlaskForm):
    """Add a title to the shopping list or remove it."""

    movie_id = IntegerField("Movie ID", validators=[Optional()], widget=HiddenInput())
    add_submit = SubmitField("Add to List")
    exclude_submit = SubmitField("Remove from List")


class CustomPosterUploadForm(FlaskForm):
    """Poster picker: upload a custom poster image."""

    custom_poster = FileField("Poster Image File", validators=[FileRequired()])
    poster_submit = SubmitField("Upload")


class TMDBPosterSelectForm(FlaskForm):
    """One "use this poster" button per image on the poster picker page."""

    poster_path = HiddenField(validators=[DataRequired()])
    poster_select_submit = SubmitField("Use this poster")


class CustomPosterRemoveForm(FlaskForm):
    """Poster picker: delete the custom poster."""

    poster_remove_submit = SubmitField("Remove custom poster")


class FailedJobForm(FlaskForm):
    """System page: requeue or forget a failed background job."""

    failed_job_id = HiddenField(validators=[DataRequired()])
    failed_queue = HiddenField(validators=[DataRequired()])
    requeue_submit = SubmitField("Requeue")
    forget_submit = SubmitField("Forget")


class RejectActionForm(FlaskForm):
    """Rejects page: send a file back for import, or delete it."""

    file_path = HiddenField(validators=[DataRequired()])
    reimport_submit = SubmitField("Re-import")
    delete_submit = SubmitField("Delete")


class MovieMergeForm(FlaskForm):
    """Maintenance page: merge duplicate movies sharing a TMDb id."""

    merge_tmdb_id = HiddenField(validators=[DataRequired()])
    merge_submit = SubmitField("Merge")


class SubtitleTriageForm(FlaskForm):
    """Per-file actions on the possibly-forced subtitles triage page.

    Track selection travels as plain track_ids checkboxes (a file can
    hide more than one forced track); the form carries the file and
    the two actions.
    """

    file_id = IntegerField(validators=[Optional()], widget=HiddenInput())
    mark_forced_submit = SubmitField("Flag selected as forced")
    dismiss_submit = SubmitField("Nothing forced here")


class FilenameTestForm(FlaskForm):
    """Maintenance page: preview how a filename would import."""

    test_filename = StringField("Filename", validators=[DataRequired()])
    filename_test_submit = SubmitField("Preview import")
