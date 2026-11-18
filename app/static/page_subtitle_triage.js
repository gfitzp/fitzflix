// The script of the Subtitle triage page. subtitle_triage.html loads this
// file.

// Enlarge-in-place for the burned-in snapshots. A click on a
// thumbnail, or Enter on it, loads it into the shared modal.

document.addEventListener("click", function (event) {
	var snapshot = event.target.closest(".triage-snapshot");
	if (!snapshot) return;
	document.getElementById("triageSnapshotModalImage").src = snapshot.src;
	bootstrap.Modal.getOrCreateInstance(document.getElementById("triageSnapshotModal")).show();
});
document.addEventListener("keydown", function (event) {
	if (event.key !== "Enter") return;
	var snapshot = event.target.closest(".triage-snapshot");
	if (!snapshot) return;
	event.preventDefault();
	snapshot.click();
});
