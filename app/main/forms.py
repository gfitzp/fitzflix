from datetime import datetime

from flask_wtf import FlaskForm
from flask_wtf.file import FileField, FileRequired
from wtforms import (
    BooleanField,
    DateField,
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


class ResetFrameScoresForm(FlaskForm):
    """Profile: wipe the user's Name That Frame standings — streaks,
    bests, points, and win rates on every difficulty."""

    reset_frames_submit = SubmitField("Reset game scores")


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


class LetterboxdUsernameForm(FlaskForm):
    """Name the Letterboxd account whose RSS feed syncs into this
    user's diary; blank disables the sync."""

    letterboxd_username = StringField("Letterboxd Username", validators=[Optional()])
    letterboxd_submit = SubmitField("Update Letterboxd Sync")


class PlexPlayerForm(FlaskForm):
    """This user's playback device: the address of the Plex player
    their play buttons target. The profile route probes the address
    and reads the machine id off the player itself; blank removes the
    device (and the play buttons with it)."""

    plex_player_address = StringField(
        "Playback Device Address", validators=[Optional()]
    )
    plex_player_submit = SubmitField("Update Playback Device")


class InfusePlayerForm(FlaskForm):
    """This user's Infuse target: the Apple TV's Companion address.
    Submitting starts the one-time PIN pairing (the TV shows a PIN,
    entered via InfusePinForm); blank removes the device."""

    infuse_player_address = StringField("Apple TV Address", validators=[Optional()])
    infuse_player_submit = SubmitField("Pair Apple TV for Infuse")


class InfusePinForm(FlaskForm):
    """The PIN the Apple TV is showing for an in-flight pairing."""

    infuse_pin = StringField("PIN Shown on the Apple TV", validators=[DataRequired()])
    infuse_pin_submit = SubmitField("Finish Pairing")


class DefaultPlayerForm(FlaskForm):
    """Which app the plain play buttons target when this user has both
    Plex and Infuse playback configured.

    The submit label must not put two SQL keywords next to each other:
    the CloudFront WAF's SQLi_BODY rule blocked "Set Default Player"
    live with a bare 403 (2026-08-26), and tested against the same WAF
    "Update Default Player" blocks too (UPDATE and DEFAULT are both
    keywords) while "Update Playback Device" and "Save Default Player"
    pass — one keyword is fine, an adjacent pair reads as SQL.
    """

    default_player = RadioField(
        "Default Player",
        choices=[("plex", "Plex"), ("infuse", "Infuse")],
        validators=[DataRequired()],
    )
    default_player_submit = SubmitField("Save Default Player")


class ImportForm(FlaskForm):
    """Manually trigger an import-directory scan."""

    submit = SubmitField("Scan Import Directory")


class MovieReviewForm(FlaskForm):
    """Log or edit a viewing. Ratings come from the quick-answer
    ladder riding the form (3+ stars auto-flag liked); the date is
    optional — blank means seen sometime, unknown when — and the
    submit logs a bare unrated watch."""

    review = TextAreaField("Review")
    date_watched = DateField(
        "Date watched (optional)", format="%Y-%m-%d", validators=[Optional()]
    )
    review_submit = SubmitField("Log Watch")

    def validate_date_watched(self, date_watched):
        """Reject watch dates in the future."""

        if datetime.strptime(str(date_watched.data), "%Y-%m-%d") > datetime.now():
            raise ValidationError("Enter a date in the past.")


class TMDBLookupForm(FlaskForm):
    """Movie page: refresh or re-point a title's TMDB data.

    A blank id means "search TMDB by title" — useful for a record that
    has never been matched, destructive for one that has, so the routes
    refuse a blank submit when an id is already stored (#207).
    """

    tmdb_id = IntegerField("TMDB ID", validators=[Optional()])
    lookup_submit = SubmitField("Refresh TMDB Data")


class TMDBRemoveForm(FlaskForm):
    """Movie and TV pages: detach a record from TMDB for good.

    For titles TMDB has no entry for — home movies, and ids TMDB has
    deleted out from under a record.
    """

    remove_submit = SubmitField("Remove TMDB ID")


class RuntimeMismatchForm(FlaskForm):
    """The runtime triage page (#234): accept one flagged file's length
    as known-benign so it stops reappearing."""

    file_id = IntegerField(widget=HiddenInput(), validators=[Optional()], default=None)
    acknowledge_submit = SubmitField("Acknowledge")


class TMDBTriageForm(FlaskForm):
    """The TMDB triage page (#226): per unmatched record, either flag
    it as unmatchable — the Remove button's path, so no refresh ever
    guesses an id from the title — or match it to an id entered by
    hand. One hidden id rides per row, movie or series."""

    movie_id = IntegerField(widget=HiddenInput(), validators=[Optional()], default=None)
    series_id = IntegerField(
        widget=HiddenInput(), validators=[Optional()], default=None
    )
    tmdb_id = IntegerField("TMDB ID", validators=[Optional()])
    flag_submit = SubmitField("Flag as unmatchable")
    lookup_submit = SubmitField("Match")


class TMDBRefreshForm(FlaskForm):
    """Maintenance page: refresh TMDB data for the whole library."""

    tmdb_refresh = SubmitField("Refresh TMDB Info")


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


class MovieShoppingFilterForm(FlaskForm):
    """Movie shopping list: library, media, and quality-range filters."""

    filter_status = RadioField(
        "Library",
        choices=[
            ("all", "All films"),
            ("criterion", "Films with a Criterion release"),
            ("watchlist", "Watchlisted films not in the library"),
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

    Availability displays (movie pages, TMDB search) are customized to
    these picks per user — explicitly not a site-wide setting.
    """

    providers = SelectMultipleField(
        "Streaming services",
        coerce=int,
        option_widget=widgets.CheckboxInput(),
        widget=widgets.ListWidget(prefix_label=False),
    )
    providers_submit = SubmitField("Save Streaming Services")


class AvailabilityAlertsForm(FlaskForm):
    """Profile page: the watchlist availability digest opt-ins
    (#156/#230) — the nightly email when a watchlisted film arrives in
    the library or turns up on a subscribed service, plus the separate
    rentals opt-in. Submit label checked against the CloudFront WAF's
    adjacent-SQL-keywords rule: "Save" and "Alert" are safe together.
    """

    notify_availability = BooleanField(
        "Email me when films on my watchlist become available"
    )
    notify_rentals = BooleanField("Also tell me when they become available to rent")
    alerts_submit = SubmitField("Save Alert Settings")


class WatchlistForm(FlaskForm):
    """Watchlist toggles on film pages, and per-row removal on the
    watchlist page itself (movie_id rides in the hidden field there)."""

    movie_id = IntegerField(widget=HiddenInput(), validators=[Optional()], default=None)
    add_watchlist_submit = SubmitField("Add to Watchlist")
    remove_watchlist_submit = SubmitField("Remove from Watchlist")


class RadarrForm(FlaskForm):
    """The ad-hoc Radarr hand-off: request an unowned film for
    download, or withdraw a request — one film at a time, by a human,
    never automatically."""

    movie_id = IntegerField(widget=HiddenInput(), validators=[Optional()], default=None)
    radarr_request_submit = SubmitField("Request via Radarr")
    radarr_remove_submit = SubmitField("Remove from Radarr")


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
    """Maintenance page: merge duplicate movies sharing a TMDB id."""

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


class LossyAudioTriageForm(FlaskForm):
    """Per-file actions on the lossy-audio triage page (#212).

    The lossless track to promote travels as a plain lossless_track
    radio; the form carries the file and the three actions — remux
    with that track in the lead, keep the file as-is, or build the
    listening-clip comparison (#223).
    """

    file_id = IntegerField(validators=[Optional()], widget=HiddenInput())
    promote_submit = SubmitField("Remux with this track in the lead")
    dismiss_submit = SubmitField("Keep as-is")
    generate_submit = SubmitField("Generate listening clips")


class FilenameTestForm(FlaskForm):
    """Maintenance page: preview how a filename would import."""

    test_filename = StringField("Filename", validators=[DataRequired()])
    filename_test_submit = SubmitField("Preview import")


class GuessFrameForm(FlaskForm):
    """Name That Frame guesses: the hidden token names the
    round, and either a chosen movie id or free text arrives,
    depending on the difficulty."""

    token = StringField(widget=HiddenInput(), validators=[Optional()])
    difficulty = StringField(widget=HiddenInput(), validators=[Optional()])
    choice = StringField(validators=[Optional()])
    guess = StringField("Your guess", validators=[Optional()])
    guess_submit = SubmitField("Guess")
    # Extra Difficult (#202): trade this look at the frame for a wider
    # one instead of guessing
    zoom_out = SubmitField("Zoom Out")
    # Extra Difficult again: surrender a round that's past its first
    # zoom-out — it ends as a miss (Glenn's ask, Aug 27 2026)
    give_up = SubmitField("I give up")
