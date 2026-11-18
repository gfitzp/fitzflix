// The buttons that start a film on the Apple TV. This file uses
// postForState of live.js.

// Plex remote playback: any [data-plex-play] form posts in the
// background and reports into its own [data-plex-play-status]
// line. This covers the button of the movie page and the poster
// popover card. Thus, the user can send a film to the Apple TV
// from anywhere that a poster shows. The handler is delegated
// because the popover cards are injected long after the page load
document.addEventListener("click", function(event) {
	var button = event.target.closest("[data-plex-play] button");
	if (!button) return;
	event.preventDefault();
	var form = button.closest("[data-plex-play]");
	var status = form.querySelector("[data-plex-play-status]");
	// Some forms cannot hold their status line. The secondary
	// form of the Play split-button is in a dropdown menu that
	// closes on click. Such a form names a shared line by id instead
	if (!status) status = document.getElementById(form.getAttribute("data-plex-play-status-ref") || "");
	var label = button.innerHTML;
	button.disabled = true;
	button.innerHTML = "Starting…";
	if (status) status.textContent = "";
	postForState(form.getAttribute("action"), "play", new FormData(form), false).then(function(state) {
		if (!status) return;
		status.classList.toggle("text-danger", !state.ok);
		status.classList.toggle("text-muted", state.ok);
		status.textContent = state.message;
	}).catch(function() {
		if (!status) return;
		status.classList.add("text-danger");
		status.textContent = "Fitzflix did not answer.";
	}).finally(function() {
		button.disabled = false;
		button.innerHTML = label;
	});
});
