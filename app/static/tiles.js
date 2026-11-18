// The poster tiles: the star row, the watchlist toggle, and the state
// that each tile gets after the page load. This file uses backgroundPost
// of live.js, and cardHide and invalidateCard of poster_card.js.

// The one-tap star row: the forms marked data-ladder-live post
// in the background, and the row repaints in place with no
// reload. On any failure, this falls back to a normal form
// submission. The gallery hydration below shares the painting.
// The hydration fills the blank tile ladders from 1 batched
// state fetch.

function paintLadder(row, state) {
	var rating = state.rating === null ? 0 : state.rating;
	var estimated = (state.rating === null && state.estimated)
		? state.estimated : 0;
	row.title = estimated
		? "Estimated " + parseFloat(estimated.toFixed(2))
			+ " for you — tap a star to rate it yourself" : "";
	// The stars are reversed in the DOM (see the ladder
	// macro). Thus, this paints by the own value of each
	// button. A half-star rating and a fractional estimate
	// both part-fill their last star through a gradient stop
	row.querySelectorAll(".star").forEach(function(star) {
		var value = parseInt(star.value, 10);
		var fillPct = Math.round(
			Math.max(0, Math.min(1, rating - value + 1)) * 100);
		var estPct = Math.round(
			Math.max(0, Math.min(1, estimated - value + 1)) * 100);
		star.classList.toggle("filled", fillPct > 0);
		star.classList.toggle("fill-partial", fillPct > 0 && fillPct < 100);
		star.classList.toggle("estimated", estPct > 0);
		star.classList.toggle("est-partial", estPct > 0 && estPct < 100);
		// The fraction goes with the star as a quantized
		// fill-N class (5% steps, fixed CSS rules). It is never
		// a custom property. iOS Safari resolves a custom
		// property incorrectly inside a pseudo-element (the
		// star-widget WebKit lesson)
		Array.prototype.slice.call(star.classList).forEach(function (cls) {
			if (/^fill-\d+$/.test(cls)) star.classList.remove(cls);
		});
		var frac = (fillPct > 0 && fillPct < 100) ? fillPct
			: ((estPct > 0 && estPct < 100) ? estPct : 0);
		if (frac) {
			star.classList.add("fill-" + Math.max(5, Math.min(95,
				Math.round(frac / 5) * 5)));
		}
		star.title = (state.rating !== null && value === state.rating)
			? "Tap again to remove your rating"
			: star.getAttribute("aria-label");
	});
	var x = row.querySelector(".x-btn");
	if (x) {
		x.classList.toggle("active", !!state.flagged);
		// The user cannot dismiss a seen film. The hydration
		// hides the ✕ when a diary row exists (the client-side
		// mirror of allow_not_interested). The live posts carry
		// the flag too
		if (typeof state.has_review !== "undefined") {
			x.classList.toggle("d-none", !!state.has_review);
		}
		x.title = state.flagged
			? "Undo not interested"
			: "Not interested — never saw it";
	}
}

// The own Remove of the watchlist page clears its tile in place
// and settles the filter counts. It uses the bucket that the
// cell rendered with. A rating or a ✕ on the tile also
// removes the film from the watchlist. A cell counts
// down 1 time only, whichever post removes it first

function removeWatchlistCell(cell) {
	if (!cell || cell._watchlistRemoved) return;
	cell._watchlistRemoved = true;
	["all", cell.getAttribute("data-bucket")].forEach(function(name) {
		var count = name && document.querySelector('[data-watchlist-count="' + name + '"]');
		if (count) count.textContent = Math.max(0, parseInt(count.textContent, 10) - 1);
	});
	// The per-service counts of the dropdown settle the same
	// way, from the services of the cell
	if (cell.getAttribute("data-bucket") === "services") {
		(cell.getAttribute("data-services") || "").split(",").forEach(function(id) {
			var option = id && document.querySelector('[data-watchlist-service] option[value="' + id + '"]');
			if (!option) return;
			var left = Math.max(0, parseInt(option.getAttribute("data-watchlist-service-count"), 10) - 1);
			option.setAttribute("data-watchlist-service-count", left);
			option.textContent = option.textContent.replace(/\(\d+\)$/, "(" + left + ")");
		});
	}
	cell.remove();

	// The film left the watchlist. When the grid is empty, the
	// page shows the empty state that applies. The filter
	// state applies while other films stay on the watchlist
	var filtered = document.querySelector('[data-watchlist-empty="filter"]');
	var left = 0;
	if (filtered) {
		left = Math.max(0, parseInt(filtered.getAttribute("data-watchlist-total"), 10) - 1);
		filtered.setAttribute("data-watchlist-total", left);
		var link = filtered.querySelector("[data-watchlist-total-link]");
		if (link) {
			link.textContent = "Show all " + left + " watchlisted film" + (left !== 1 ? "s" : "");
		}
	}
	if (!document.querySelector(".poster-cell")) {
		var empty = document.querySelector('[data-watchlist-empty="all"]');
		if (filtered && left > 0) {
			filtered.hidden = false;
		} else if (empty) {
			if (filtered) filtered.hidden = true;
			empty.hidden = false;
		}
	}
}

// The answer of a live tile post. The watchlist toggle that shares
// the scope of the form (a poster tile, or the data-watchlist-scope
// of the Criterion 24/7 card) follows the state, because a rating
// or a ✕ clears the watchlist entry of the film on the server. The
// cached popover card of the tile is stale. Pages that react to a
// changed tile verdict listen for the fitzflix:tile-state event.
// The Recommendations page swaps the acted-on tile for a new
// suggestion. On the watchlist page, a film that left the
// watchlist leaves the grid

function afterTileState(form, state) {
	var scope = form.closest(".poster-actions, [data-watchlist-scope]");
	if (scope && typeof state.on_watchlist !== "undefined") {
		syncWatchlistForm(
			scope.querySelector("[data-card-watchlist]"),
			state.on_watchlist
		);
	}
	invalidateCard(scope);
	form.dispatchEvent(new CustomEvent("fitzflix:tile-state",
		{bubbles: true, detail: state}));
	if (state.on_watchlist === false) {
		var cell = form.closest(".poster-cell");
		if (cell && cell.querySelector("[data-watchlist-remove-cell]")) {
			removeWatchlistCell(cell);
		}
	}
}

document.addEventListener("click", function(event) {
	var button = event.target.closest("[data-ladder-live] button[name=quick_rating]");
	if (!button) return;
	var form = button.closest("form");
	if (!form) return;
	event.preventDefault();
	var data = new FormData(form);
	data.set("quick_rating", button.value);
	backgroundPost(form, form.getAttribute("action") || window.location.href,
		"ladder", data, function(state) {
			paintLadder(button.closest(".star-row"), state);
			afterTileState(form, state);
		}, {name: "quick_rating", value: button.value});
});

// The 2 faces of a watchlist toggle: Add and Remove. Only 1 shows.

function syncWatchlistForm(form, onWatchlist) {
	if (!form) return;
	var add = form.querySelector("[name=add_watchlist_submit]");
	var remove = form.querySelector("[name=remove_watchlist_submit]");
	if (add) add.classList.toggle("d-none", !!onWatchlist);
	if (remove) remove.classList.toggle("d-none", !onWatchlist);
}

// The watchlist toggle of the tile posts in the background and
// swaps its 2 faces on the JSON state. On any failure, this falls
// back to a normal form submission

document.addEventListener("submit", function(event) {
	var form = event.target.closest("form[data-card-watchlist]");
	if (!form) return;
	event.preventDefault();
	var data = new FormData(form);
	var submitter = null;
	if (event.submitter && event.submitter.name) {
		submitter = {name: event.submitter.name, value: event.submitter.value || "1"};
		data.set(submitter.name, submitter.value);
	}
	backgroundPost(form, form.getAttribute("action"), "card", data, function(state) {
		syncWatchlistForm(form, state.on_watchlist);
		afterTileState(form, state);
	}, submitter);
});

// The batched state of the tiles. paintTileState paints the star
// row and the watchlist toggle of 1 tile from it.

// base.html gives the address of the state fetch on the script tag.

var STATES_URL = document.currentScript.getAttribute("data-states-url");

function fetchStates(movieIds, tmdbIds) {
	var params = [];
	if (movieIds.length) params.push("movie_ids=" + movieIds.join(","));
	if (tmdbIds.length) params.push("tmdb_ids=" + tmdbIds.join(","));
	return fetch(STATES_URL + "?" + params.join("&"), {credentials: "same-origin"})
		.then(function(response) {
			if (!response.ok) throw new Error("state fetch failed");
			return response.json();
		});
}

function paintTileState(tile, state) {
	var row = tile.querySelector(".star-row");
	if (row) paintLadder(row, state);
	syncWatchlistForm(tile.querySelector("[data-card-watchlist]"), state.on_watchlist);
}

// A page that adds tiles after the load (the Recommendations
// refill) asks for their state here. Each tile on the page with
// one of the ids paints.

window.fitzflixHydrate = function(movieIds, tmdbIds) {
	return fetchStates(movieIds, tmdbIds)
		.then(function(payload) {
			[
				["data-state-movie", movieIds, payload.movies],
				["data-state-tmdb", tmdbIds, payload.tmdb]
			].forEach(function(kind) {
				kind[1].forEach(function(id) {
					var state = kind[2][id];
					if (!state) return;
					document.querySelectorAll("[" + kind[0] + '="' + id + '"]')
						.forEach(function(tile) { paintTileState(tile, state); });
				});
			});
		})
		.catch(function() {});
};

// Gallery hydration: the tiles
// render their star row blank and their toggle Add-first.
// Batched /movie_states fetches paint the real verdicts,
// estimates, and watchlist faces. Duplicate tiles for the same
// film (the editions of a movie on the library wall) all paint
// from the 1 entry. The ids go in chunks below the 300-id cap
// of the endpoint. The unpaginated watchlist can carry hundreds
// of films. 1 oversized request would answer 400 and leave
// every ladder blank. Each chunk paints its own tiles when it
// arrives. Thus, 1 failed chunk costs only its own tiles. The
// posts still answer with the authoritative state.

(function() {
	var tiles = document.querySelectorAll("[data-state-movie], [data-state-tmdb]");
	// Poster folds: every popover-armed movie tile also
	// hydrates its corner fold from the same batched fetch. This
	// parses the id of the film off its own card URL. Thus, no
	// template carries fold-specific markup. At most 1 fold
	// paints, in the top-right corner. Red (leaving) outranks
	// green (newly added). The leaving shelf and page suppress
	// only the red fold with data-no-leaving-fold on a wrapper.
	// Everything there is leaving by definition, so the green
	// fold gets the corner. The newly-added shelf and page
	// suppress only the green fold with data-no-new-fold, for
	// the mirror reason. tv_card tiles never match.
	var cards = [];
	document.querySelectorAll("[data-card-url]").forEach(function(el) {
		var cardUrl = el.getAttribute("data-card-url");
		if (cardUrl.indexOf("movie_card") === -1) return;
		var params = new URLSearchParams(cardUrl.split("?")[1] || "");
		var id = params.get("movie_id");
		var attr = "data-state-movie";
		if (!id) {
			id = params.get("tmdb_id");
			attr = "data-state-tmdb";
		}
		if (id) cards.push({
			attr: attr,
			id: id,
			el: el,
			noLeaving: !!el.closest("[data-no-leaving-fold]"),
			noNew: !!el.closest("[data-no-new-fold]")
		});
	});
	if (!tiles.length && !cards.length) return;
	var STATE_CHUNK = 300;
	var ids = [];
	function collect(attr, id) {
		if (!id) return;
		var known = ids.some(function(entry) {
			return entry.attr === attr && entry.id === id;
		});
		if (!known) ids.push({attr: attr, id: id});
	}
	tiles.forEach(function(tile) {
		["data-state-movie", "data-state-tmdb"].forEach(function(attr) {
			collect(attr, tile.getAttribute(attr));
		});
	});
	cards.forEach(function(card) {
		collect(card.attr, card.id);
	});
	function fold(kind, label, glyph) {
		var corner = document.createElement("div");
		corner.className = "poster-fold poster-fold-" + kind;
		corner.title = label;
		corner.setAttribute("role", "img");
		corner.setAttribute("aria-label", label);
		corner.innerHTML = '<svg viewBox="0 0 16 16" fill="none"'
			+ ' stroke="currentColor" stroke-width="2.4"'
			+ ' stroke-linecap="round">' + glyph + '</svg>';
		return corner;
	}
	function paintFolds(el, state, noLeaving, noNew) {
		var leaving = noLeaving ? null : state.fold_leaving;
		var added = noNew ? null : state.fold_new;
		if (!added && !leaving) return;
		if (el.querySelector(".poster-folds")) return;
		el.classList.add("poster-fold-host");
		var box = document.createElement("div");
		box.className = "poster-folds";
		if (leaving) {
			box.appendChild(fold("leaving",
				"Leaving the Criterion Channel " + leaving,
				'<circle cx="8" cy="8" r="5.4"/><path d="M8 5.2v2.8l2 1.3"/>'));
		} else {
			box.appendChild(fold("new", added,
				'<path d="M8 3.2v9.6M3.2 8h9.6"/>'));
		}
		el.insertBefore(box, el.firstChild);
	}
	function paintFrom(payload) {
		tiles.forEach(function(tile) {
			var state = tile.hasAttribute("data-state-movie")
				? payload.movies[tile.getAttribute("data-state-movie")]
				: payload.tmdb[tile.getAttribute("data-state-tmdb")];
			if (state) paintTileState(tile, state);
		});
		cards.forEach(function(card) {
			var state = card.attr === "data-state-movie"
				? payload.movies[card.id]
				: payload.tmdb[card.id];
			if (state) paintFolds(card.el, state, card.noLeaving, card.noNew);
		});
	}
	// The in-flight shimmer: every tile that a request still owes
	// an answer pulses (see state-loading in the stylesheet). The
	// pulse stops when its state paints. A tile that a follow-up
	// pass asks for again pulses until its state arrives
	var byMovie = {};
	var byTmdb = {};
	tiles.forEach(function(tile) {
		var id = tile.getAttribute("data-state-movie");
		if (id) (byMovie[id] = byMovie[id] || []).push(tile);
		id = tile.getAttribute("data-state-tmdb");
		if (id) (byTmdb[id] = byTmdb[id] || []).push(tile);
	});
	function setLoading(movieIds, tmdbIds, on) {
		movieIds.forEach(function(id) {
			(byMovie[id] || []).forEach(function(tile) {
				tile.classList.toggle("state-loading", on);
			});
		});
		tmdbIds.forEach(function(id) {
			(byTmdb[id] || []).forEach(function(tile) {
				tile.classList.toggle("state-loading", on);
			});
		});
	}
	// A career page can hold more record-less films than the
	// TMDB fetch cap of 1 request. The answer paints at
	// once and names the ids that still need TMDB in "pending".
	// The next pass asks for those ids only. The passes stop
	// when none are left, or when a pass warms nothing (TMDB is
	// down). Thus, every page fills with no reload
	function hydrate(movieIds, tmdbIds) {
		setLoading(movieIds, tmdbIds, true);
		fetchStates(movieIds, tmdbIds)
			.then(function(payload) {
				paintFrom(payload);
				setLoading(movieIds, tmdbIds, false);
				var pending = (payload.pending || []).map(String);
				if (pending.length && pending.length < tmdbIds.length) {
					hydrate([], pending);
				}
			})
			.catch(function() {
				setLoading(movieIds, tmdbIds, false);
			});
	}
	for (var start = 0; start < ids.length; start += STATE_CHUNK) {
		var movieIds = [];
		var tmdbIds = [];
		ids.slice(start, start + STATE_CHUNK).forEach(function(entry) {
			(entry.attr === "data-state-movie" ? movieIds : tmdbIds)
				.push(entry.id);
		});
		hydrate(movieIds, tmdbIds);
	}
})();
