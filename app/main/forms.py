from datetime import datetime

from flask_wtf import FlaskForm
from flask_wtf.file import FileField, FileRequired
from wtforms import (
    BooleanField,
    DateField,
    DecimalField,
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
    InputRequired,
    Optional,
    Regexp,
    ValidationError,
)
from wtforms.widgets import HiddenInput
from app.models import User


class EditProfileForm(FlaskForm):
    email = StringField("New Email Address", validators=[DataRequired(), Email()])
    email2 = StringField(
        "Confirm Email Address", validators=[DataRequired(), Email(), EqualTo("email")]
    )
    submit = SubmitField("Update")

    def __init__(self, original_email, *args, **kwargs):
        super(EditProfileForm, self).__init__(*args, **kwargs)
        self.original_email = original_email

    def validate_email(self, email):
        if email.data != self.original_email:
            user = User.query.filter_by(email=self.email.data).first()
            if user is not None:
                raise ValidationError("Please use a different email address.")


class UpdateAPIKeyForm(FlaskForm):
    regenerate_key_submit = SubmitField("Regenerate API Key")


class ImportForm(FlaskForm):
    submit = SubmitField("Scan Import Directory")


class MovieReviewForm(FlaskForm):
    rating = DecimalField("Rating (out of 5)", places=1, validators=[InputRequired()])
    review = TextAreaField("Review")
    date_watched = DateField("Date Watched", format="%Y-%m-%d", validators=[Optional()])
    review_submit = SubmitField("Rate Movie")

    def validate_rating(self, rating):
        if rating.data < 0 or rating.data > 5:
            raise ValidationError("Please enter a rating between 0 and 5 stars.")

    def validate_date_watched(self, date_watched):
        if datetime.strptime(str(date_watched.data), "%Y-%m-%d") > datetime.now():
            raise ValidationError("Enter a date in the past.")


class TMDBLookupForm(FlaskForm):
    tmdb_id = IntegerField("TMDB ID", validators=[Optional()])
    lookup_submit = SubmitField("Refresh TMDB Data")


class TMDBRefreshForm(FlaskForm):
    tmdb_refresh = SubmitField("Refresh TMDb Info")


class CriterionForm(FlaskForm):
    spine_number = IntegerField("Spine #", validators=[Optional()])
    set_title = StringField("Collector's Set Title", validators=[Optional()])
    in_print = BooleanField("In Print", validators=[Optional()])
    bluray_release = BooleanField("Blu-ray Release", validators=[Optional()])
    quality = SelectField("Released as")
    owned = BooleanField("Owned", validators=[Optional()])
    criterion_submit = SubmitField("Update Criterion Info")

    def validate_spine_number(self, spine_number):
        if spine_number.data < 1:
            raise ValidationError("Enter a positive spine number.")


class TranscodeForm(FlaskForm):
    transcode_submit = SubmitField("Create Transcoded File")
    transcode_all = SubmitField("Transcode All")


class LibrarySearchForm(FlaskForm):
    search_query = StringField("Search...", validators=[Optional()])
    search_submit = SubmitField("Search")


class CriterionRefreshForm(FlaskForm):
    criterion_refresh = SubmitField("Refresh Criterion Collection Info")


class CriterionFilterForm(FlaskForm):
    filter_status = RadioField(
        "Library",
        choices=[
            ("all", "All films with a Criterion release"),
            ("owned", "Owned Criterion releases"),
        ],
    )
    filter_submit = SubmitField("Filter")


class MovieShoppingFilterForm(FlaskForm):
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
    quality = SelectField("Quality")
    filter_submit = SubmitField("Filter")


class TVShoppingFilterForm(FlaskForm):
    quality = SelectField("Maximum quality")
    filter_submit = SubmitField("Filter")


class ReviewExportForm(FlaskForm):
    export_submit = SubmitField("Export Reviews")


class ReviewUploadForm(FlaskForm):
    file = FileField("Reviews File")
    upload_submit = SubmitField("Upload Reviews")


class S3DownloadForm(FlaskForm):
    s3_download_submit = SubmitField("Download from AWS")


class S3UploadForm(FlaskForm):
    s3_upload_submit = SubmitField("Upload to AWS")


class MultiCheckboxField(SelectMultipleField):
    widget = widgets.ListWidget(prefix_label=False)
    option_widget = widgets.CheckboxInput()


class MKVPropEditForm(FlaskForm):
    default_audio = RadioField("Default audio track", validators=[Optional()])
    default_subtitle = RadioField("Default subtitle track", validators=[Optional()])
    forced_subtitles = MultiCheckboxField(
        "Forced subtitle tracks", validators=[Optional()]
    )
    mkvpropedit_submit = SubmitField("Update MKV Properties")


class MKVMergeForm(FlaskForm):
    audio_tracks = MultiCheckboxField("Audio tracks", validators=[Optional()])
    subtitle_tracks = MultiCheckboxField("Subtitle tracks", validators=[Optional()])
    mkvmerge_submit = SubmitField("Remux MKV File")


class SyncAWSStorageForm(FlaskForm):
    password = PasswordField("Password:", validators=[DataRequired()])
    sync_submit = SubmitField("Sync AWS S3 Storage")


class FileDeleteForm(FlaskForm):
    delete_submit = SubmitField("Delete and Purge File")


class SeriesDeleteForm(FlaskForm):
    delete_submit = SubmitField("Delete Series")


class TrackMetadataScanForm(FlaskForm):
    scan_submit = SubmitField("Rescan Track Metadata")


class MovieShoppingExcludeForm(FlaskForm):
    movie_id = IntegerField("Movie ID", validators=[Optional()], widget=HiddenInput())
    add_submit = SubmitField("Add to List")
    exclude_submit = SubmitField("Remove from List")


class CustomPosterUploadForm(FlaskForm):
    custom_poster = FileField("Poster Image File", validators=[FileRequired()])
    poster_submit = SubmitField("Upload")
