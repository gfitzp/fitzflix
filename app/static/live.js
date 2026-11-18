// The functions that the live parts of the pages share: the poll and the
// background post. base.html loads this file before each script that uses
// them.

// The shared poll of the live parts of the pages: the queue badge
// here, the Criterion card of the landing page, and the metrics of
// the System page. Each poll waits for the answer before it
// schedules the next one. Thus, slow answers cannot overlap or
// arrive out of order. A hidden tab stops the poll. The poll starts
// again when the tab shows. ms is a number, or a function that
// returns the next delay. onPayload gets the text or the JSON of a
// good answer, and returns false to stop the poll for good. With
// stopOnFailure, the end of the session (an expired session
// answers 401 or redirects to the login page) also stops it.
// Another failure is one bad tick: a 503 from CloudFront
// while the web process restarts, a network failure, or a
// request that the timeout below aborted. The next poll goes out
// on the normal cadence. after runs after each poll.

var POLL_TIMEOUT_MS = 20000;

// A request that does not settle in ms is aborted. Then the chain
// of the poll continues. Without this, a connection that Safari
// let hang held the poll until Safari dropped it

function fetchWithTimeout(url, fetchOptions, ms) {
	var options = Object.assign({}, fetchOptions || {});
	var controller = typeof AbortController === "function" ? new AbortController() : null;
	var timer = null;
	if (controller) {
		options.signal = controller.signal;
		timer = setTimeout(function() { controller.abort(); }, ms);
	}
	return fetch(url, options).finally(function() { clearTimeout(timer); });
}

function startPoll(url, ms, onPayload, options) {
	options = options || {};
	var timer = null;
	var stopped = false;

	function schedule() {
		clearTimeout(timer);
		if (document.hidden || stopped) return;
		timer = setTimeout(poll, typeof ms === "function" ? ms() : ms);
	}

	function poll() {
		fetchWithTimeout(url, options.fetchOptions, POLL_TIMEOUT_MS)
			.then(function(response) {
				var sessionEnded = response.status === 401 || response.redirected;
				if (sessionEnded && options.stopOnFailure) {
					stopped = true;
					return null;
				}
				if (!response.ok || response.redirected) {
					throw new Error(response.status);
				}
				return options.json ? response.json() : response.text();
			})
			.then(function(payload) {
				if (payload === null) return;
				var result = onPayload(payload);
				if (result === false) {
					stopped = true;
					return;
				}
				return result;
			})
			.catch(function(error) {
				// A body that is not JSON is not page data
				if (options.stopOnFailure && error instanceof SyntaxError) stopped = true;
			})
			.finally(function() {
				if (options.after) options.after();
				schedule();
			});
	}

	document.addEventListener("visibilitychange", function() {
		clearTimeout(timer);
		if (!document.hidden && !stopped) poll();
	});
	if (options.immediate) {
		poll();
	} else {
		schedule();
	}
}

// The shared background post of the live forms (the star row, the
// play buttons and the watchlist toggle). The post carries the
// X-Requested-With header of its kind, and the answer is the JSON
// state of the widget. With requireOk, an error status is a failure.

function postForState(url, kind, data, requireOk) {
	return fetch(url, {
		method: "POST",
		body: data,
		credentials: "same-origin",
		headers: {"X-Requested-With": kind}
	}).then(function(response) {
		if (requireOk && !response.ok) throw new Error(kind + " post failed");
		return response.json();
	});
}

// A live form has no second post in flight. Two quick taps
// could land out of order and paint the older state. A double tap
// on 1 star could write 2 rows. A tap during a post does nothing.
// onState paints the answer. On any failure, the form falls back
// to a normal submission. The tapped button is only on the
// FormData. form.submit() sends no submitter. Thus, the fallback
// first adds the name and the value of the button as a hidden
// input. requestSubmit is not used, because it would start
// the submit handler of the form again.

function backgroundPost(form, url, kind, data, onState, submitter) {
	if (form._postInFlight) return;
	form._postInFlight = true;
	postForState(url, kind, data, true).then(function(state) {
		form._postInFlight = false;
		onState(state);
	}).catch(function() {
		form._postInFlight = false;
		form.querySelectorAll("input[data-fallback-submitter]").forEach(
			function(input) { input.remove(); });
		if (submitter && submitter.name) {
			var input = document.createElement("input");
			input.type = "hidden";
			input.name = submitter.name;
			input.value = submitter.value;
			input.setAttribute("data-fallback-submitter", "");
			form.appendChild(input);
		}
		form.submit();
	});
}
