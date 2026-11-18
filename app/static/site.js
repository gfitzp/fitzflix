// The scripts that each page runs, for a visitor who is signed in or not.
// base.html loads this file.

// Fitzflix has no service worker. The earlier worker kept the
// first page of each shopping list for offline use, and it froze
// every page of the site in Safari when Safari suspended it.
// A browser can still hold that worker and its caches. Each page
// removes them. Service workers exist only in secure contexts.
// Thus, this does nothing over plain http

if ("serviceWorker" in navigator && window.isSecureContext) {
	navigator.serviceWorker.getRegistrations().then(function(registrations) {
		registrations.forEach(function(registration) {
			registration.unregister();
		});
	}).catch(function() {});
	if ("caches" in window) {
		caches.keys().then(function(keys) {
			keys.forEach(function(key) {
				caches.delete(key);
			});
		}).catch(function() {});
	}
}

// Navigation feedback, signed in or not. When a same-tab
// link or form commits to leave the page, the top hairline starts
// to grow. This listens on window, in the bubbling phase. Thus,
// it runs after every handler that can cancel the navigation.
// An example is a ladder tap, which posts in the background.
// Thus, a prevented default never lights the hairline. pageshow
// clears it when Safari restores
// the page from the back-forward cache, where the scripts do not
// run again
(function() {
	var bar = document.getElementById("nav-progress");
	if (!bar) return;
	function start() {
		bar.classList.remove("nav-progress-active");
		void bar.offsetWidth;  // restart the transition on a repeat tap
		bar.classList.add("nav-progress-active");
	}
	window.addEventListener("click", function(event) {
		if (event.defaultPrevented || event.button !== 0) return;
		if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
		var anchor = event.target.closest("a[href]");
		if (!anchor) return;
		if (anchor.target && anchor.target !== "_self") return;
		if (anchor.hasAttribute("download")) return;
		var href = anchor.getAttribute("href");
		if (!href || href.charAt(0) === "#") return;
		if (/^(javascript|mailto|tel):/i.test(href)) return;
		if (anchor.origin !== window.location.origin) return;
		if (anchor.href.split("#")[0] === window.location.href.split("#")[0]
				&& anchor.hash) return;
		start();
	});
	window.addEventListener("submit", function(event) {
		if (event.defaultPrevented) return;
		var form = event.target;
		if (form.target && form.target !== "_self") return;
		start();
	});
	window.addEventListener("pageshow", function() {
		bar.classList.remove("nav-progress-active");
	});
})();
