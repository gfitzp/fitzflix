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

	// Each planned answer of the passkey routes is JSON. Three answers
	// are not: the sign-in page after a redirect when the session ended,
	// the error page of a proxy during a restart, and an error page of
	// the server. The script read each answer as JSON first, and the
	// person saw the message of the parser of the browser (#755). The
	// text of an ended session is whole: no password helps then. The
	// text of a server fault says "try again" itself (again).

	function post(url, body) {
		function marked(text, mark) {
			var error = new Error(text);
			error[mark] = true;
			return error;
		}
		var NOT_DONE = "The server could not do this step. Try again in a moment.";
		var SESSION_ENDED = "Your session ended. Reload the page and sign in again.";
		return fetch(url, {
			method: "POST",
			credentials: "same-origin",
			headers: {"Content-Type": "application/json", "X-CSRFToken": csrfToken()},
			body: JSON.stringify(body || {})
		}).then(function(response) {
			var type = response.headers.get("Content-Type") || "";
			if (type.indexOf("application/json") === -1) {
				throw response.redirected ? marked(SESSION_ENDED, "whole") : marked(NOT_DONE, "again");
			}
			return response.json().then(function(data) {
				if (!response.ok) throw new Error(data.error || "The request failed.");
				return data;
			});
		}, function() {
			throw marked(NOT_DONE, "again");
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

	// The text of a passkey proof that failed. The form then shows its
	// password field, if the account has a password. An error that is
	// whole says what to do, and the password does not help for it.

	function failedProof(error, form) {
		var text = message(error);
		if (error && error.whole) return text;
		if (showPassword(form, true)) {
			text += error && error.again ? " Or type your password." : " Try again, or type your password.";
		}
		return text;
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

	// Add a passkey to the signed-in account (Profile page). The add
	// form has the identity fields (#675). The server checks the proof
	// before it gives the options. The proof is a passkey confirmation
	// when the form has the hidden field and no typed password.

	function register(form, button) {
		var nameInput = form.querySelector("[data-passkey-name]");
		var field = form.querySelector("input[name=passkey_assertion]");
		var password = form.querySelector("input[name=password]");
		run(button, function() {
			var typed = password && !password.hidden ? password.value : "";
			var proof = field && !typed ? askPasskey(form, field) : Promise.resolve();
			return proof.then(function() {
				return post(button.getAttribute("data-options-url"), {
					password: typed,
					passkey_assertion: field ? field.value : ""
				});
			}).then(function(options) {
				if (field) field.value = "";
				return navigator.credentials.create({publicKey: creationOptions(options)});
			}).then(function(credential) {
				return post(button.getAttribute("data-register-url"), {
					credential: credentialJSON(credential),
					name: nameInput ? nameInput.value : ""
				});
			}).then(function(data) {
				window.location.assign(data.redirect);
			});
		}, function(error) {
			if (field) field.value = "";
			return field ? failedProof(error, form) : message(error);
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

	// A form that costs money or changes a way in to the account has
	// a hidden passkey_assertion field when the account has a passkey. The submit button of the form
	// asks for the passkey. The assertion goes into the hidden field,
	// and the form submits again with the same button. The password
	// field of the form stays hidden until a passkey request fails.
	// A password in a visible field submits with no passkey request.

	var sentButtons = [];

	// The path that the form posts to. The server binds the passkey
	// answer to this path (#676). An empty action is the page itself.

	function formPath(form) {
		return new URL(form.getAttribute("action") || "", window.location.href).pathname;
	}

	function showPassword(form, visible) {
		var password = form.querySelector("input[name=password]");
		if (!password) return false;
		password.hidden = !visible;
		var label = password.id ? form.querySelector("label[for='" + password.id + "']") : null;
		if (label) label.hidden = !visible;
		return true;
	}

	// Ask the authenticator for a confirmation for the form, and put
	// the answer in the hidden field.

	function askPasskey(form, field) {
		return post(field.getAttribute("data-options-url"), {path: formPath(form)}).then(function(options) {
			return navigator.credentials.get({publicKey: requestOptions(options)});
		}).then(function(credential) {
			field.value = JSON.stringify(credentialJSON(credential));
		});
	}

	function confirmOnSubmit(event) {
		var form = event.target;
		var submit = event.submitter || form.querySelector("input[type=submit], button[type=submit]");
		if (form.hasAttribute("data-passkey-add")) {
			event.preventDefault();
			register(form, submit);
			return;
		}
		var field = form.querySelector("input[name=passkey_assertion]");
		if (!field || field.value) return;
		var password = form.querySelector("input[name=password]");
		if (password && !password.hidden && password.value) return;
		event.preventDefault();
		run(submit, function() {
			return askPasskey(form, field).then(function() {
				// A disabled button is not in the form data. Thus, the
				// button is on for the submit and off again after it.
				submit.disabled = false;
				if (form.requestSubmit) form.requestSubmit(submit);
				else submit.click();
				sentButtons.push(submit);
				window.setTimeout(function() { submit.disabled = true; }, 0);
			});
		}, function(error) {
			return failedProof(error, form);
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
			var button = event.target.closest("[data-passkey-signin]");
			if (!button) return;
			event.preventDefault();
			signIn(button);
		});
	});
})();
