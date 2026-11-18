// The script of the Watchlist page. watchlist.html loads this file.

// The service dropdown and the availability pills agree with
// each other. A chosen service checks On my services. A
// different pill puts the dropdown back to Any service. The
// route applies the same rule on the server. Thus, the page
// never submits a service under a different bucket
(function() {
	var select = document.querySelector("[data-watchlist-service]");
	if (!select) return;
	var pills = document.querySelectorAll('input[name="availability"]');
	select.addEventListener("change", function() {
		if (!select.value) return;
		pills.forEach(function(pill) {
			pill.checked = pill.value === "services";
		});
	});
	pills.forEach(function(pill) {
		pill.addEventListener("change", function() {
			if (pill.checked && pill.value !== "services") select.value = "";
		});
	});
})();
