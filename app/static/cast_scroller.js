// The arrows and the fades of the cast strip. The cast_scroller macro of
// _cast_scroller.html loads this file.

(function() {
	var scroller = document.querySelector(".cast-scroller");
	var fadeLeft = document.querySelector(".cast-scroll-fade-left");
	var fadeRight = document.querySelector(".cast-scroll-fade-right");

	// Each fade only means "more cast this way". The left one appears
	// after the strip has scrolled. The right one hides when the last
	// card is fully in view (or when the cast fits without a scroll)

	function updateFade() {
		fadeLeft.style.opacity = scroller.scrollLeft > 1 ? "1" : "0";
		var atEnd = scroller.scrollLeft + scroller.clientWidth >= scroller.scrollWidth - 1;
		fadeRight.style.opacity = atEnd ? "0" : "1";
	}
	scroller.addEventListener("scroll", updateFade, {passive: true});
	window.addEventListener("resize", updateFade);
	updateFade();

	document.querySelector(".cast-scroll-prev").addEventListener("click", function() {
		scroller.scrollBy({left: -320, behavior: "smooth"});
	});
	document.querySelector(".cast-scroll-next").addEventListener("click", function() {
		scroller.scrollBy({left: 320, behavior: "smooth"});
	});
})();
