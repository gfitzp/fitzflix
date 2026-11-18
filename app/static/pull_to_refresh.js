// Pull to refresh for the installed app. bootstrap.html loads this file
// for each visitor.

(function () {
	if (!window.matchMedia("(display-mode: standalone)").matches) return;
	if (!("ontouchstart" in window)) return;
	var THRESHOLD = 70;
	var startY = null;
	var pulled = 0;
	var indicator = document.createElement("div");
	indicator.style.cssText = "position:fixed;top:0;left:0;right:0;height:0;display:flex;align-items:flex-end;justify-content:center;overflow:hidden;z-index:2000;background:var(--bs-tertiary-bg);color:var(--bs-secondary-color);font-size:.85rem;padding-bottom:.5rem;";
	// The indicator grows to exactly the height of the navbar and
	// shares its background. Thus, the pull reads as the navbar that
	// makes room, not as a foreign banner
	var maxHeight = 56;
	document.addEventListener("DOMContentLoaded", function () {
		document.body.appendChild(indicator);
		var navbar = document.querySelector(".navbar.fixed-top");
		if (navbar && navbar.offsetHeight) maxHeight = navbar.offsetHeight;
	});
	document.addEventListener("touchstart", function (event) {
		startY = window.scrollY === 0 ? event.touches[0].clientY : null;
		pulled = 0;
	}, { passive: true });
	document.addEventListener("touchmove", function (event) {
		if (startY === null) return;
		pulled = event.touches[0].clientY - startY;
		if (pulled > 0) {
			indicator.style.height = Math.min(pulled / 2, maxHeight) + "px";
			indicator.textContent = pulled >= THRESHOLD ? "Release to refresh" : "Pull to refresh";
		}
	}, { passive: true });
	document.addEventListener("touchend", function () {
		if (startY !== null && pulled >= THRESHOLD) {
			indicator.replaceChildren();
			var spinner = document.createElement("div");
			spinner.className = "spinner-border spinner-border-sm";
			spinner.setAttribute("role", "status");
			var spinnerLabel = document.createElement("span");
			spinnerLabel.className = "visually-hidden";
			spinnerLabel.textContent = "Refreshing…";
			spinner.appendChild(spinnerLabel);
			indicator.appendChild(spinner);
			location.reload();
		} else {
			indicator.style.height = "0";
			indicator.textContent = "";
		}
		startY = null;
		pulled = 0;
	});
})();
