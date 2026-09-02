"""Edit the DVR channels (#182). Admins do CRUD on the dvr_channel table.

The dial is data. Each row is a channel with rule columns: genres,
keywords, network country, title pins, and the Criterion and leaving
overlays. Each row also has explicit movie and series picks. This
module resolves the picks from title text. Each change enqueues a
lineup rebuild. Thus, the next guide refresh of Plex sees the new dial.
The slug of a channel is frozen at creation. Plex maps the tvg-ids and
the stream URLs by the slug. Thus, a rename of a channel never moves
its stream.
"""

import re

from flask import (
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import login_required

from app import db
from app.dvr import _slugify, channel_lineup
from app.main import bp
from app.main.forms import DVRChannelActionForm, DVRChannelForm, DVRMemberForm
from app.main.helpers import admin_required
from app.models import DVRChannel, Movie, TMDBGenre, TVSeries


def _enqueue_rebuild(reason):
    """Queue a lineup rebuild. Thus, the stored dial matches the edited
    definitions.

    Return the job. Return None when a rebuild is already queued (that
    rebuild picks up the edit), or when the DVR is not configured."""

    from app.dvr import enqueue_lineup_rebuild

    return enqueue_lineup_rebuild(reason)


def _apply_form(channel, form):
    """Copy the editor form onto the channel row.

    This copies each field except the slug. The slug is frozen at
    creation."""

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
    """Return an error message if the identity of the form is in use.

    The number, the name, or the derived slug of the form can collide
    with a different channel. Then this returns a message that the
    caller can flash. Return None if the identity is free."""

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
    """Return one line that describes the membership rules of a channel.

    The list page shows this line."""

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
    """Resolve a movie from title text. Return (movie, error).

    This tries an exact "Title (Year)" match first. Then it tries a
    unique substring match."""

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
    """Resolve a TV series from title text. Return (series, error).

    This tries an exact title match first. Then it tries a unique
    substring match."""

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


@bp.route("/dvr/title-search.json")
@login_required
@admin_required
def dvr_title_search():
    """Return the lookahead suggestions for the pick fields of the editor.

    The suggestions are canonical title strings: "Title (Year)" for a
    movie, and the bare title for a series. The member resolvers parse
    exactly these strings. Thus, a picked suggestion always resolves to
    one item."""

    query = (request.args.get("q") or "").strip().lower()
    if len(query) < 2:
        return jsonify({"results": []})
    if request.args.get("kind") == "series":
        rows = (
            TVSeries.query.filter(db.func.lower(TVSeries.title).like(f"%{query}%"))
            .order_by(TVSeries.title)
            .limit(8)
            .all()
        )
        results = [series.title for series in rows]
    else:
        rows = (
            Movie.query.filter(db.func.lower(Movie.title).like(f"%{query}%"))
            .order_by(Movie.title, Movie.year)
            .limit(8)
            .all()
        )
        results = [f"{movie.title} ({movie.year})" for movie in rows]
    return jsonify({"results": results})


@bp.route("/dvr/channels", methods=["GET", "POST"])
@login_required
@admin_required
def dvr_channels():
    """Show the dial and apply its actions.

    The page shows each channel row with its rules and the program
    count of the last build. It also shows a creation form and the
    delete and rebuild actions."""

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
        if _enqueue_rebuild("manual"):
            flash("Fitzflix will rebuild the channel lineups.", "info")
        else:
            flash(
                "No rebuild queued. A rebuild is already waiting, or "
                "DVR_TOKEN is not configured.",
                "warning",
            )
        return redirect(url_for("main.dvr_channels"))

    channels = DVRChannel.query.order_by(DVRChannel.number.asc()).all()
    counts, summaries = {}, {}
    for channel in channels:
        lineup = channel_lineup(current_app.redis, channel.slug)
        counts[channel.id] = len(lineup["programs"]) if lineup else 0
        summaries[channel.id] = _rules_summary(channel)

    # These are the two URLs that the Plex setup asks for. They use the
    # address that Plex uses to reach Fitzflix (DVR_TUNER_URL). The user
    # can copy and paste them.

    setup = None
    token = current_app.config["DVR_TOKEN"]
    if token:
        base = current_app.config["DVR_TUNER_URL"].rstrip("/")
        setup = {
            "tuner": f"{base}/dvr/{token}",
            "guide": f"{base}/dvr/{token}/guide.xml",
        }
    return render_template(
        "dvr_channels.html",
        title="DVR Channels",
        channels=channels,
        counts=counts,
        summaries=summaries,
        form=form,
        action_form=action_form,
        setup=setup,
    )


@bp.route("/dvr/channels/<int:channel_id>", methods=["GET", "POST"])
@login_required
@admin_required
def dvr_channel_edit(channel_id):
    """Show the editor of one channel and apply its actions.

    The editor has the rule fields. It also adds and removes the
    explicit movie and series picks by title."""

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
