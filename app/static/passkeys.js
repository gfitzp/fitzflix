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

	function say(status, text) {
		if (!status) return;
		status.textContent = text;
		status.hidden = !text;
	}

	// failed is optional. It gets the error and returns the text to show.

	function run(button, steps, failed) {
		var status = statusOf(button);
		say(status, "");
		button.disabled = true;
		steps().catch(function(error) {
			say(status, failed ? failed(error) : message(error));
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

	// A form that costs money has a hidden passkey_assertion field
	// when the account has a passkey. The submit button of the form
	// asks for the passkey. The assertion goes into the hidden field,
	// and the form submits again with the same button. The password
	// field of the form stays hidden until a passkey request fails.
	// A password in a visible field submits with no passkey request.

	var sentButtons = [];

	function showPassword(form, visible) {
		var password = form.querySelector("input[name=password]");
		if (!password) return false;
		password.hidden = !visible;
		var label = password.id ? form.querySelector("label[for='" + password.id + "']") : null;
		if (label) label.hidden = !visible;
		return true;
	}

	function confirmOnSubmit(event) {
		var form = event.target;
		var field = form.querySelector("input[name=passkey_assertion]");
		if (!field || field.value) return;
		var password = form.querySelector("input[name=password]");
		if (password && !password.hidden && password.value) return;
		event.preventDefault();
		var submit = event.submitter || form.querySelector("input[type=submit], button[type=submit]");
		run(submit, function() {
			return post(field.getAttribute("data-options-url")).then(function(options) {
				return navigator.credentials.get({publicKey: requestOptions(options)});
			}).then(function(credential) {
				field.value = JSON.stringify(credentialJSON(credential));
				// A disabled button is not in the form data. Thus, the
				// button is on for the submit and off again after it.
				submit.disabled = false;
				if (form.requestSubmit) form.requestSubmit(submit);
				else submit.click();
				sentButtons.push(submit);
				window.setTimeout(function() { submit.disabled = true; }, 0);
			});
		}, function(error) {
			var text = message(error);
			if (showPassword(form, true)) text += " Try again, or type your password.";
			return text;
		});
	}

	// An assertion is good for 1 post. A page that comes back from
	// the history starts with empty fields and live buttons.

	window.addEventListener("pageshow", function() {
		document.querySelectorAll("input[name=passkey_assertion]").forEach(function(field) {
			field.value = "";
		});
		sentButtons.splice(0).forEach(function(button) { button.disabled = false; });
	});

	document.addEventListener("DOMContentLoaded", function() {
		if (!supported()) return;
		document.querySelectorAll("[data-passkey-add], [data-passkey-login]").forEach(function(element) {
			element.hidden = false;
		});
		document.querySelectorAll("[data-passkey-unsupported]").forEach(function(element) {
			element.hidden = true;
		});
		document.querySelectorAll("input[name=passkey_assertion]").forEach(function(field) {
			if (field.form) showPassword(field.form, false);
		});
		document.addEventListener("submit", confirmOnSubmit);
		document.addEventListener("click", function(event) {
			var button = event.target.closest("[data-passkey-register], [data-passkey-signin]");
			if (!button) return;
			event.preventDefault();
			if (button.hasAttribute("data-passkey-register")) register(button);
			else signIn(button);
		});
	});
})();
