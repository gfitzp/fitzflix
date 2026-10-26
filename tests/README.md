# The Fitzflix test suite

Run the suite from the repository root:

```
venv/bin/python -m pytest -q -p no:cacheprovider
```

The suite has approximately 1,800 tests. It takes approximately 50 seconds. The shared pieces are in these files:

- `conftest.py`: the session app, the clean state of each test, the signed-in clients, `held_lock`, `sample_mkv`, `sample_mp4`, `csrf_token_from`, and `page_csrf_token`
- `factories.py`: the records (`make_movie`, `make_movie_file`, `make_tv_series`, `make_tv_file`, `make_person`)
- `fakes.py`: the stand-ins (`Response`, `tmdb_router`, `S3`, `ArchiveS3`, `SQS`, `Job`, `RecordingQueue`, `Sonarr`)
- `media.py`: the sample media (`make_sample_mkv`, `make_sample_mp4`)
- `localization_helpers.py`: the helpers of the 3 localization test files

## The traps

These rules come from real failures. Each one cost a debugging session.

### The singleton of get_app

`app.atmos` calls `get_app()` when it loads. The other task modules (`app.videos`, `app.triage`, and others) get the app later, through a `LocalProxy`. The first app of the process becomes that singleton. A test module that imports `app.atmos` at the top runs that call during the collection. That is before the test app exists. Then each task function of the suite uses the production database, Redis, and S3.

Import the task modules inside the test functions. Give each test the `app` fixture, also a test of a pure function. The `app` fixture asserts that the singleton is the test app. Never weaken that assert.

### The session database and clean_state

The app fixture is session-scoped. All test files share 1 SQLite database. The autouse `clean_state` fixture deletes the rows after each test. It keeps the quality tiers, the feature types, and the 2 users.

The user rows survive from test to test. A test that changes a user column (`plex_username`, `letterboxd_username`, a notification flag) must set the value at its start. It must not trust the default. `reset_user_state` puts only `letterboxd_username` back after each test.

### The configuration

Never assign to `app.config` directly. The app is session-scoped, so the value leaks into each later test. Use `monkeypatch.setitem(app.config, "KEY", value)`. The audit is:

```
grep -rn 'app\.config\[[^]]*\] *=' tests/ | grep -v setitem
```

### Redis

`clean_state` flushes Redis database 9 before each test, not after it. A test that fails leaves its keys for you to examine. A test must not trust that a key is absent at its end. A blank shelf or a blank Redis-backed page can be a key that a fixture left.

### The shared directories and the sweep

The import, staging, rejects, held, artwork, and library directories live for the whole session. After each test, `clean_state` empties the 2 library roots, the held directory, and the artwork directory (`clear_artifacts`). It also removes the files in the rejects directory and the log archives. It does not empty the import and staging directories. A test that uses them cleans them itself. A test must not leave a file that another test can find.

### GROWTH_CHECK_SECONDS

The import waits for a file that still grows. The autouse `no_growth_wait` fixture sets the wait to 0. A test of the growth check sets it again with `monkeypatch`.

### The sample media

Build a Matroska file with `make_sample_mkv` from `media.py`. Give the file a path in the temp directory of the session (`_TMP`). The function builds each path 1 time per run. Do not write a new ffmpeg recipe in a test.

### The retry ids

A deferred task gets a retry id from `app.tasks`. Build the expected id with `requeue_job_id` from `app.tasks`, inside the test. Do not write the id by hand. The `requeue_id` helper of `test_scheduling.py` does this.

### The flash messages

The flash messages and the page text follow ASD-STE100. The tests assert on the exact wording. A change of a message is a change of its tests. Run `venv/bin/python tests/test_ste.py` before a commit.

### The network

The test configuration makes TMDB unreachable. A test that needs TMDB patches the 1 getter, `app.tmdb_apply.tmdb_get`. The fetches of each module go through `tmdb_json` and that getter. Use `tmdb_router` from `fakes.py` for a fake that answers by the end of the URL. The `fake_tmdb` fixture answers each request with no match. The Plex fakes patch `plex_get`, `plex_write`, or `plex_command` in the module that calls them.

### The clients and the forms

`admin_client` is signed in as the admin user. `user_client` is signed in as the member user. A form post needs the CSRF token of the same client. `page_csrf_token(client, path)` reads it from a page. `csrf_token_from(html)` reads it from a page that you have already.
