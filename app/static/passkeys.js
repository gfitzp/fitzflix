// Passkeys (#669). A page gives the browser the options of a ceremony,
// the authenticator answers, and the page posts the answer. Each post
// carries the csrf token of the page in a header. The buttons appear
// only when the browser supports WebAuthn. The addresses of the routes
// are data attributes of the buttons. Thus, this file is static.

(function() {
	"use strict";

	function supported() {
		return !!(window.PublicKeyCredential && navigator.credentials && navigator.credentials.create);
	}

	function csrfToken() {
		var input = document.querySelector("input[name=csrf_token]");
		return input ? input.value : "";
	}

	// The server sends and takes base64url text. The browser API uses
	// ArrayBuffer values. These 2 functions convert between them.

	function toBytes(text) {
		var base64 = text.replace(/-/g, "+").replace(/_/g, "/");
		var pad = base64.length % 4;
		if (pad) base64 += "====".slice(pad);
		var binary = atob(base64);
		var bytes = new Uint8Array(binary.length);
		for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
		return bytes.buffer;
	}

	function toText(buffer) {
		var bytes = new Uint8Array(buffer);
		var binary = "";
		for (var i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
		return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
	}

	function post(url, body) {
		return fetch(url, {
			method: "POST",
			credentials: "same-origin",
			headers: {"Content-Type": "application/json", "X-CSRFToken": csrfToken()},
			body: JSON.stringify(body || {})
		}).then(function(response) {
			return response.json().then(function(data) {
				if (!response.ok) throw new Error(data.error || "The request failed.");
				return data;
			});
		});
	}

	function creationOptions(options) {
		options.challenge = toBytes(options.challenge);
		options.user.id = toBytes(options.user.id);
		(options.excludeCredentials || []).forEach(function(item) { item.id = toBytes(item.id); });
		return options;
	}

	function requestOptions(options) {
		options.challenge = toBytes(options.challenge);
		(options.allowCredentials || []).forEach(function(item) { item.id = toBytes(item.id); });
		return options;
	}

	// The JSON form of a PublicKeyCredential, as the server parser
	// expects it. A registration has an attestation object. A sign-in
	// has the authenticator data and the signature.

	function credentialJSON(credential) {
		var response = credential.response;
		var out = {
			id: credential.id,
			rawId: toText(credential.rawId),
			type: credential.type,
			clientExtensionResults: credential.getClientExtensionResults ? credential.getClientExtensionResults() : {},
			response: {clientDataJSON: toText(response.clientDataJSON)}
		};
		if (credential.authenticatorAttachment) out.authenticatorAttachment = credential.authenticatorAttachment;
		if (response.attestationObject) {
			out.response.attestationObject = toText(response.attestationObject);
			out.response.transports = response.getTransports ? response.getTransports() : [];
		} else {
			out.response.authenticatorData = toText(response.authenticatorData);
			out.response.signature = toText(response.signature);
			out.response.userHandle = response.userHandle ? toText(response.userHandle) : null;
		}
		return out;
	}

	function message(error) {
		if (error && error.name === "NotAllowedError") return "The passkey request was cancelled.";
		if (error && error.name === "InvalidStateError") return "This device already has a passkey for this account.";
		return (error && error.message) || "The passkey request failed.";
	}

	function statusOf(element) {
		var scope = element.closest("[data-passkey-add], [data-passkey-login], form") || document;
		return scope.querySelector("[data-passkey-status]");
	}

	function run(button, steps) {
		var status = statusOf(button);
		if (status) status.textContent = "";
		button.disabled = true;
		steps().catch(function(error) {
			if (status) status.textContent = message(error);
			button.disabled = false;
		});
	}

	// Add a passkey to the signed-in account (Profile page).

	function register(button) {
		var row = button.closest("[data-passkey-add]");
		var nameInput = row ? row.querySelector("[data-passkey-name]") : null;
		run(button, function() {
			return post(button.getAttribute("data-options-url")).then(function(options) {
				return navigator.credentials.create({publicKey: creationOptions(options)});
			}).then(function(credential) {
				return post(button.getAttribute("data-register-url"), {
					credential: credentialJSON(credential),
					name: nameInput ? nameInput.value : ""
				});
			}).then(function(data) {
				window.location.assign(data.redirect);
			});
		});
	}

	// Sign in with any passkey of this site (Sign In page).

	function signIn(button) {
		var remember = document.getElementById("remember_me");
		run(button, function() {
			return post(button.getAttribute("data-options-url")).then(function(options) {
				return navigator.credentials.get({publicKey: requestOptions(options)});
			}).then(function(credential) {
				return post(button.getAttribute("data-login-url"), {
					credential: credentialJSON(credential),
					remember: !!(remember && remember.checked),
					next: button.getAttribute("data-next") || ""
				});
			}).then(function(data) {
				window.location.assign(data.redirect);
			});
		});
	}

	// Confirm a form that costs money with a passkey of the signed-in
	// account. The assertion goes into the hidden field of the form,
	// and the form submits with its own submit button.

	function confirm(button) {
		var form = button.form || button.closest("form");
		var field = form.querySelector("input[name=passkey_assertion]");
		var submit = form.querySelector("input[type=submit], button[type=submit]");
		run(button, function() {
			return post(button.getAttribute("data-options-url")).then(function(options) {
				return navigator.credentials.get({publicKey: requestOptions(options)});
			}).then(function(credential) {
				field.value = JSON.stringify(credentialJSON(credential));
				if (form.requestSubmit) form.requestSubmit(submit);
				else submit.click();
			});
		});
	}

	document.addEventListener("DOMContentLoaded", function() {
		if (!supported()) return;
		document.querySelectorAll("[data-passkey-add], [data-passkey-login], [data-passkey-confirm]").forEach(function(element) {
			element.hidden = false;
		});
		document.querySelectorAll("[data-passkey-unsupported]").forEach(function(element) {
			element.hidden = true;
		});
		document.addEventListener("click", function(event) {
			var button = event.target.closest("[data-passkey-register], [data-passkey-signin], [data-passkey-confirm]");
			if (!button) return;
			event.preventDefault();
			if (button.hasAttribute("data-passkey-register")) register(button);
			else if (button.hasAttribute("data-passkey-signin")) signIn(button);
			else confirm(button);
		});
	});
})();
