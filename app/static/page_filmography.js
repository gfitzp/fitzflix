// The Show more button of the biography of a person. filmography.html
// loads this file.

(function() {
	var bio = document.getElementById("person-biography");
	var toggle = document.getElementById("person-biography-toggle");
	if (bio.scrollHeight > bio.clientHeight + 1) {
		toggle.style.display = "inline";
	}
	toggle.addEventListener("click", function() {
		var clamped = bio.style.webkitLineClamp !== "unset";
		bio.style.webkitLineClamp = clamped ? "unset" : "4";
		toggle.textContent = clamped ? "Show less" : "Show more";
	});
})();
