// The script of the System page. system.html loads this file and gives
// the address of the health fragment on the script tag.

// Poll the health fragment every 5 seconds while the page is visible
// (startPoll in live.js). Each poll waits for the answer. Thus, a
// slow response never stacks overlapping requests. For example, a
// dead network mount can hold system_health() for its 10-second
// probe timeout. A redirect means that the session or the admin gate
// lapsed. It is a failure, and the container keeps its data.
(function() {
	var METRICS_URL = document.currentScript.getAttribute("data-metrics-url");
	var container = document.getElementById("system-health");
	var stale = document.getElementById("system-health-stale");
	var age = document.getElementById("system-health-age");
	var lastSuccess = Date.now();

	function markStaleness() {
		var seconds = Math.round((Date.now() - lastSuccess) / 1000);
		if (seconds >= 30) {
			container.style.opacity = "0.5";
			age.textContent = seconds < 120 ? seconds + " seconds" : Math.round(seconds / 60) + " minutes";
			stale.hidden = false;
		} else {
			container.style.opacity = "";
			stale.hidden = true;
		}
	}

	// The script files of base.html load after this block.
	document.addEventListener("DOMContentLoaded", function() {
		startPoll(METRICS_URL, 5000, function(fragment) {
			container.innerHTML = fragment;
			lastSuccess = Date.now();
		}, {fetchOptions: {cache: "no-store"}, after: markStaleness});
	});
})();
