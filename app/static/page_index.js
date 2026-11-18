// The Criterion card of the landing page. index.html loads this file.

(function() {
	var holder = document.getElementById("criterion-now");
	if (!holder) return;
	var NOW_URL = holder.getAttribute("data-now-url");
	var POLL_MS = 60000;

	function filmOf(root) {
		var card = root.querySelector("[data-now-film]");
		return card ? card.getAttribute("data-now-film") : "";
	}

	// The end time of the film that shows, as epoch milliseconds.
	// The next fetch lands some seconds after it. Thus, the card
	// turns over within seconds, not at the next minute.
	function endsOf(root) {
		var card = root.querySelector("[data-now-ends]");
		var ends = card ? parseInt(card.getAttribute("data-now-ends"), 10) : NaN;
		return isNaN(ends) ? null : ends;
	}

	// The next poll lands some seconds after the end of the film. A
	// hidden tab stops the polling fully. It polls immediately when
	// the tab returns (startPoll in live.js).
	function nextDelay() {
		var delay = POLL_MS;
		var ends = endsOf(holder);
		if (ends !== null) {
			var untilEnd = ends - Date.now() + 3000;
			if (untilEnd > 1000 && untilEnd < delay) delay = untilEnd;
		}
		return delay;
	}

	function paint(html) {
		var fresh = document.createElement("div");
		fresh.innerHTML = html;
		// A replacement removes the Up next posters. Thus, an open
		// poster card of this card closes first. A card that is open
		// on another shelf stays.
		function closeCard() {
			document.dispatchEvent(new CustomEvent("fitzflix:card-hide", {detail: {within: holder}}));
		}
		if (filmOf(fresh) !== filmOf(holder)) {
			// A different film (or no film): replace the whole card.
			closeCard();
			holder.innerHTML = html;
			return;
		}
		var status = holder.querySelector("[data-now-status]");
		var latest = fresh.querySelector("[data-now-status]");
		if (status && latest) status.innerHTML = latest.innerHTML;
		// The preview holds images. A rewrite of unchanged markup
		// recreates them, and the posters flash. Thus, the repaint
		// happens only when the markup differs.
		var next = holder.querySelector("[data-now-next]");
		var freshNext = fresh.querySelector("[data-now-next]");
		if (next && freshNext && next.innerHTML !== freshNext.innerHTML) {
			closeCard();
			next.innerHTML = freshNext.innerHTML;
		}
	}

	// The script files of base.html load after this block.
	document.addEventListener("DOMContentLoaded", function() {
		startPoll(NOW_URL, nextDelay, paint, {
			fetchOptions: {credentials: "same-origin", cache: "no-store"}
		});
	});
})();
