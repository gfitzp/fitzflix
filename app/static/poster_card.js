// The card that shows when the pointer is on a poster.

// The poster popover (#45c): the anchors marked data-card-url
// fetch the card fragment of their film 1 time and show it in a
// Bootstrap popover. Only hover-capable pointers get the card.
// They preview on hover, with a grace period to move into the
// card, and a click follows the link. On touch, a poster tap
// follows the link of the poster to the page of the film. No
// card shows (revised by Glenn, 2026-10: on a phone the card
// often opened partly off the screen. Before that, 2026-08, a
// tap toggled the card and the title link of the card was the
// way to the page). The card is informational only. The ladder
// and the watchlist toggle are under the poster. Thus, an action
// on a tile drops its cached card (the watchlist badge inside
// went stale). 1 card at a time.

var cardOpen = null;    // the anchor whose card is showing
var cardWanted = null;  // the anchor a pending hover/fetch intends
var cardShowTimer = null;
var cardHideTimer = null;
var cardHover = window.matchMedia("(hover: hover) and (pointer: fine)").matches;

function invalidateCard(actions) {
	if (!actions || !actions.parentElement) return;
	var anchor = actions.parentElement.querySelector("[data-card-url]");
	if (!anchor || !anchor._cardPop) return;
	if (cardOpen === anchor) cardHide();
	anchor._cardPop.dispose();
	anchor._cardPop = null;
}

function cardHide() {
	cardWanted = null;
	clearTimeout(cardShowTimer);
	clearTimeout(cardHideTimer);
	if (cardOpen && cardOpen._cardPop) cardOpen._cardPop.hide();
	cardOpen = null;
}

function cardShow(anchor) {
	if (cardOpen === anchor) return;
	if (cardOpen) cardHide();
	cardWanted = anchor;
	if (anchor._cardPop) {
		anchor._cardPop.show();
		cardOpen = anchor;
		return;
	}
	if (anchor._cardLoading) return;
	anchor._cardLoading = true;
	fetch(anchor.getAttribute("data-card-url"), {credentials: "same-origin"})
		.then(function(response) {
			if (!response.ok) throw new Error("card fetch failed");
			return response.text();
		})
		.then(function(html) {
			anchor._cardLoading = false;
			var holder = document.createElement("div");
			holder.innerHTML = html;
			var content = holder.querySelector(".poster-card");
			if (!content) throw new Error("empty card");
			// The page-specific labels go with the tile as a
			// JSON array (data-card-reasons). They render as
			// wrapping badges inside the card. Long "liked by
			// people who liked…" reasons fold to multiple lines
			// inside the fixed width of the popover
			var reasons = anchor.getAttribute("data-card-reasons");
			var slot = content.querySelector("[data-card-reasons]");
			if (reasons && slot) {
				try {
					JSON.parse(reasons).forEach(function(label) {
						var badge = document.createElement("span");
						badge.className = "badge text-bg-light border fw-normal me-1 mb-1";
						badge.textContent = label;
						slot.appendChild(badge);
					});
				} catch (err) {}
			}
			// Each show reuses the same content node
			anchor._cardPop = new bootstrap.Popover(anchor, {
				content: content,
				html: true,
				sanitize: false,
				trigger: "manual",
				placement: "auto",
				container: "body",
				customClass: "poster-card-pop"
			});
			// Show only if the pointer still wants this card. The
			// hovers move on more quickly than the fetches return
			if (cardWanted === anchor) {
				anchor._cardPop.show();
				cardOpen = anchor;
			}
		})
		.catch(function() {
			anchor._cardLoading = false;
		});
}

if (cardHover) {
	document.addEventListener("mouseover", function(event) {
		var anchor = event.target.closest("[data-card-url]");
		if (anchor) {
			clearTimeout(cardHideTimer);
			if (cardOpen === anchor || cardWanted === anchor) return;
			clearTimeout(cardShowTimer);
			cardWanted = anchor;
			cardShowTimer = setTimeout(function() { cardShow(anchor); }, 350);
			return;
		}
		if (event.target.closest(".poster-card-pop")) {
			clearTimeout(cardHideTimer);
			return;
		}
		if (cardWanted && !cardOpen) {
			cardWanted = null;
			clearTimeout(cardShowTimer);
		}
		if (cardOpen) {
			clearTimeout(cardHideTimer);
			cardHideTimer = setTimeout(cardHide, 300);
		}
	});
}

document.addEventListener("click", function(event) {
	// A click or a tap on a poster follows its link. A click
	// inside the card acts on it. A click anywhere else closes it
	if (event.target.closest("[data-card-url]")) return;
	if (cardOpen && !event.target.closest(".poster-card-pop")) cardHide();
});

document.addEventListener("keydown", function(event) {
	if (event.key === "Escape") cardHide();
});

// A page that replaces its posters in place fires this event
// first. The open card then closes. Otherwise it would stay on
// the screen after its poster is gone. The detail names the area
// that changes. A card whose poster is outside it stays open.
document.addEventListener("fitzflix:card-hide", function(event) {
	var within = event.detail && event.detail.within;
	var anchor = cardOpen || cardWanted;
	if (within && anchor && !within.contains(anchor)) return;
	cardHide();
});
