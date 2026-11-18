// The script of the Queue page. queue.html loads this file. It gives the
// address of the rows and their ETag on the script tag.

(function() {
	// The ETag of the rows on the page. A poll sends it, and an
	// unchanged queue answers with no body. Then the rows and their
	// forms stay in place.

	var ROWS_URL = document.currentScript.getAttribute("data-rows-url");
	var rowsEtag = document.currentScript.getAttribute("data-rows-etag") || null;

	// The server rows carry the raw UTC stamp in each tooltip. Thus,
	// the tooltips get the local time from the stamp of the
	// flask-moment span (#547). moment.js loads in the scripts block,
	// after this content. Thus, the first pass waits for
	// DOMContentLoaded. A direct call threw a ReferenceError and
	// stopped this whole script (follow-up #547).

	function localizeFirstPaintTitles() {
		if (typeof moment === "undefined") return;
		document.querySelectorAll("#all-tasks abbr").forEach(function(abbr) {
			var span = abbr.querySelector("[data-timestamp]");
			if (span) {
				abbr.setAttribute("title", moment(span.getAttribute("data-timestamp")).format("LLL"));
			}
		});
	}

	function scheduleFirstPaintTitles() {
		if (document.readyState === "loading") {
			document.addEventListener("DOMContentLoaded", localizeFirstPaintTitles);
		} else {
			localizeFirstPaintTitles();
		}
	}

	scheduleFirstPaintTitles();

	// The rows change only at a poll. Thus, a minute timer writes the
	// relative times ("3 minutes ago") again from their stamps.

	setInterval(function() {
		if (document.hidden || typeof flask_moment_render === "undefined") return;
		document.querySelectorAll("#all-tasks [data-timestamp]").forEach(flask_moment_render);
	}, 60000);

	// The ETag goes in the header and in the address (#753). A
	// proxy can drop the header. An answer with the ETag that the
	// page has changes nothing, whatever its status is. Thus, the
	// forms of the rows stay in place.

	function refreshRows() {
		var headers = rowsEtag ? { "If-None-Match": rowsEtag } : {};
		var url = rowsEtag ? ROWS_URL + "?etag=" + encodeURIComponent(rowsEtag) : ROWS_URL;
		return fetchWithTimeout(url, { credentials: "same-origin", cache: "no-store", headers: headers }, POLL_TIMEOUT_MS)
			.then(function(response) {
				// A redirect is the sign-in page after the session
				// ended. It must not land in the table.
				if (response.status !== 200 || response.redirected) return;
				var etag = response.headers.get("ETag");
				if (etag && etag === rowsEtag) return;
				return response.text().then(function(html) {
					var tbody = document.getElementById("all-tasks");
					if (!tbody) return;
					tbody.innerHTML = html;
					tbody.querySelectorAll(".flask-moment").forEach(flask_moment_render);
					localizeFirstPaintTitles();
					rowsEtag = etag;
				});
			});
	}

	window.fitzflixQueueHooks = window.fitzflixQueueHooks || [];
	window.fitzflixQueueHooks.push(refreshRows);
})();
