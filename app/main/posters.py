"""Poster management (split from routes.py).

This module has the per-movie and per-file picker pages, the custom
artwork uploads, the TMDB gallery picks, and the library-folder copies
that Plex reads."""

import io
import json
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
from werkzeug.utils import secure_filename

from app import db
from app.main.forms import (
    CustomPosterRemoveForm,
    CustomPosterUploadForm,
    TMDBPosterSelectForm,
)
from app.models import (
    File,
    Movie,
    tmdb_get,
)
from app.main import bp
from app.main.helpers import admin_required


def save_custom_poster(uploaded_data, poster_filename, custom_poster_dir):
    """Validate an uploaded poster, then write the original and its thumbnails.

    Return the path of the saved original. If the upload is not a usable
    poster image, raise ValueError with a message that is ready to flash.
    """

    try:
        Image.open(uploaded_data).verify()
    except Exception:
        raise ValueError(f"'{uploaded_data.filename}' is corrupted!")

    with Image.open(uploaded_data) as poster:
        current_app.logger.info(f"Uploaded poster format: {poster.format}")
        if poster.format not in ["JPEG", "PNG"]:
            raise ValueError(f"'{poster.format}' is not an appropriate file type!")

        os.makedirs(os.path.join(custom_poster_dir, "original"), exist_ok=True)
        original_file = os.path.join(custom_poster_dir, "original", poster_filename)
        poster.save(original_file)

        original_width, original_height = poster.size

        for width in ["92", "154", "185", "342", "500", "780"]:
            current_app.logger.info(f"'{original_file}' Creating w{width} thumbnail")

            percent = int(width) / float(original_width)
            height = int(original_height * float(percent))
            size = (int(width), int(height))

            subdir_path = os.path.join(custom_poster_dir, f"w{width}")
            os.makedirs(subdir_path, exist_ok=True)

            poster_thumbnail = poster.copy()
            poster_thumbnail.thumbnail(size)
            if poster.format == "JPEG":
                poster_thumbnail.save(
                    os.path.join(subdir_path, poster_filename), quality=95
                )
            else:
                poster_thumbnail.save(os.path.join(subdir_path, poster_filename))

    return original_file


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


def _custom_poster_dir(scope, record_id):
    """Return the directory of the custom poster tree of a movie or a file.

    In production, CUSTOM_ARTWORK_DIR is app/static/custom. Thus, Fitzflix
    serves these files with url_for('static', ...). The nightly backup
    mirrors them to S3. The backup also propagates deletions.
    """

    return os.path.join(current_app.config["CUSTOM_ARTWORK_DIR"], scope, str(record_id))


def _assign_movie_poster(movie, uploaded_data):
    """Run a poster image through the custom-poster pipeline for a movie.

    The pipeline makes the thumbnails, a copy beside each main-feature
    file, and the precedence column. Return a message to flash on
    failure, or None on success.
    """

    file_ext = os.path.splitext(secure_filename(uploaded_data.filename))[1]
    poster_filename = f"poster{file_ext}"

    if file_ext not in [".jpg", ".jpeg", ".png", ".tbn"]:
        return f"'{poster_filename}' is an invalid movie poster file type!"

    movie_files = (
        File.query.filter(File.movie_id == movie.id)
        .filter(File.feature_type_id == None)
        .all()
    )

    try:
        try:
            original_file = save_custom_poster(
                uploaded_data,
                poster_filename,
                _custom_poster_dir("movie", movie.id),
            )
        except ValueError as error:
            return str(error)

        for file in movie_files:
            replace_library_poster(
                os.path.join(current_app.config["LIBRARY_DIR"], file.dirname),
                original_file,
                poster_filename,
            )

        movie.custom_poster = poster_filename
        db.session.commit()

    except Exception:
        current_app.logger.error(traceback.format_exc())
        db.session.rollback()
        return f"Unable to assign a custom poster to '{movie.title}'!"

    return None


def _assign_file_poster(file, uploaded_data):
    """Assign the custom poster of one file (the file-scoped twin of
    _assign_movie_poster).

    This function replaces the library copy only for a main feature."""

    file_ext = os.path.splitext(secure_filename(uploaded_data.filename))[1]
    poster_filename = f"poster{file_ext}"

    if file_ext not in [".jpg", ".jpeg", ".png", ".tbn"]:
        return f"'{poster_filename}' is an invalid movie poster file type!"

    try:
        try:
            original_file = save_custom_poster(
                uploaded_data,
                poster_filename,
                _custom_poster_dir("file", file.id),
            )
        except ValueError as error:
            return str(error)

        if file.feature_type_id == None:
            replace_library_poster(
                os.path.join(current_app.config["LIBRARY_DIR"], file.dirname),
                original_file,
                poster_filename,
            )

        file.custom_poster = poster_filename
        db.session.commit()

    except Exception:
        current_app.logger.error(traceback.format_exc())
        db.session.rollback()
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
        if file.feature_type_id == None:
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
    cache_key = f"fitzflix:tmdb:movie:{tmdb_id}:posters"
    cached = current_app.redis.get(cache_key)
    if cached:
        return json.loads(cached)
    if not current_app.config["TMDB_API_KEY"]:
        return None
    try:
        r = tmdb_get(
            current_app.config["TMDB_API_URL"] + f"/movie/{tmdb_id}/images",
            params={"api_key": current_app.config["TMDB_API_KEY"]},
            timeout=10,
        )
        r.raise_for_status()
        posters = r.json().get("posters") or []
    except Exception:
        current_app.logger.warning(traceback.format_exc())
        return None
    current_app.redis.set(cache_key, json.dumps(posters), ex=86400)
    return posters


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
    title = f"{movie.tmdb_title if movie.tmdb_title else movie.title} ({movie.tmdb_release_date.strftime('%Y') if movie.tmdb_title else movie.year})"
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
