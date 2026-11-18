// The script of the Missing files page. missing_files.html loads this
// file.

// The header checkbox selects or clears every archived row.
document.getElementById("missing-select-all").addEventListener("change", function (event) {
	document.querySelectorAll("#missing-restore-form input[name=file_id]").forEach(function (box) {
		box.checked = event.target.checked;
	});
});
