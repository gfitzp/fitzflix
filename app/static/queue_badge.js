// The queue badge of the navigation bar and the progress of the tasks
// that run. This file uses startPoll of live.js.

// The queue badge and the task progress: this polls
// /api/queue_details. It pauses while the tab is hidden (a phone
// in a pocket must not poll forever). It schedules the next poll
// only after the last poll finished. Thus, slow responses cannot
// overlap and arrive out of order

(function() {
	// base.html gives the address of the poll on the script tag.

	var QUEUE_URL = document.currentScript.getAttribute("data-queue-url");
	var POLL_MS = 5000;
	var queueBadge = document.getElementById("queue_count");
	var lastCount = queueBadge ? parseInt(queueBadge.textContent, 10) : NaN;
	var runningSignature = null;

	function flashQueueCount(color) {
		if (!queueBadge) return;
		queueBadge.classList.add("flash-" + color);
		setTimeout(function() {
			queueBadge.classList.remove("flash-" + color);
		}, 1000);
	}

	function setQueueCount(count) {
		if (queueBadge) queueBadge.textContent = count;
		if (!isNaN(lastCount)) {
			if (count < lastCount) flashQueueCount("green");
			else if (count > lastCount) flashQueueCount("red");
		}
		lastCount = count;
	}

	function applyProgress(alert, task) {
		var label = alert.querySelector(".task-label");
		var bar = alert.querySelector(".progress-bar");
		if (task.progress >= 0) {
			label.textContent = task.description + "... " + task.progress + "%";
			bar.className = "progress-bar bg-info";
			bar.style.width = task.progress + "%";
			bar.setAttribute("aria-valuenow", task.progress);
		} else {
			label.textContent = task.description + "...";
			bar.className = "progress-bar progress-bar-striped progress-bar-animated bg-info";
			bar.style.width = "100%";
			bar.setAttribute("aria-valuenow", "100");
		}
	}

	function renderRunning(running) {
		var container = document.getElementById("running-tasks");
		if (!container) return;

		// While the same tasks run, only their progress bars change.
		// The update in place lets the CSS width transition of the
		// bar animate. The bar does not jump

		var signature = running.map(function(task) {
			return task.id + "|" + task.description;
		}).join("\n");
		if (signature === runningSignature) {
			running.forEach(function(task) {
				var alert = document.getElementById(task.id);
				if (alert) applyProgress(alert, task);
			});
			return;
		}

		container.textContent = "";
		running.forEach(function(task) {
			var alert = document.createElement("div");
			alert.className = "alert alert-info mt-3";
			alert.setAttribute("role", "alert");
			alert.id = task.id;

			var label = document.createElement("span");
			label.className = "task-label";
			alert.appendChild(label);

			var progress = document.createElement("div");
			progress.className = "progress mt-1";
			var bar = document.createElement("div");
			bar.className = "progress-bar";
			bar.setAttribute("role", "progressbar");
			bar.setAttribute("aria-valuemin", "0");
			bar.setAttribute("aria-valuemax", "100");
			progress.appendChild(bar);
			alert.appendChild(progress);

			applyProgress(alert, task);
			container.appendChild(alert);
		});
		runningSignature = signature;
	}

	// The poll stops for good when the answer is not queue data
	//. An expired session answers 401 or redirects to the
	// login page. The next page load starts the poll again. A
	// network failure is not a stop. The poll tries again

	startPoll(QUEUE_URL, POLL_MS, function(queue_details) {
		// The login page is not queue data. Never paint it into the
		// badge
		if (!queue_details || typeof queue_details.count !== "number") {
			return false;
		}
		setQueueCount(queue_details.count);
		renderRunning(queue_details.running || []);

		// A page can paint more from the same answer. page_queue.js
		// paints its table, and page_file_activity.js fetches its
		// cards. Each hook gets the answer. The next poll waits
		// for the promise that a hook returns

		var hooks = window.fitzflixQueueHooks || [];
		return Promise.all(hooks.map(function(hook) {
			return hook(queue_details);
		}));
	}, {
		json: true,
		stopOnFailure: true,
		immediate: true,
		fetchOptions: { credentials: "same-origin" }
	});
})();
