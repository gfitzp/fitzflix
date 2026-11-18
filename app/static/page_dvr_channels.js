// The script of the DVR channels page. dvr_channels.html loads this file.

// These are the copy buttons for the Plex setup URLs
document.querySelectorAll(".dvr-copy").forEach(function (button) {
	button.addEventListener("click", function () {
		const input = button.parentElement.querySelector("input");
		navigator.clipboard.writeText(input.value).then(function () {
			button.textContent = "Copied";
			setTimeout(function () { button.textContent = "Copy"; }, 1500);
		}).catch(function () {
			input.select();
		});
	});
});
