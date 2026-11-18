// The search box of the navigation bar. base.html loads this file for a
// person who is signed in.

// The type-ahead for the global search box: a debounced fetch
// against /search.json, rendered as a dropdown. The arrow keys move
// through the suggestions. Enter opens the highlighted suggestion.
// If no suggestion is highlighted, Enter submits the form for the
// full results page

(function() {
	// base.html gives the address of the search on the script tag.

	var SEARCH_URL = document.currentScript.getAttribute("data-search-url");
	var inputs = document.querySelectorAll(".global-search-input");
	if (!inputs.length) return;

	inputs.forEach(function(input) {
		var menu = input.nextElementSibling;
		var timer = null;
		var activeIndex = -1;

		// Invariant: the menu shows the suggestions for the text
		// that is in the box now. wanted is that text while
		// the menu can open for it. An answer for an other text is
		// old: the person typed more, cleared the box or closed the
		// menu while the request was on its way.

		var wanted = null;

		function items() {
			return menu.querySelectorAll(".dropdown-item");
		}

		function hide() {
			wanted = null;
			menu.classList.remove("show");
			menu.innerHTML = "";
			activeIndex = -1;
		}

		function setActive(index) {
			var list = items();
			if (!list.length) return;
			activeIndex = (index + list.length) % list.length;
			list.forEach(function(item, i) {
				item.classList.toggle("active", i === activeIndex);
			});
			list[activeIndex].scrollIntoView({ block: "nearest" });
		}

		function suggest(q) {
			return fetch(SEARCH_URL + "?q=" + encodeURIComponent(q))
				.then(function(r) { return r.json(); })
				.then(function(data) {
					if (wanted !== q) return;
					menu.innerHTML = "";
					activeIndex = -1;
					(data.results || []).forEach(function(item) {
						var a = document.createElement("a");
						a.className = "dropdown-item small text-truncate";
						a.href = item.url;
						a.textContent = item.title + " — " + item.detail;
						menu.appendChild(a);
					});
					if (menu.children.length) {
						menu.classList.add("show");
					} else {
						hide();
					}
				})
				.catch(function() {
					if (wanted === q) hide();
				});
		}

		function typed() {
			clearTimeout(timer);
			var q = input.value.trim();
			if (q.length < 2) {
				hide();
				return;
			}
			wanted = q;
			timer = setTimeout(function() { suggest(q); }, 250);
		}

		input.addEventListener("input", typed);

		input.addEventListener("keydown", function(e) {
			if (e.key === "Escape") {
				hide();
				return;
			}
			if (!menu.classList.contains("show")) return;
			if (e.key === "ArrowDown") {
				e.preventDefault();
				setActive(activeIndex + 1);
			} else if (e.key === "ArrowUp") {
				e.preventDefault();
				setActive(activeIndex - 1);
			} else if (e.key === "Enter" && activeIndex >= 0) {
				e.preventDefault();
				window.location = items()[activeIndex].href;
			}
		});

		document.addEventListener("click", function(e) {
			if (e.target !== input && !menu.contains(e.target)) hide();
		});
	});
})();
