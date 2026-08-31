"""DVR channel editor (#182): admin CRUD over the dvr_channel table.

The dial is data: each row is a channel with rule columns (genres,
keywords, network country, title pins, Criterion/leaving overlays)
plus explicit movie/series picks resolved here by title text. Every
mutation enqueues a lineup rebuild so Plex's next guide refresh sees
the new dial. Slugs are frozen at creation — they are the tvg-ids and
stream URLs Plex maps by, so renaming a channel never moves its
stream.
"""

import re

from flask import abort, current_app, flash, redirect, render_template, url_for
from flask_login import login_required

from app import db
from app.dvr import _slugify, channel_lineup
from app.main import bp
from app.main.forms import DVRChannelActionForm, DVRChannelForm, DVRMemberForm
from app.main.helpers import admin_required
from app.models import DVRChannel, Movie, TMDBGenre, TVSeries


def _enqueue_rebuild(reason):
    """Queue a lineup rebuild so the stored dial catches up with the
    edited definitions."""

    current_app.maintenance_queue.enqueue(
        "app.dvr.build_channel_lineups",
        args=(),
        job_timeout=3600,
        description=f"Building virtual DVR channel lineups ({reason})",
    )


def _apply_form(channel, form):
    """Copy the editor form onto the channel row — everything except
    the slug, which is frozen at creation."""

    channel.name = form.name.data.strip()
    channel.number = form.number.data
    channel.enabled = form.enabled.data
    channel.include_movies = form.include_movies.data
    channel.include_tv = form.include_tv.data
    channel.genres = (form.genres.data or "").strip() or None
    channel.keywords = (form.keywords.data or "").strip() or None
    channel.network_country = (form.network_country.data or "").strip().upper() or None
    channel.title_pins = (form.title_pins.data or "").strip() or None
    channel.criterion_only = form.criterion_only.data
    channel.leaving_only = form.leaving_only.data


def _identity_taken(form, channel_id=None):
    """A flashable error when the form's number, name, or derived slug
    collides with another channel; None when free."""

    slug = _slugify(form.name.data)
    if not slug:
        return "The channel name needs at least one letter or number."
    for other in DVRChannel.query.filter(
        (DVRChannel.number == form.number.data)
        | (DVRChannel.name == form.name.data.strip())
        | (DVRChannel.slug == slug)
    ).all():
        if other.id != channel_id:
            return (
                f"Channel {other.number} ({other.name}) already uses that "
                f"number, name, or slug."
            )
    return None


def _rules_summary(channel):
    """One line describing a channel's membership rules for the list
    page."""

    parts = []
    if channel.include_movies:
        parts.append("movies")
    if channel.include_tv:
        parts.append("TV")
    if channel.genres:
        parts.append(f"genres: {channel.genres}")
    if channel.keywords:
        parts.append(f"keywords: {channel.keywords}")
    if channel.network_country:
        parts.append(f"networks: {channel.network_country}")
    if channel.title_pins:
        parts.append(f"pins: {channel.title_pins}")
    if channel.criterion_only:
        parts.append("Criterion-only")
    if channel.leaving_only:
        parts.append("leaving-only")
    picks = channel.movies.count() + channel.series.count()
    if picks:
        parts.append(f"{picks} explicit pick{'s' if picks != 1 else ''}")
    return " · ".join(parts) or "no rules"


def _resolve_movie(text):
    """A movie resolved from title text — exact "Title (Year)" first,
    then a unique substring. Returns (movie, error)."""

    text = (text or "").strip()
    if not text:
        return None, "Enter a movie title."
    match = re.match(r"^(.*\S)\s+\((\d{4})\)$", text)
    if match:
        candidates = Movie.query.filter(
            db.func.lower(Movie.title) == match.group(1).lower(),
            Movie.year == int(match.group(2)),
        ).all()
    else:
        candidates = (
            Movie.query.filter(db.func.lower(Movie.title).like(f"%{text.lower()}%"))
            .order_by(Movie.title, Movie.year)
            .limit(6)
            .all()
        )
    if len(candidates) == 1:
        return candidates[0], None
    if not candidates:
        return None, f'No movie matches "{text}".'
    options = "; ".join(f"{m.title} ({m.year})" for m in candidates[:5])
    return None, f'"{text}" is ambiguous — try one of: {options}.'


def _resolve_series(text):
    """A TV series resolved from title text — exact title first, then
    a unique substring. Returns (series, error)."""

    text = (text or "").strip()
    if not text:
        return None, "Enter a series title."
    exact = TVSeries.query.filter(db.func.lower(TVSeries.title) == text.lower()).all()
    candidates = (
        exact
        or TVSeries.query.filter(
            db.func.lower(TVSeries.title).like(f"%{text.lower()}%")
        )
        .order_by(TVSeries.title)
        .limit(6)
        .all()
    )
    if len(candidates) == 1:
        return candidates[0], None
    if not candidates:
        return None, f'No series matches "{text}".'
    options = "; ".join(series.title for series in candidates[:5])
    return None, f'"{text}" is ambiguous — try one of: {options}.'


@bp.route("/dvr/channels", methods=["GET", "POST"])
@login_required
@admin_required
def dvr_channels():
    """The dial: every channel row with its rules and last-built
    program count, a creation form, and delete/rebuild actions."""

    form = DVRChannelForm()
    action_form = DVRChannelActionForm()

    if form.save_submit.data and form.validate_on_submit():
        error = _identity_taken(form)
        if error:
            flash(error, "danger")
        else:
            channel = DVRChannel(slug=_slugify(form.name.data))
            _apply_form(channel, form)
            db.session.add(channel)
            db.session.commit()
            _enqueue_rebuild(f"created {channel.name}")
            flash(f"Channel {channel.number} ({channel.name}) created.", "success")
            return redirect(url_for("main.dvr_channel_edit", channel_id=channel.id))
        return redirect(url_for("main.dvr_channels"))

    if action_form.delete_submit.data and action_form.validate_on_submit():
        channel = db.session.get(DVRChannel, action_form.channel_id.data or 0)
        if channel:
            db.session.delete(channel)
            db.session.commit()
            _enqueue_rebuild(f"deleted {channel.name}")
            flash(f"Channel {channel.number} ({channel.name}) deleted.", "success")
        return redirect(url_for("main.dvr_channels"))

    if action_form.rebuild_submit.data and action_form.validate_on_submit():
        _enqueue_rebuild("manual")
        flash("Rebuilding the channel lineups.", "info")
        return redirect(url_for("main.dvr_channels"))

    channels = DVRChannel.query.order_by(DVRChannel.number.asc()).all()
    counts, summaries = {}, {}
    for channel in channels:
        lineup = channel_lineup(current_app.redis, channel.slug)
        counts[channel.id] = len(lineup["programs"]) if lineup else 0
        summaries[channel.id] = _rules_summary(channel)
    return render_template(
        "dvr_channels.html",
        title="DVR Channels",
        channels=channels,
        counts=counts,
        summaries=summaries,
        form=form,
        action_form=action_form,
    )


@bp.route("/dvr/channels/<int:channel_id>", methods=["GET", "POST"])
@login_required
@admin_required
def dvr_channel_edit(channel_id):
    """One channel's editor: the rule fields, plus explicit movie and
    series picks added and removed by title."""

    channel = db.session.get(DVRChannel, channel_id)
    if channel is None:
        abort(404)
    form = DVRChannelForm(obj=channel)
    member_form = DVRMemberForm()

    if form.save_submit.data and form.validate_on_submit():
        error = _identity_taken(form, channel_id=channel.id)
        if error:
            flash(error, "danger")
        else:
            _apply_form(channel, form)
            db.session.commit()
            _enqueue_rebuild(f"edited {channel.name}")
            flash(f"Channel {channel.number} ({channel.name}) saved.", "success")
        return redirect(url_for("main.dvr_channel_edit", channel_id=channel.id))

    if member_form.add_movie_submit.data and member_form.validate_on_submit():
        movie, error = _resolve_movie(member_form.member_title.data)
        if error:
            flash(error, "danger")
        elif channel.movies.filter(Movie.id == movie.id).first():
            flash(f"{movie.title} ({movie.year}) is already on this channel.", "info")
        else:
            channel.movies.append(movie)
            db.session.commit()
            _enqueue_rebuild(f"edited {channel.name}")
            flash(f"Added {movie.title} ({movie.year}).", "success")
        return redirect(url_for("main.dvr_channel_edit", channel_id=channel.id))

    if member_form.add_series_submit.data and member_form.validate_on_submit():
        series, error = _resolve_series(member_form.member_title.data)
        if error:
            flash(error, "danger")
        elif channel.series.filter(TVSeries.id == series.id).first():
            flash(f"{series.title} is already on this channel.", "info")
        else:
            channel.series.append(series)
            db.session.commit()
            _enqueue_rebuild(f"edited {channel.name}")
            flash(f"Added {series.title}.", "success")
        return redirect(url_for("main.dvr_channel_edit", channel_id=channel.id))

    if member_form.remove_submit.data and member_form.validate_on_submit():
        member_id = member_form.member_id.data or 0
        if member_form.member_kind.data == "movie":
            member = channel.movies.filter(Movie.id == member_id).first()
            if member:
                channel.movies.remove(member)
        else:
            member = channel.series.filter(TVSeries.id == member_id).first()
            if member:
                channel.series.remove(member)
        if member:
            db.session.commit()
            _enqueue_rebuild(f"edited {channel.name}")
            flash("Removed.", "success")
        return redirect(url_for("main.dvr_channel_edit", channel_id=channel.id))

    genre_names = [
        name
        for (name,) in db.session.query(TMDBGenre.name)
        .distinct()
        .order_by(TMDBGenre.name)
        .all()
    ]
    lineup = channel_lineup(current_app.redis, channel.slug)
    return render_template(
        "dvr_channel.html",
        title=f"DVR Channel {channel.number}",
        channel=channel,
        form=form,
        member_form=member_form,
        genre_names=genre_names,
        program_count=len(lineup["programs"]) if lineup else 0,
        movies=channel.movies.order_by(Movie.title, Movie.year).all(),
        series=channel.series.order_by(TVSeries.title).all(),
    )
