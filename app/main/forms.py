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
    Length,
    NumberRange,
    Optional,
    ValidationError,
)
from wtforms.widgets import HiddenInput
from app.models import User


class ResetFrameScoresForm(FlaskForm):
    """Delete the Name That Frame standings of the user, on the Profile page.

    The standings are the streaks, bests, points, and win rates on every
    difficulty."""

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
        """Reject an address that a different account already uses."""

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
    """Name the Letterboxd account whose RSS feed syncs into the diary.

    A blank value disables the sync."""

    letterboxd_username = StringField("Letterboxd Username", validators=[Optional()])
    letterboxd_submit = SubmitField("Update Letterboxd Sync")


class PlexPlayerForm(FlaskForm):
    """Set the playback device of this user.

    The value is the address of the Plex player that the play buttons
    target. The profile route probes the address and reads the machine
    id from the player. A blank value removes the device and the play
    buttons with it."""

    plex_player_address = StringField(
        "Playback Device Address", validators=[Optional()]
    )
    plex_player_submit = SubmitField("Update Playback Device")


class InfusePlayerForm(FlaskForm):
    """Set the Apple TV Companion address that Infuse playback targets.

    A submit starts the one-time PIN pairing. The TV shows a PIN, and
    the user enters it through InfusePinForm. A blank value removes the
    device."""

    infuse_player_address = StringField("Apple TV Address", validators=[Optional()])
    infuse_player_submit = SubmitField("Pair Apple TV for Infuse")


class InfusePinForm(FlaskForm):
    """Enter the PIN that the Apple TV shows for a pairing in progress."""

    infuse_pin = StringField("PIN Shown on the Apple TV", validators=[DataRequired()])
    infuse_pin_submit = SubmitField("Finish Pairing")


class DefaultPlayerForm(FlaskForm):
    """Choose the app that the plain play buttons target.

    This applies when the user has both Plex and Infuse playback
    configured.

    The submit label must not put 2 SQL keywords next to each other. The
    SQLi_BODY rule of the CloudFront WAF blocked "Set Default Player"
    live with a bare 403 (2026-08-26). A test against the same WAF
    showed that "Update Default Player" is also blocked (UPDATE and
    DEFAULT are both keywords). "Update Playback Device" and "Save
    Default Player" pass. One keyword is fine. An adjacent pair reads as
    SQL.
    """

    default_player = RadioField(
        "Default Player",
        choices=[("plex", "Plex"), ("infuse", "Infuse")],
        validators=[DataRequired()],
    )
    default_player_submit = SubmitField("Save Default Player")


class ImportForm(FlaskForm):
    """Start an import-directory scan manually."""

    submit = SubmitField("Scan Import Directory")


class MovieReviewForm(FlaskForm):
    """Log or edit a viewing.

    The rating comes from the quick-answer ladder that goes with the
    form. A rating of 3 or more stars automatically sets liked. The
    date is optional. A blank date means seen at an unknown time. The
    submit button logs a bare unrated watch."""

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
    """Refresh the TMDB data of a title, or point it at a new id (movie page).

    A blank id means "search TMDB by title". This is useful for a record
    that was never matched. It is destructive for a record that was
    matched. Thus, the routes refuse a blank submit when an id is
    already stored (#207).
    """

    tmdb_id = IntegerField("TMDB ID", validators=[Optional()])
    lookup_submit = SubmitField("Refresh TMDB Data")


class TMDBRemoveForm(FlaskForm):
    """Detach a record from TMDB permanently (movie and TV pages).

    This is for titles that TMDB has no entry for. Examples are home
    movies, and ids that TMDB deleted while a record still used them.
    """

    remove_submit = SubmitField("Remove TMDB ID")


class RuntimeMismatchForm(FlaskForm):
    """Accept the length of one flagged file as known-benign (#234).

    This is on the runtime triage page. The file then stops appearing
    there."""

    file_id = IntegerField(widget=HiddenInput(), validators=[Optional()], default=None)
    acknowledge_submit = SubmitField("Acknowledge")


class TMDBTriageForm(FlaskForm):
    """Flag an unmatched record as unmatchable, or match it to an id (#226).

    This is on the TMDB triage page. The flag uses the path of the
    Remove button. Then no refresh guesses an id from the title. The
    match uses an id that the user enters by hand. One hidden id goes
    with each row, for a movie or a series."""

    movie_id = IntegerField(widget=HiddenInput(), validators=[Optional()], default=None)
    series_id = IntegerField(
        widget=HiddenInput(), validators=[Optional()], default=None
    )
    tmdb_id = IntegerField("TMDB ID", validators=[Optional()])
    flag_submit = SubmitField("Flag as unmatchable")
    lookup_submit = SubmitField("Match")


class TMDBRefreshForm(FlaskForm):
    """Refresh the TMDB data for the whole library (Maintenance page)."""

    tmdb_refresh = SubmitField("Refresh TMDB Info")


class CriterionForm(FlaskForm):
    """Edit the Criterion Collection details of a film (movie page)."""

    spine_number = IntegerField("Spine #", validators=[Optional()])
    set_title = StringField("Collector's Set Title", validators=[Optional()])
    in_print = BooleanField("In Print", validators=[Optional()])
    quality = SelectField("Released as")
    owned = BooleanField("Owned", validators=[Optional()])
    criterion_submit = SubmitField("Update Criterion Info")

    def validate_spine_number(self, spine_number):
        """Reject a spine number that is not positive."""

        if spine_number.data < 1:
            raise ValidationError("Enter a positive spine number.")


class TranscodeForm(FlaskForm):
    """Queue transcodes, from the movie and file pages."""

    transcode_submit = SubmitField("Create Transcoded File")
    transcode_all = SubmitField("Transcode All")


class LibrarySearchForm(FlaskForm):
    """Show the search box of the library listings."""

    search_query = StringField("Search...", validators=[Optional()])
    search_submit = SubmitField("Search")


class CriterionRefreshForm(FlaskForm):
    """Refresh the Criterion data from Wikidata (Maintenance page)."""

    criterion_refresh = SubmitField("Refresh Criterion Collection Info")


class MovieShoppingFilterForm(FlaskForm):
    """Filter the movie shopping list by library, media, and quality range."""

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
    """Filter the TV shopping list by maximum quality."""

    quality = SelectField("Maximum quality")
    filter_submit = SubmitField("Filter")


class StreamingProvidersForm(FlaskForm):
    """Choose the streaming services of this user (Profile page).

    The availability displays (movie pages, TMDB search) use these
    choices per user. This is not a site-wide setting.
    """

    providers = SelectMultipleField(
        "Streaming services",
        coerce=int,
        option_widget=widgets.CheckboxInput(),
        widget=widgets.ListWidget(prefix_label=False),
    )
    providers_submit = SubmitField("Save Streaming Services")


class AvailabilityAlertsForm(FlaskForm):
    """Set the opt-ins for the watchlist availability digest (#156/#230).

    This is on the Profile page. The digest is the nightly email that
    reports a watchlisted film that arrived in the library or on a
    subscribed service. The rentals opt-in is separate. The submit label
    was checked against the adjacent-SQL-keywords rule of the CloudFront
    WAF. "Save" and "Alert" are safe together.
    """

    notify_availability = BooleanField(
        "Email me when films on my watchlist become available or are leaving a service"
    )
    notify_rentals = BooleanField("Also tell me when they become available to rent")
    alerts_submit = SubmitField("Save Alert Settings")


class WatchlistForm(FlaskForm):
    """Toggle the watchlist on film pages, or remove a row on the watchlist page.

    On the watchlist page, movie_id goes in the hidden field."""

    movie_id = IntegerField(widget=HiddenInput(), validators=[Optional()], default=None)
    add_watchlist_submit = SubmitField("Add to Watchlist")
    remove_watchlist_submit = SubmitField("Remove from Watchlist")


class RadarrForm(FlaskForm):
    """Request an unowned film for download through Radarr, or withdraw it.

    This is the ad-hoc Radarr hand-off. It handles one film at a time.
    A person makes the request. Fitzflix never makes it automatically."""

    movie_id = IntegerField(widget=HiddenInput(), validators=[Optional()], default=None)
    radarr_request_submit = SubmitField("Request via Radarr")
    radarr_remove_submit = SubmitField("Remove from Radarr")


class ReviewExportForm(FlaskForm):
    """Email the Letterboxd-format review export (History page).

    The default export covers only the entries added or edited since the
    last export. The checkbox requests everything.
    """

    full_export = BooleanField("Full export")
    export_submit = SubmitField("Export Reviews")


class ReviewUploadForm(FlaskForm):
    """Import a Letterboxd zip or a legacy ratings file (History page)."""

    file = FileField("Reviews File")
    upload_submit = SubmitField("Import Reviews")


class S3DownloadForm(FlaskForm):
    """Restore a file from AWS after a password check (file page)."""

    password = PasswordField("Password:", validators=[DataRequired()])
    s3_download_submit = SubmitField("Restore from AWS")


class SeasonRestoreForm(FlaskForm):
    """Restore a season from AWS in bulk after a password check (season page)."""

    password = PasswordField("Password:", validators=[DataRequired()])
    season_restore_submit = SubmitField("Bulk restore season from AWS")


class SeriesRestoreForm(FlaskForm):
    """Restore a series from AWS in bulk after a password check (series page)."""

    password = PasswordField("Password:", validators=[DataRequired()])
    series_restore_submit = SubmitField("Bulk restore series from AWS")


class S3UploadForm(FlaskForm):
    """Upload the original file to AWS (file page)."""

    s3_upload_submit = SubmitField("Upload to AWS")


class MultiCheckboxField(SelectMultipleField):
    """Render a SelectMultipleField as a list of checkboxes."""

    widget = widgets.ListWidget(prefix_label=False)
    option_widget = widgets.CheckboxInput()


class MKVPropEditForm(FlaskForm):
    """Set the default tracks and the forced flags of a file (file page)."""

    default_audio = RadioField("Default audio track", validators=[Optional()])
    default_subtitle = RadioField("Default subtitle track", validators=[Optional()])
    forced_subtitles = MultiCheckboxField(
        "Forced subtitle tracks", validators=[Optional()]
    )
    mkvpropedit_submit = SubmitField("Update MKV Properties")


class MKVMergeForm(FlaskForm):
    """Remove unwanted tracks through a remux (file page)."""

    audio_tracks = MultiCheckboxField("Audio tracks", validators=[Optional()])
    subtitle_tracks = MultiCheckboxField("Subtitle tracks", validators=[Optional()])
    mkvmerge_submit = SubmitField("Remux MKV File")


class SyncAWSStorageForm(FlaskForm):
    """Sync and prune the S3 storage after a password check (Maintenance page)."""

    password = PasswordField("Password:", validators=[DataRequired()])
    sync_submit = SubmitField("Sync AWS S3 Storage")


class FileDeleteForm(FlaskForm):
    """Delete a file and purge its records (file page)."""

    delete_submit = SubmitField("Delete and Purge File")


class SeriesDeleteForm(FlaskForm):
    """Delete the series (series page)."""

    delete_submit = SubmitField("Delete Series")


class TrackMetadataScanForm(FlaskForm):
    """Scan the track metadata again, per file or for the whole library."""

    scan_submit = SubmitField("Rescan Track Metadata")


class MovieShoppingExcludeForm(FlaskForm):
    """Add a title to the shopping list or remove it."""

    movie_id = IntegerField("Movie ID", validators=[Optional()], widget=HiddenInput())
    add_submit = SubmitField("Add to List")
    exclude_submit = SubmitField("Remove from List")


class CustomPosterUploadForm(FlaskForm):
    """Upload a custom poster image (poster picker)."""

    custom_poster = FileField("Poster Image File", validators=[FileRequired()])
    poster_submit = SubmitField("Upload")


class TMDBPosterSelectForm(FlaskForm):
    """Show one "use this poster" button per image on the poster picker page."""

    poster_path = HiddenField(validators=[DataRequired()])
    poster_select_submit = SubmitField("Use this poster")


class CustomPosterRemoveForm(FlaskForm):
    """Delete the custom poster (poster picker)."""

    poster_remove_submit = SubmitField("Remove custom poster")


class FailedJobForm(FlaskForm):
    """Requeue or forget a failed background job (System page)."""

    failed_job_id = HiddenField(validators=[DataRequired()])
    failed_queue = HiddenField(validators=[DataRequired()])
    requeue_submit = SubmitField("Requeue")
    forget_submit = SubmitField("Forget")


class RejectActionForm(FlaskForm):
    """Send a file back for import, or delete it (Rejects page)."""

    file_path = HiddenField(validators=[DataRequired()])
    reimport_submit = SubmitField("Re-import")
    delete_submit = SubmitField("Delete")


class MovieMergeForm(FlaskForm):
    """Merge duplicate movies that share a TMDB id (Maintenance page)."""

    merge_tmdb_id = HiddenField(validators=[DataRequired()])
    merge_submit = SubmitField("Merge")


class SubtitleTriageForm(FlaskForm):
    """Apply per-file actions on the possibly-forced subtitles triage page.

    The track selection travels as plain track_ids checkboxes. A file
    can hide more than one forced track. The form carries the file and
    the 2 actions.
    """

    file_id = IntegerField(validators=[Optional()], widget=HiddenInput())
    mark_forced_submit = SubmitField("Flag selected as forced")
    dismiss_submit = SubmitField("Nothing forced here")


class LossyAudioTriageForm(FlaskForm):
    """Apply per-file actions on the lossy-audio triage page (#212).

    The lossless track to promote travels as a plain lossless_track
    radio. The form carries the file and the 3 actions: remux with that
    track in the lead, keep the file as it is, or build the
    listening-clip comparison (#223).
    """

    file_id = IntegerField(validators=[Optional()], widget=HiddenInput())
    promote_submit = SubmitField("Remux with this track in the lead")
    dismiss_submit = SubmitField("Keep as-is")
    generate_submit = SubmitField("Generate listening clips")


class FilenameTestForm(FlaskForm):
    """Preview how Fitzflix imports a filename (Maintenance page)."""

    test_filename = StringField("Filename", validators=[DataRequired()])
    filename_test_submit = SubmitField("Preview import")


class GuessFrameForm(FlaskForm):
    """Submit a Name That Frame guess.

    The hidden token names the round. The guess is a chosen movie id or
    free text, depending on the difficulty."""

    token = StringField(widget=HiddenInput(), validators=[Optional()])
    difficulty = StringField(widget=HiddenInput(), validators=[Optional()])
    choice = StringField(validators=[Optional()])
    guess = StringField("Your guess", validators=[Optional()])
    guess_submit = SubmitField("Guess")
    # Extra Difficult (#202): trade this look at the frame for a wider
    # look, instead of a guess.
    zoom_out = SubmitField("Zoom Out")
    # Extra Difficult again: give up a round that is past its first
    # zoom-out. The round ends as a miss (requested by Glenn, 2026-08-27).
    give_up = SubmitField("I give up")


class DVRChannelForm(FlaskForm):
    """Create or edit the definition of one DVR channel (#182).

    This is the DVR channel editor. The rule fields are comma-separated
    term lists. Fitzflix derives the slug from the name at creation.
    The slug never changes after that."""

    channel_id = IntegerField(validators=[Optional()], widget=HiddenInput())
    name = StringField("Name", validators=[DataRequired(), Length(max=64)])
    number = IntegerField(
        "Number", validators=[DataRequired(), NumberRange(min=1, max=9999)]
    )
    enabled = BooleanField("Enabled", default=True)
    include_movies = BooleanField("Movies matching the rules")
    include_tv = BooleanField("TV series matching the rules")
    genres = StringField("Genres", validators=[Optional(), Length(max=1024)])
    keywords = StringField("Keywords", validators=[Optional(), Length(max=1024)])
    network_country = StringField(
        "Network country", validators=[Optional(), Length(max=16)]
    )
    title_pins = StringField("Title pins", validators=[Optional(), Length(max=1024)])
    criterion_only = BooleanField("Only films streaming on the Criterion Channel")
    leaving_only = BooleanField("Only films leaving the Criterion Channel")
    save_submit = SubmitField("Save channel")


class DVRChannelActionForm(FlaskForm):
    """Delete a channel or start a lineup rebuild (DVR channel list)."""

    channel_id = IntegerField(validators=[Optional()], widget=HiddenInput())
    delete_submit = SubmitField("Delete")
    rebuild_submit = SubmitField("Rebuild lineups now")


class DVRMemberForm(FlaskForm):
    """Add or remove an explicit movie or series member (DVR channel editor).

    The server resolves the member from the title text."""

    channel_id = IntegerField(validators=[Optional()], widget=HiddenInput())
    member_title = StringField("Title", validators=[Optional(), Length(max=255)])
    member_kind = StringField(widget=HiddenInput(), validators=[Optional()])
    member_id = IntegerField(validators=[Optional()], widget=HiddenInput())
    add_movie_submit = SubmitField("Add movie")
    add_series_submit = SubmitField("Add series")
    remove_submit = SubmitField("Remove")
