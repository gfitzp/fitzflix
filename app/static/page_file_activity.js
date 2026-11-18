// The script of the File Activity page. file_activity.html loads this
// file on page 1 and gives the address of the card list on the script tag.

window.fileActivityCardsUrl = document.currentScript.getAttribute("data-cards-url");

(function() {
	// The File Activity dashboard (window.fileActivityCardsUrl is
	// set on page 1) fetches its card list again at each poll. The
	// server answers with no body when no card changed (the ETag). A new
	// list keeps each card on the page whose data-card-version is
	// the same. Thus, an open poster popover on a card stays
	// open. The other cards come in, in the order of the list. A
	// card that left the list goes.

	var cardsEtag = null;

	// The list changes only at a poll. Thus, a minute timer writes
	// the relative times ("3 minutes ago") again from their stamps.

	setInterval(function() {
		if (document.hidden || typeof flask_moment_render === "undefined") return;
		document.querySelectorAll("#file-activity-cards [data-timestamp]").forEach(flask_moment_render);
	}, 60000);

	function placeCards(holder, html) {
		var wrapper = document.createElement("div");
		wrapper.innerHTML = html;
		var current = {};
		holder.querySelectorAll(":scope > [data-card-key]").forEach(function(card) {
			current[card.getAttribute("data-card-key")] = card;
		});
		var position = null;
		Array.prototype.slice.call(wrapper.children).forEach(function(card) {
			var key = card.getAttribute("data-card-key");
			var old = current[key];
			delete current[key];
			var keep = old && old.getAttribute("data-card-version") === card.getAttribute("data-card-version");
			if (old && !keep) old.remove();
			var node = keep ? old : card;
			var next = position ? position.nextElementSibling : holder.firstElementChild;
			if (node !== next) holder.insertBefore(node, next);
			if (!keep) node.querySelectorAll(".flask-moment").forEach(flask_moment_render);
			position = node;
		});
		Object.keys(current).forEach(function(key) { current[key].remove(); });
	}

	function refreshCards() {
		if (!window.fileActivityCardsUrl) return null;
		// The ETag goes in the header and in the address (#753). A
		// proxy can drop the header.
		var headers = cardsEtag ? { "If-None-Match": cardsEtag } : {};
		var url = window.fileActivityCardsUrl;
		if (cardsEtag) url += "?etag=" + encodeURIComponent(cardsEtag);
		return fetchWithTimeout(url, { credentials: "same-origin", cache: "no-store", headers: headers }, POLL_TIMEOUT_MS)
			.then(function(response) {
				if (response.status !== 200 || response.redirected) return;
				var etag = response.headers.get("ETag");
				if (etag && etag === cardsEtag) return;
				return response.text().then(function(html) {
					var holder = document.getElementById("file-activity-cards");
					if (holder) placeCards(holder, html);
					cardsEtag = etag;
				});
			});
	}

	window.fitzflixQueueHooks = window.fitzflixQueueHooks || [];
	window.fitzflixQueueHooks.push(refreshCards);
})();
