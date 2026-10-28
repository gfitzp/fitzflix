"""Poster management (split from routes.py).

This module has the per-movie and per-file picker pages, the custom
artwork uploads, the TMDB gallery picks, and the library-folder copies
that Plex reads."""

import io
import os
import re
import shutil
import traceback


from PIL import Image

from flask import (
    current_app,
    render_template,
    flash,
    redirect,
    url_for,
    request,
)

import requests

# Flask 2.4 removed flask.Markup. Import it from its real home.
from flask_login import login_required
from werkzeug.datastructures import FileStorage

from app.custom_artwork import custom_artwork_dir
from werkzeug.utils import secure_filename

from app import db
from app.cache import cached_fetch
from app.main.forms import (
    CustomPosterRemoveForm,
    CustomPosterUploadForm,
    TMDBPosterSelectForm,
)
from app.models import (
    File,
    Movie,
)
from app.main import bp
from app.main.helpers import admin_required
from app.plex_collections import enqueue_criterion_sync
from app.tmdb_apply import tmdb_json_or

# The TMDB poster gallery of a film, cached in Redis.

POSTERS_KEY = "fitzflix:tmdb:movie:{tmdb_id}:posters"
POSTERS_SECONDS = 86400


# The upload limits (#470). A poster above these pixel sizes is refused
# before the full decode. Each thumbnail comes from 1 reduced copy.

MAX_POSTER_SIZE = (6000, 9000)
THUMBNAIL_WIDTHS = ("92", "154", "185", "342", "500", "780")
UPLOAD_EXTENSIONS = (".jpg", ".jpeg", ".png")
FORMAT_EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png"}


def _jpeg_ready(image):
    """Return image in a mode that the JPEG encoder can write."""

    return image if image.mode in ("RGB", "L") else image.convert("RGB")


def _save_poster_image(image, path, image_format):
    """Write 1 poster image in its own format."""

    if image_format == "JPEG":
        _jpeg_ready(image).save(path, "JPEG", quality=95)
    else:
        image.save(path, image_format)


def save_custom_poster(uploaded_data, poster_filename, custom_poster_dir):
    """Validate an uploaded poster, then write the original and its thumbnails.

    Return the path of the saved original. Its name has the extension of
    the real image format, not of the uploaded name (#470). If the
    upload is not a usable poster image, raise ValueError with a message
    that is ready to flash.
    """

    try:
        Image.open(uploaded_data).verify()
    except Exception:
        raise ValueError(f"'{uploaded_data.filename}' is corrupted!")

    with Image.open(uploaded_data) as poster:
        current_app.logger.info(f"Uploaded poster format: {poster.format}")
        if poster.format not in FORMAT_EXTENSIONS:
            raise ValueError(f"'{poster.format}' is not an appropriate file type!")
        width, height = poster.size
        if width > MAX_POSTER_SIZE[0] or height > MAX_POSTER_SIZE[1]:
            raise ValueError(
                f"'{uploaded_data.filename}' is too large ({width}x{height} "
                f"pixels). The limit is {MAX_POSTER_SIZE[0]}x{MAX_POSTER_SIZE[1]} "
                f"pixels."
            )
        image_format = poster.format
        poster_filename = (
            os.path.splitext(poster_filename)[0] + FORMAT_EXTENSIONS[image_format]
        )

        os.makedirs(os.path.join(custom_poster_dir, "original"), exist_ok=True)
        original_file = os.path.join(custom_poster_dir, "original", poster_filename)
        _save_poster_image(poster, original_file, image_format)

        # 1 reduced copy at the largest thumbnail width. The smaller
        # thumbnails come from it, not from the full image.

        largest = int(THUMBNAIL_WIDTHS[-1])
        reduced = poster.copy()
        reduced.thumbnail((largest, max(1, int(height * largest / width))))

    for width_text in THUMBNAIL_WIDTHS:
        current_app.logger.info(f"'{original_file}' Creating w{width_text} thumbnail")
        size = (int(width_text), max(1, int(height * int(width_text) / width)))
        subdir_path = os.path.join(custom_poster_dir, f"w{width_text}")
        os.makedirs(subdir_path, exist_ok=True)
        thumbnail = reduced.copy()
        thumbnail.thumbnail(size)
        _save_poster_image(
            thumbnail, os.path.join(subdir_path, poster_filename), image_format
        )

    return original_file


def _upload_name(uploaded_data):
    """Return the poster file name of an upload, or None if its type is refused.

    The extension check ignores case. Thus, a camera "IMG_0001.JPG" is
    accepted. A .tbn upload is refused, because Pillow cannot write it."""

    file_ext = os.path.splitext(secure_filename(uploaded_data.filename))[1].lower()
    if file_ext not in UPLOAD_EXTENSIONS:
        return None
    return f"poster{file_ext}"


def replace_library_poster(library_directory, original_file, poster_filename):
    """Remove the poster art from a library directory, then copy in the new art."""

    for name in os.listdir(library_directory):
        if name.lower().startswith(
            ("cover", "default", "movie", "poster")
        ) and name.lower().endswith(("jpg", "jpeg", "png", "tbn")):
            current_app.logger.info(f"Deleting {os.path.join(library_directory, name)}")
            os.remove(os.path.join(library_directory, name))

    destination_file = os.path.join(library_directory, poster_filename)
    shutil.copy(original_file, destination_file)
    current_app.logger.info(f"'{original_file}' Copied to '{destination_file}'")


# The extensions that a library poster or an episode thumb can have

LIBRARY_POSTER_EXTENSIONS = (".jpg", ".jpeg", ".png", ".tbn")


def _episode_thumb_names(file):
    """Return the names that the episode thumb of a TV file can have.

    Plex reads "<video name without extension>.<ext>" as the thumb of 1
    episode. A season folder holds many episodes. Thus, each episode has
    its own name. The name "poster.<ext>" in a season folder is the
    poster of the season, not of an episode (#469)."""

    stem = os.path.splitext(file.basename)[0]
    return [f"{stem}{ext}" for ext in LIBRARY_POSTER_EXTENSIONS]


def replace_episode_thumb(library_directory, original_file, file):
    """Replace the episode thumb of 1 TV file. Touch no other art."""

    remove_episode_thumb(library_directory, file)
    stem = os.path.splitext(file.basename)[0]
    destination_file = os.path.join(
        library_directory, f"{stem}{os.path.splitext(original_file)[1].lower()}"
    )
    shutil.copy(original_file, destination_file)
    current_app.logger.info(f"'{original_file}' Copied to '{destination_file}'")


def remove_episode_thumb(library_directory, file):
    """Delete the episode thumb of 1 TV file, if it exists."""

    for name in _episode_thumb_names(file):
        path = os.path.join(library_directory, name)
        if os.path.isfile(path):
            os.remove(path)
            current_app.logger.info(f"Deleted '{path}'")


def _custom_poster_dir(scope, record_id):
    """Return the directory of the custom poster tree of a movie or a file.

    In production, CUSTOM_ARTWORK_DIR is app/static/custom. Thus, Fitzflix
    serves these files with url_for('static', ...). The nightly backup
    mirrors them to S3. The backup also propagates deletions.
    """

    return custom_artwork_dir(scope, record_id)


def _assign_movie_poster(movie, uploaded_data):
    """Run a poster image through the custom-poster pipeline for a movie.

    The pipeline makes the thumbnails, a copy beside each main-feature
    file, and the precedence column. Return a message to flash on
    failure, or None on success.
    """

    poster_filename = _upload_name(uploaded_data)
    if poster_filename is None:
        return f"'{uploaded_data.filename}' is an invalid movie poster file type!"

    movie_files = (
        File.query.filter(File.movie_id == movie.id)
        .filter(File.feature_type_id == None)
        .all()
    )
    custom_dir = _custom_poster_dir("movie", movie.id)
    had_tree = os.path.isdir(custom_dir)

    try:
        try:
            original_file = save_custom_poster(
                uploaded_data, poster_filename, custom_dir
            )
        except ValueError as error:
            return str(error)
        poster_filename = os.path.basename(original_file)

        for file in movie_files:
            # A file with its own custom poster keeps it. The file
            # outranks the movie (#470). A file with no local folder
            # (an archive-only copy) has no library copy to replace.
            if file.custom_poster:
                continue
            library_directory = os.path.join(
                current_app.config["LIBRARY_DIR"], file.dirname
            )
            if not os.path.isdir(library_directory):
                continue
            replace_library_poster(library_directory, original_file, poster_filename)

        movie.custom_poster = poster_filename
        db.session.commit()

        # A custom poster is part of the Criterion rule (#281).

        if movie.criterion_spine_number:
            enqueue_criterion_sync()

    except Exception:
        current_app.logger.error(traceback.format_exc())
        db.session.rollback()
        # The tree of a failed first upload must not stay. The nightly
        # backup would mirror it.
        if not had_tree:
            shutil.rmtree(custom_dir, ignore_errors=True)
        return f"Unable to assign a custom poster to '{movie.title}'!"

    return None


def _assign_file_poster(file, uploaded_data):
    """Assign the custom poster of one file (the file-scoped twin of
    _assign_movie_poster).

    This function replaces the library copy only for a main feature."""

    poster_filename = _upload_name(uploaded_data)
    if poster_filename is None:
        return f"'{uploaded_data.filename}' is an invalid movie poster file type!"
    custom_dir = _custom_poster_dir("file", file.id)
    had_tree = os.path.isdir(custom_dir)

    try:
        try:
            original_file = save_custom_poster(
                uploaded_data, poster_filename, custom_dir
            )
        except ValueError as error:
            return str(error)
        poster_filename = os.path.basename(original_file)

        library_directory = os.path.join(
            current_app.config["LIBRARY_DIR"], file.dirname
        )
        if file.series_id is not None:
            replace_episode_thumb(library_directory, original_file, file)
        elif file.feature_type_id == None:
            replace_library_poster(library_directory, original_file, poster_filename)

        file.custom_poster = poster_filename
        db.session.commit()

    except Exception:
        current_app.logger.error(traceback.format_exc())
        db.session.rollback()
        if not had_tree:
            shutil.rmtree(custom_dir, ignore_errors=True)
        return f"Unable to assign a custom poster to '{file.basename}'!"

    return None


def _remove_movie_poster(movie):
    """Delete the custom poster of a movie.

    This function deletes the custom-artwork tree, the copies beside the
    library files of the movie, and the precedence column. A main-feature
    file with its own custom poster keeps that art. The file has
    precedence over the movie. Thus, this function restores the library
    copy from the file-scoped original. It does not delete it. Return a
    message to flash on failure, or None on success.
    """

    poster_filename = movie.custom_poster
    try:
        movie_files = (
            File.query.filter(File.movie_id == movie.id)
            .filter(File.feature_type_id == None)
            .all()
        )
        for file in movie_files:
            library_directory = os.path.join(
                current_app.config["LIBRARY_DIR"], file.dirname
            )
            if not os.path.isdir(library_directory):
                continue
            if file.custom_poster:
                file_original = os.path.join(
                    _custom_poster_dir("file", file.id), "original", file.custom_poster
                )
                if os.path.isfile(file_original):
                    replace_library_poster(
                        library_directory, file_original, file.custom_poster
                    )
                    continue
            library_copy = os.path.join(library_directory, poster_filename)
            if os.path.isfile(library_copy):
                os.remove(library_copy)
                current_app.logger.info(f"Deleted '{library_copy}'")

        shutil.rmtree(_custom_poster_dir("movie", movie.id), ignore_errors=True)
        movie.custom_poster = None
        db.session.commit()
        if movie.criterion_spine_number:
            enqueue_criterion_sync()

    except Exception:
        current_app.logger.error(traceback.format_exc())
        db.session.rollback()
        return f"Unable to remove the custom poster for '{movie.title}'!"

    return None


def _remove_file_poster(file):
    """Remove the custom poster of one file (the file-scoped twin of
    _remove_movie_poster).

    If the movie still has its own custom poster, this function restores
    that art to the library directory. If not, it deletes the poster copy.
    """

    poster_filename = file.custom_poster
    try:
        if file.series_id is not None:
            library_directory = os.path.join(
                current_app.config["LIBRARY_DIR"], file.dirname
            )
            if os.path.isdir(library_directory):
                remove_episode_thumb(library_directory, file)
        elif file.feature_type_id == None:

            library_directory = os.path.join(
                current_app.config["LIBRARY_DIR"], file.dirname
            )
            if os.path.isdir(library_directory):
                movie = file.movie
                movie_original = (
                    os.path.join(
                        _custom_poster_dir("movie", movie.id),
                        "original",
                        movie.custom_poster,
                    )
                    if movie and movie.custom_poster
                    else None
                )
                if movie_original and os.path.isfile(movie_original):
                    replace_library_poster(
                        library_directory, movie_original, movie.custom_poster
                    )
                else:
                    library_copy = os.path.join(library_directory, poster_filename)
                    if os.path.isfile(library_copy):
                        os.remove(library_copy)
                        current_app.logger.info(f"Deleted '{library_copy}'")

        shutil.rmtree(_custom_poster_dir("file", file.id), ignore_errors=True)
        file.custom_poster = None
        db.session.commit()

    except Exception:
        current_app.logger.error(traceback.format_exc())
        db.session.rollback()
        return f"Unable to remove the custom poster for '{file.basename}'!"

    return None


def _tmdb_poster_gallery(tmdb_id):
    """Return the TMDB poster gallery for a movie. Cache it for 1 day.

    Return the /movie/{id}/images posters list. Return None if the gallery
    is not available (no TMDB id, no API key, or the fetch failed).
    """

    if not tmdb_id:
        return None

    def fetch():
        if not current_app.config["TMDB_API_KEY"]:
            return None, False
        body = tmdb_json_or(f"/movie/{tmdb_id}/images", None, None)
        if body is None:
            return None, False
        return body.get("posters") or [], True

    return cached_fetch(POSTERS_KEY.format(tmdb_id=tmdb_id), POSTERS_SECONDS, fetch)


def _fetch_tmdb_poster(poster_path):
    """Download a TMDB poster and wrap it as a form upload.

    Thus, a picked poster goes through the same pipeline as an uploaded
    poster. Return (file_storage, error_message).
    """

    if not re.fullmatch(r"/[A-Za-z0-9]+\.(?:jpg|jpeg|png)", poster_path or ""):
        return None, "That isn't a TMDB poster path."
    try:
        r = requests.get(
            f"{current_app.config['TMDB_IMAGE_URL']}/original{poster_path}",
            timeout=30,
        )
        r.raise_for_status()
    except Exception:
        current_app.logger.error(traceback.format_exc())
        return None, "Couldn't download that poster from TMDB."
    return (
        FileStorage(
            stream=io.BytesIO(r.content), filename=os.path.basename(poster_path)
        ),
        None,
    )


def _poster_gallery_context(posters):
    """Split a poster gallery into the languages present and the subset to
    show for the ?language= filter of the request."""

    languages = sorted({p.get("iso_639_1") or "none" for p in posters or []})
    active = request.args.get("language")
    if active not in languages and active != "all":
        # Show the English posters by default, if there are some. If not,
        # show all posters.
        active = "en" if "en" in languages else "all"
    if posters and active != "all":
        shown = [p for p in posters if (p.get("iso_639_1") or "none") == active]
    else:
        shown = posters
    return shown, languages, active


@bp.route("/movie/<int:movie_id>/poster", methods=["GET", "POST"])
@login_required
@admin_required
def movie_poster(movie_id):
    """Show the poster picker: select from the TMDB gallery or upload an
    image.

    This is an admin tool, and a library tool (#186 follow-up). A record
    with no local files shows its TMDB poster. Thus, the picker declines
    it."""

    movie = Movie.query.filter_by(id=movie_id).first_or_404()
    title = movie.display_title
    if File.query.filter_by(movie_id=movie.id).first() is None:
        flash(
            f"'{title}' has no local files. Its poster follows TMDB.",
            "warning",
        )
        return redirect(url_for("main.movie", movie_id=movie.id))

    custom_poster_form = CustomPosterUploadForm()
    poster_select_form = TMDBPosterSelectForm()
    poster_remove_form = CustomPosterRemoveForm()

    if (
        poster_remove_form.poster_remove_submit.data
        and poster_remove_form.validate_on_submit()
    ):
        if not movie.custom_poster:
            flash(f"'{title}' has no custom poster to remove.", "warning")
            return redirect(url_for("main.movie_poster", movie_id=movie.id))
        error = _remove_movie_poster(movie)
        if error:
            flash(error, "danger")
        else:
            flash(f"Removed the custom poster for '{title}'", "success")
        return redirect(url_for("main.movie_poster", movie_id=movie.id))

    if (
        custom_poster_form.poster_submit.data
        and custom_poster_form.validate_on_submit()
    ):
        error = _assign_movie_poster(movie, custom_poster_form.custom_poster.data)
        if error:
            flash(error, "danger")
            return redirect(url_for("main.movie_poster", movie_id=movie.id))
        flash(f"Uploaded a custom poster for '{title}'", "success")
        return redirect(url_for("main.movie", movie_id=movie.id))

    if (
        poster_select_form.poster_select_submit.data
        and poster_select_form.validate_on_submit()
    ):
        uploaded_data, error = _fetch_tmdb_poster(poster_select_form.poster_path.data)
        if not error:
            error = _assign_movie_poster(movie, uploaded_data)
        if error:
            flash(error, "danger")
            return redirect(url_for("main.movie_poster", movie_id=movie.id))
        flash(f"Set the poster for '{title}' from TMDB", "success")
        return redirect(url_for("main.movie", movie_id=movie.id))

    posters, languages, active_language = _poster_gallery_context(
        _tmdb_poster_gallery(movie.tmdb_id)
    )

    return render_template(
        "poster_picker.html",
        title=f'Poster for "{title}"',
        movie=movie,
        back_url=url_for("main.movie", movie_id=movie.id),
        back_label=title,
        posters=posters,
        languages=languages,
        active_language=active_language,
        language_url=lambda language: url_for(
            "main.movie_poster", movie_id=movie.id, language=language
        ),
        custom_poster_form=custom_poster_form,
        poster_select_form=poster_select_form,
        poster_remove_form=poster_remove_form,
        has_custom_poster=bool(movie.custom_poster),
        default_poster_path=movie.tmdb_poster_path,
        upload_enabled=True,
    )


@bp.route("/file/<int:file_id>/poster", methods=["GET", "POST"])
@login_required
@admin_required
def file_poster(file_id):
    """Show the poster picker for the custom poster of one file (the
    file-scoped twin of movie_poster).

    The TMDB gallery appears for a movie file. A TV file gets only the
    upload form, because TMDB season and episode artwork is not connected.
    """

    file = File.query.filter_by(id=file_id).first_or_404()
    movie = file.movie

    # Fitzflix writes a custom poster next to the library file. Thus,
    # there must be a library file.

    file_exists_locally = os.path.isfile(
        os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
    )

    custom_poster_form = CustomPosterUploadForm()
    poster_select_form = TMDBPosterSelectForm()
    poster_remove_form = CustomPosterRemoveForm()

    if (
        poster_remove_form.poster_remove_submit.data
        and poster_remove_form.validate_on_submit()
    ):
        if not file.custom_poster:
            flash(f"'{file.basename}' has no custom poster to remove.", "warning")
            return redirect(url_for("main.file_poster", file_id=file.id))
        error = _remove_file_poster(file)
        if error:
            flash(error, "danger")
        else:
            flash(f"Removed the custom poster for '{file.basename}'", "success")
        return redirect(url_for("main.file_poster", file_id=file.id))

    if (
        custom_poster_form.poster_submit.data
        and custom_poster_form.validate_on_submit()
    ):
        if not file_exists_locally:
            flash(f"'{file.basename}' is not present locally.", "warning")
            return redirect(url_for("main.file_poster", file_id=file.id))
        error = _assign_file_poster(file, custom_poster_form.custom_poster.data)
        if error:
            flash(error, "danger")
            return redirect(url_for("main.file_poster", file_id=file.id))
        flash(f"Uploaded a custom poster for '{file.basename}'", "success")
        return redirect(url_for("main.file", file_id=file.id))

    if (
        poster_select_form.poster_select_submit.data
        and poster_select_form.validate_on_submit()
    ):
        if not file_exists_locally:
            flash(f"'{file.basename}' is not present locally.", "warning")
            return redirect(url_for("main.file_poster", file_id=file.id))
        uploaded_data, error = _fetch_tmdb_poster(poster_select_form.poster_path.data)
        if not error:
            error = _assign_file_poster(file, uploaded_data)
        if error:
            flash(error, "danger")
            return redirect(url_for("main.file_poster", file_id=file.id))
        flash(f"Set the poster for '{file.basename}' from TMDB", "success")
        return redirect(url_for("main.file", file_id=file.id))

    posters, languages, active_language = _poster_gallery_context(
        _tmdb_poster_gallery(movie.tmdb_id if movie else None)
    )

    return render_template(
        "poster_picker.html",
        title=f'Poster for "{file.basename}"',
        movie=movie,
        file=file,
        back_url=url_for("main.file", file_id=file.id),
        back_label=file.basename,
        posters=posters,
        languages=languages,
        active_language=active_language,
        language_url=lambda language: url_for(
            "main.file_poster", file_id=file.id, language=language
        ),
        custom_poster_form=custom_poster_form,
        poster_select_form=poster_select_form,
        poster_remove_form=poster_remove_form,
        has_custom_poster=bool(file.custom_poster),
        default_poster_path=movie.tmdb_poster_path if movie else None,
        upload_enabled=file_exists_locally,
    )
