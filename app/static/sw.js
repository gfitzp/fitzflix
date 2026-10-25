// This is the Fitzflix service worker. It keeps the shopping list and the
// search usable in stores with bad reception. The worker serves the static
// assets (posters, icons, CDN styles) from the cache and revalidates them
// in the background. The app shell pages are network-first. Thus, they are
// always fresh online. The last good copy is the offline fallback.
//
// The worker caches only successful responses. A failure that is kept
// cache-first is permanent. Example (#206): the reverse proxy returned a
// 502. A gunicorn recycle in the middle of a request is sufficient to cause
// one. The worker stored the 502 under the URL of a custom poster. From
// then on, it served the 502 in the place of that image. This occurred in
// the one browser that loaded the poster during the restart. It continued
// until the user emptied that cache by hand.
//
// The worker keeps only the pages in SHELL_PAGES (#334). It never keeps
// the profile page (it shows the API key), JSON answers, or the other
// signed-in pages. A logout deletes the kept pages.

const CACHE = "fitzflix-v3";
const PAGES = "fitzflix-pages-v3";

// The pages that the worker keeps for offline use. The search page has a
// different copy for each query. MAX_SEARCH_PAGES sets how many it keeps

const SHELL_PAGES = ["/", "/shopping-list/movie", "/shopping-list/tv"];
const SEARCH_PAGE = "/search";
const MAX_SEARCH_PAGES = 10;
const LOGOUT_PAGE = "/auth/logout";

self.addEventListener("install", function () {
	self.skipWaiting();
});

self.addEventListener("activate", function (event) {
	event.waitUntil(
		caches
			.keys()
			.then(function (keys) {
				return Promise.all(
					keys
						.filter(function (key) {
							return key !== CACHE && key !== PAGES;
						})
						.map(function (key) {
							return caches.delete(key);
						})
				);
			})
			.then(function () {
				return self.clients.claim();
			})
	);
});

// An error page or a missing file must never replace the last good copy.
// The offline fallback would then serve the failure instead of the page. A
// cached 404 for an asset would outlive the file that replaced it.
// Cross-origin CDN responses are opaque (status 0). Thus, the worker judges
// them by type, not by status

function isCacheable(response) {
	return response.ok || response.type === "opaque";
}

// A page is kept only if the server lets the browser store it. A redirect
// (for example, to the sign-in page) is not the page that the user asked
// for

function isKeepablePage(response) {
	var control = response.headers.get("Cache-Control") || "";
	return (
		response.ok &&
		!response.redirected &&
		control.indexOf("no-store") === -1 &&
		(response.headers.get("Content-Type") || "").indexOf("text/html") === 0
	);
}

function isShellPage(request, url) {
	if (request.mode !== "navigate" || url.origin !== location.origin) {
		return false;
	}
	if (url.pathname === SEARCH_PAGE) return true;
	return url.search === "" && SHELL_PAGES.indexOf(url.pathname) !== -1;
}

// Delete the oldest search pages when there are more than MAX_SEARCH_PAGES

function trimSearchPages(cache) {
	return cache.keys().then(function (keys) {
		var searches = keys.filter(function (key) {
			return new URL(key.url).pathname === SEARCH_PAGE;
		});
		return Promise.all(
			searches
				.slice(0, Math.max(0, searches.length - MAX_SEARCH_PAGES))
				.map(function (key) {
					return cache.delete(key);
				})
		);
	});
}

var OFFLINE_PAGE =
	"<!doctype html><title>Fitzflix — offline</title>" +
	"<div style=\"font-family: sans-serif; margin: 3em auto; max-width: 30em; text-align: center;\">" +
	"<h1>You're offline</h1>" +
	"<p>This page hasn't been viewed while online yet, so there's no saved copy to show. " +
	"The shopping lists, the home page, and recent searches are kept for offline use.</p></div>";

self.addEventListener("fetch", function (event) {
	var request = event.request;

	// Only GET requests are cacheable. Shopping-cart toggles and other POST
	// requests always go to the network

	if (request.method !== "GET") return;

	var url = new URL(request.url);

	// A logout deletes the kept pages. Thus, the next user of the browser
	// cannot see them offline

	if (url.origin === location.origin && url.pathname === LOGOUT_PAGE) {
		event.waitUntil(caches.delete(PAGES));
		return;
	}

	// The manifest must stay network-first, although it is under /static/.
	// If the worker served it cache-first, an installed app would never see
	// the changes to start_url, icons, or shortcuts

	var isManifest = url.pathname.endsWith("/site.webmanifest");

	// Static assets and CDN resources: the worker answers from the cache
	// and refreshes in the background

	if (
		!isManifest &&
		(url.pathname.startsWith("/static/") || url.origin !== location.origin)
	) {
		event.respondWith(
			caches.match(request).then(function (cached) {
				// Stale-while-revalidate: the cached copy answers
				// immediately. The background fetch replaces it for the
				// next time. These URLs are stable, but their contents are
				// not. A custom poster and the CSS of the site are both
				// replaced in place. Thus, an entry that never refreshed
				// would serve the old bytes until the cache version changed

				var revalidated = fetch(request)
					.then(function (response) {
						if (isCacheable(response)) {
							var copy = response.clone();
							caches.open(CACHE).then(function (cache) {
								cache.put(request, copy);
							});
						}
						return response;
					})
					.catch(function () {
						return cached || Response.error();
					});

				return cached || revalidated;
			})
		);
		return;
	}

	// The other requests (JSON, polls, other pages) always go to the
	// network. The worker does not keep them

	if (!isShellPage(request, url)) {
		if (request.mode !== "navigate") return;
		event.respondWith(
			fetch(request).catch(function () {
				return new Response(OFFLINE_PAGE, {
					headers: { "Content-Type": "text/html" },
				});
			})
		);
		return;
	}

	// Shell pages: network first (this refreshes the kept copy). The kept
	// copy answers when offline. An explanation answers when the user never
	// visited the page online

	event.respondWith(
		fetch(request)
			.then(function (response) {
				if (isCacheable(response) && isKeepablePage(response)) {
					var copy = response.clone();
					caches.open(PAGES).then(function (cache) {
						return cache.put(request, copy).then(function () {
							return trimSearchPages(cache);
						});
					});
				}
				return response;
			})
			.catch(function () {
				return caches.open(PAGES).then(function (cache) {
					return cache.match(request).then(function (cached) {
						return (
							cached ||
							new Response(OFFLINE_PAGE, {
								headers: { "Content-Type": "text/html" },
							})
						);
					});
				});
			})
	);
});
