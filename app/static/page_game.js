// The Share and Copy image buttons of Name That Frame. game.html loads
// this file.

(() => {
	const box = document.getElementById("brag-box");
	const url = box.dataset.bragUrl;
	const text = box.dataset.bragText;
	const status = document.getElementById("brag-status");
	const share = document.getElementById("brag-share");
	const copyImage = document.getElementById("brag-copy-image");
	const pngBlob = async () => {
		const response = await fetch(url);
		if (!response.ok) throw new Error("expired");
		return await response.blob();
	};
	if (navigator.canShare && navigator.canShare({files: [new File([""], "f.png", {type: "image/png"})]})) {
		share.hidden = false;
		share.addEventListener("click", async () => {
			try {
				const file = new File([await pngBlob()], "name-that-frame.png", {type: "image/png"});
				await navigator.share({files: [file], text: text});
			} catch (error) {
				if (error.name !== "AbortError") status.textContent = "Sharing did not work. Try Copy image.";
			}
		});
	}
	copyImage.addEventListener("click", async () => {
		try {
			if (!navigator.clipboard || !window.ClipboardItem) throw new Error("unsupported");
			// The entry is a Promise. Safari requires that the clipboard
			// write starts inside the click gesture.
			await navigator.clipboard.write([new ClipboardItem({"image/png": pngBlob()})]);
			status.textContent = "Image copied.";
		} catch (error) {
			status.textContent = "Fitzflix cannot copy the image here. Press and hold it to copy it, or use Share.";
		}
	});
})();
