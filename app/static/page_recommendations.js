// The script of the Recommendations page. recommendations.html loads this
// file and gives the address of the tile fetch on the script tag.

// The refill loop: an action on a suggestion (a rating, a
// watchlist add, a ✕) replaces only that tile with the next film that
// matches the criteria of the shelf. The live posts of the tile already
// repaint the widgets. tiles.js announces each success with a
// fitzflix:tile-state event. This listener answers only for the tiles
// inside a shelf. Undo gestures (the removal of a rating, an undo of ✕,
// the removal of a watchlist add) never replace the tile.
(function() {
	var TILE_URL = document.currentScript.getAttribute("data-tile-url");

	function shownIds() {
		var ids = [];
		document.querySelectorAll("[data-suggestion-cell] [data-state-movie]").forEach(function(el) {
			ids.push(el.getAttribute("data-state-movie"));
		});
		return ids;
	}

	// tiles.js paints the star row and the toggle of the new tile.

	function hydrate(cell) {
		var actions = cell.querySelector("[data-state-movie]");
		var id = actions && actions.getAttribute("data-state-movie");
		if (id) window.fitzflixHydrate([id], []);
	}

	// Invariant: a film shows at most 1 time on the page. The
	// server returns the best film that the request does not name.
	// Two requests that left together named the same films and got
	// the same film. Thus, the requests go one after an other: a
	// request leaves when the film of the request before it is in
	// the page.

	var replacements = Promise.resolve();

	function replaceCell(shelf, cell) {
		replacements = replacements.then(function() {
			return fetchReplacement(shelf, cell);
		});
		return replacements;
	}

	function fetchReplacement(shelf, cell) {
		// A second action on the same tile came after its replacement.
		if (!cell.isConnected) return null;
		var params = "criteria=" + encodeURIComponent(shelf.getAttribute("data-criteria"))
			+ "&exclude=" + shownIds().join(",");
		// A request with no answer must not hold the requests after
		// it. live.js has the fetch with a time limit.
		return fetchWithTimeout(TILE_URL + "?" + params, {credentials: "same-origin"}, POLL_TIMEOUT_MS)
			.then(function(response) {
				if (response.status === 204) return null;
				if (!response.ok) throw new Error("tile fetch failed");
				return response.text();
			})
			.then(function(html) {
				// The old tile always fades out. Either a replacement
				// fades into its place, or the criteria set is spent
				// and the slot closes (Glenn: no sudden
				// disappearances). Reduced-motion users get the instant
				// version of both.
				var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
				var fresh = null;
				if (html !== null) {
					var holder = document.createElement("div");
					holder.innerHTML = html.trim();
					fresh = holder.firstElementChild;
				}
				function settle() {
					if (!fresh) {
						cell.remove();
						return;
					}
					fresh.classList.add("tile-fade-in");
					cell.replaceWith(fresh);
					hydrate(fresh);
				}
				if (reduced) {
					settle();
					return null;
				}
				cell.classList.add("tile-fade-out");
				return new Promise(function(resolve) {
					window.setTimeout(function() {
						settle();
						resolve();
					}, 250);
				});
			})
			.catch(function() {});
	}

	document.addEventListener("fitzflix:tile-state", function(event) {
		var cell = event.target.closest("[data-suggestion-cell]");
		if (!cell) return;
		var state = event.detail || {};
		var acted = (typeof state.rating === "number" && state.rating > 0)
			|| !!state.flagged || !!state.on_watchlist;
		if (!acted) return;
		var shelf = cell.closest("[data-criteria]");
		if (!shelf) return;
		// A short pause. Thus, the repaint of the tap occurs before the
		// replacement.
		window.setTimeout(function() { replaceCell(shelf, cell); }, 700);
	});
})();
