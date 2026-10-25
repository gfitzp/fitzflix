"""Test the per-test sweeps and the speed settings of the suite (#360).

The shared directories live for the whole session. A test that leaves
a file there changed the result of a later test. The lock manager and
the password hash are set for speed. A test that needs the production
values sets its own."""

import os


def test_the_sweep_empties_the_shared_directories(app):
    from tests.conftest import clear_artifacts

    log_file = app.config["LOG_FILE"]
    planted = []
    for key in ("MOVIE_LIBRARY", "TV_LIBRARY", "HELD_DIR", "CUSTOM_ARTWORK_DIR"):
        nested = os.path.join(app.config[key], "Left Behind (2001)")
        os.makedirs(nested, exist_ok=True)
        path = os.path.join(nested, "leftover.mkv")
        with open(path, "w") as f:
            f.write("x")
        planted.append(path)
    archive = f"{log_file}.2001-01-01.gz"
    with open(archive, "w") as f:
        f.write("x")
    planted.append(archive)

    clear_artifacts(app)

    assert [path for path in planted if os.path.exists(path)] == []
    for key in ("MOVIE_LIBRARY", "TV_LIBRARY", "HELD_DIR", "CUSTOM_ARTWORK_DIR"):
        assert os.listdir(app.config[key]) == []
    assert os.path.exists(log_file)


def test_the_lock_manager_tries_once_without_a_wait(app):
    assert (app.lock_manager.retry_count, app.lock_manager.retry_delay) == (1, 0)


def test_the_suite_hashes_passwords_with_a_cheap_pbkdf2(app):
    from app.models import User

    with app.app_context():
        user = User.query.first()
        assert user.password_hash.startswith("pbkdf2:sha256:1000$")
        user2 = User(email="hash@example.test")
        user2.set_password("x")
        assert user2.password_hash.startswith("pbkdf2:sha256:1000$")
        assert user2.check_password("x")


def test_the_shared_helpers_live_in_conftest_and_factories():
    """Pin the moves of #360: one copy of each helper, no cross-test imports."""

    import pathlib
    import re

    shared = ("csrf_token_from", "admin_id", "make_person", "make_cast")
    offenders = []
    for path in sorted(pathlib.Path("tests").glob("test_*.py")):
        source = path.read_text()
        for name in shared:
            if re.search(rf"^def {name}\(", source, flags=re.M):
                offenders.append(f"{path.name} defines {name}")
            if re.search(rf"from tests\.test_\w+ import[^\n]*\b{name}\b", source):
                offenders.append(f"{path.name} imports {name} from a test module")
    assert offenders == []
