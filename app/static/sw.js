// Fitzflix service worker: keeps the shopping list and search usable in
// stores with bad reception. Static assets (posters, icons, CDN styles)
// are served cache-first; pages are network-first so they're always fresh
// online, with the last good copy as the offline fallback.

const CACHE = "fitzflix-v1";

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

	// Static assets and CDN resources: cache first, fetch once

	if (
		!isManifest &&
		(url.pathname.startsWith("/static/") || url.origin !== location.origin)
	) {
		event.respondWith(
			caches.match(request).then(function (cached) {
				return (
					cached ||
					fetch(request).then(function (response) {
						var copy = response.clone();
						caches.open(CACHE).then(function (cache) {
							cache.put(request, copy);
						});
						return response;
					})
				);
			})
		);
		return;
	}

	// Pages: network first (refreshing the cached copy), cached copy when
	// offline, and an explanation when the page was never visited online

	event.respondWith(
		fetch(request)
			.then(function (response) {
				var copy = response.clone();
				caches.open(CACHE).then(function (cache) {
					cache.put(request, copy);
				});
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
