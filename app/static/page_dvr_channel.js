// The script of the page of 1 DVR channel. dvr_channel.html loads this
// file and gives the address of the title search on the script tag.

const TITLE_URL = document.currentScript.getAttribute("data-title-url");

// Lookahead on the pick fields. The suggestions are the canonical strings
// that the server-side resolvers parse. Thus, a picked title always resolves.
// The debounce and dropdown interaction is the same as in the global
// search in search.js.
document.querySelectorAll(".dvr-title-lookahead").forEach(function (input) {
	const menu = input.parentElement.querySelector(".dropdown-menu");
	let timer = null;
	let active = -1;

	// The text that the menu can open for, as in search.js. An
	// answer for an other text is old.
	let wanted = null;

	function hide() {
		wanted = null;
		menu.classList.remove("show");
		menu.innerHTML = "";
		active = -1;
	}

	function render(results) {
		menu.innerHTML = "";
		results.forEach(function (title) {
			const item = document.createElement("button");
			item.type = "button";
			item.className = "dropdown-item text-truncate";
			item.textContent = title;
			// mousedown, not click. mousedown occurs before the blur of the input.
			item.addEventListener("mousedown", function (event) {
				event.preventDefault();
				input.value = title;
				hide();
			});
			menu.appendChild(item);
		});
		menu.classList.toggle("show", results.length > 0);
		active = -1;
	}

	function typed() {
		clearTimeout(timer);
		const q = input.value.trim();
		if (q.length < 2) {
			hide();
			return;
		}
		wanted = q;
		timer = setTimeout(function () { lookup(q); }, 250);
	}

	input.addEventListener("input", typed);

	function lookup(q) {
		const url = TITLE_URL + "?kind=" +
			input.dataset.kind + "&q=" + encodeURIComponent(q);
		return fetch(url)
			.then(function (response) { return response.ok ? response.json() : { results: [] }; })
			.then(function (data) {
				if (wanted === q) render(data.results || []);
			})
			.catch(function () {
				if (wanted === q) hide();
			});
	}

	function pressed(event) {
		// Escape also stops an answer that is on its way.
		if (event.key === "Escape") {
			hide();
			return;
		}
		const items = menu.querySelectorAll(".dropdown-item");
		if (!items.length) return;
		if (event.key === "ArrowDown" || event.key === "ArrowUp") {
			event.preventDefault();
			active = (active + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
			items.forEach(function (el, i) { el.classList.toggle("active", i === active); });
		} else if (event.key === "Enter" && active >= 0) {
			// Fill the input. A second Enter (or the button) submits.
			event.preventDefault();
			input.value = items[active].textContent;
			hide();
		}
	}

	input.addEventListener("keydown", pressed);

	input.addEventListener("blur", function () { setTimeout(hide, 150); });
});
