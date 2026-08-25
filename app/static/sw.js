// Fitzflix service worker: keeps the shopping list and search usable in
// stores with bad reception. Static assets (posters, icons, CDN styles)
// are served from cache and revalidated in the background; pages are
// network-first so they're always fresh online, with the last good copy
// as the offline fallback.
//
// Only successful responses are cached. A failure kept cache-first is
// permanent: a 502 from the reverse proxy — gunicorn recycling mid-request
// is enough to produce one — was stored under a custom poster's URL and
// served in that image's place from then on, in the one browser that
// happened to load it during the restart, until its cache was emptied
// by hand (#206).

const CACHE = "fitzflix-v2";

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
							return key !== CACHE;
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

// An error page or a missing file must never displace the last good copy:
// the offline fallback would then serve the failure instead of the page,
// and a cached 404 for an asset would outlive the file that replaced it.
// Cross-origin CDN responses are opaque (status 0), so they're judged by
// type rather than by status

function isCacheable(response) {
	return response.ok || response.type === "opaque";
}

self.addEventListener("fetch", function (event) {
	var request = event.request;

	// Only GETs are cacheable; shopping-cart toggles and other POSTs
	// always go to the network

	if (request.method !== "GET") return;

	var url = new URL(request.url);

	// The manifest must stay network-first even though it lives under
	// /static/: served cache-first, an installed app would never see
	// changes to start_url, icons, or shortcuts

	var isManifest = url.pathname.endsWith("/site.webmanifest");

	// Static assets and CDN resources: answered from cache, refreshed
	// in the background

	if (
		!isManifest &&
		(url.pathname.startsWith("/static/") || url.origin !== location.origin)
	) {
		event.respondWith(
			caches.match(request).then(function (cached) {
				// Stale-while-revalidate: the cached copy answers straight
				// away, and the background fetch replaces it for next time.
				// These URLs are stable but their contents are not — a
				// custom poster and the site's own CSS are both replaced in
				// place — so an entry that never refreshed would serve the
				// old bytes until the cache version changed

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

	// Pages: network first (refreshing the cached copy), cached copy when
	// offline, and an explanation when the page was never visited online

	event.respondWith(
		fetch(request)
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
				return caches.match(request).then(function (cached) {
					return (
						cached ||
						new Response(
							"<!doctype html><title>Fitzflix — offline</title>" +
								"<div style=\"font-family: sans-serif; margin: 3em auto; max-width: 30em; text-align: center;\">" +
								"<h1>You're offline</h1>" +
								"<p>This page hasn't been viewed while online yet, so there's no saved copy to show. " +
								"Pages you visit while connected are kept for offline use.</p></div>",
							{ headers: { "Content-Type": "text/html" } }
						)
					);
				});
			})
	);
});
