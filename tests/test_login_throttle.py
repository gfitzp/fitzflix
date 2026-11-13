"""Test the throttle of failed sign-ins and password-reset requests (#263)."""

import pytest

from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD, page_csrf_token

# The address of the second CloudFront layer. It is the last entry of
# X-Forwarded-For, after the address of the client.

CLOUDFRONT_HOP = "18.68.30.205"


def sign_in(app, email, password, address="198.51.100.7", chain=None):
    """Post the sign-in form from a fresh client. Return the response.

    The address comes in as X-Forwarded-For with the 2 entries that
    CloudFront sends. chain replaces the full header."""

    client = app.test_client()
    token = page_csrf_token(client, "/auth/login")
    return client.post(
        "/auth/login",
        data={"csrf_token": token, "email": email, "password": password},
        headers={"X-Forwarded-For": chain or f"{address}, {CLOUDFRONT_HOP}"},
    )


def signed_in(response):
    return response.status_code == 302 and "/auth/login" not in response.headers.get(
        "Location", ""
    )


def test_account_locks_after_repeated_failures(app):
    """Test that the account locks, and that the lock hides the password check.

    Each failure comes from a different address. The lock per account
    does not depend on the address. During the lock, the correct
    password also gets a 429."""

    from app.auth import throttle

    for attempt in range(throttle.ACCOUNT_FAILURE_LIMIT):
        response = sign_in(app, ADMIN_EMAIL, "wrong", f"198.51.100.{attempt + 1}")
        assert response.status_code == 302

    response = sign_in(app, ADMIN_EMAIL, ADMIN_PASSWORD, "198.51.100.99")
    assert response.status_code == 429
    assert "Too many failed sign-in attempts" in response.get_data(as_text=True)

    # After the lock ends, the correct password works again.
    for key in app.redis.scan_iter(f"{throttle.KEY_PREFIX}lock:account:*"):
        app.redis.delete(key)
    assert signed_in(sign_in(app, ADMIN_EMAIL, ADMIN_PASSWORD, "198.51.100.99"))


def test_unknown_email_locks_like_a_real_account(app):
    """Test that the lock does not tell which accounts exist."""

    from app.auth import throttle

    for _ in range(throttle.ACCOUNT_FAILURE_LIMIT):
        sign_in(app, "nobody@example.test", "wrong")
    response = sign_in(app, "nobody@example.test", "wrong")
    assert response.status_code == 429


def test_address_locks_and_the_lock_grows(app):
    """Test the lock per address.

    1 address that tries many accounts locks. The second lock of the
    same day is 2 times as long as the first."""

    from app.auth import throttle

    address = "203.0.113.9"
    lock_key = f"{throttle.KEY_PREFIX}lock:address:{address}"

    for attempt in range(throttle.ADDRESS_FAILURE_LIMIT):
        sign_in(app, f"user{attempt}@example.test", "wrong", address)
    assert sign_in(app, "fresh@example.test", "wrong", address).status_code == 429

    # Another address is not affected.
    assert sign_in(app, ADMIN_EMAIL, ADMIN_PASSWORD, "203.0.113.10").status_code == 302
    first = app.redis.ttl(lock_key)
    assert 0 < first <= throttle.LOGIN_WINDOW.total_seconds()

    app.redis.delete(lock_key)
    for attempt in range(throttle.ADDRESS_FAILURE_LIMIT):
        sign_in(app, f"again{attempt}@example.test", "wrong", address)
    assert app.redis.ttl(lock_key) > throttle.LOGIN_WINDOW.total_seconds()


def test_forged_forwarding_entry_is_ignored(app):
    """Test that a client cannot pick its address through CloudFront.

    CloudFront puts a value that the client sends before the real
    address. The throttle counts the real address."""

    from app.auth import throttle

    sign_in(
        app,
        "someone@example.test",
        "wrong",
        chain=f"192.0.2.66, 203.0.113.50, {CLOUDFRONT_HOP}",
    )
    assert app.redis.get(f"{throttle.KEY_PREFIX}fail:address:203.0.113.50") == b"1"
    assert app.redis.get(f"{throttle.KEY_PREFIX}fail:address:192.0.2.66") is None
    assert app.redis.get(f"{throttle.KEY_PREFIX}fail:address:{CLOUDFRONT_HOP}") is None


def test_short_forwarding_chain_skips_only_the_address_lock(app):
    """Test a request that came through fewer hops than the config expects.

    Its address would be a CloudFront server. Thus, the lock per address
    does not count it. The lock per account still applies."""

    from app.auth import throttle

    for _ in range(throttle.ACCOUNT_FAILURE_LIMIT):
        sign_in(app, "short@example.test", "wrong", chain=CLOUDFRONT_HOP)
    assert not list(app.redis.scan_iter(f"{throttle.KEY_PREFIX}fail:address:*"))
    response = sign_in(app, "short@example.test", "wrong", chain=CLOUDFRONT_HOP)
    assert response.status_code == 429


def test_success_clears_the_failure_counts(app):
    from app.auth import throttle

    for _ in range(throttle.ACCOUNT_FAILURE_LIMIT - 1):
        sign_in(app, ADMIN_EMAIL, "wrong")
    assert signed_in(sign_in(app, ADMIN_EMAIL, ADMIN_PASSWORD))

    # The count of the account starts again from 0. 1 more failure does
    # not lock.
    sign_in(app, ADMIN_EMAIL, "wrong")
    assert signed_in(sign_in(app, ADMIN_EMAIL, ADMIN_PASSWORD))

    # The count of the address stays. A valid sign-in does not reset it.
    assert app.redis.get(f"{throttle.KEY_PREFIX}fail:address:198.51.100.7") == b"5"


def test_a_space_around_the_address_signs_in(app):
    """The sign-in form strips the typed address before the lookup (#764).

    MariaDB ignores a space at the end of the address, and SQLite does
    not. The space at the start fails the lookup on both. Thus, only the
    filter of the form lets this sign-in through on both databases."""

    assert signed_in(sign_in(app, f" {ADMIN_EMAIL} ", ADMIN_PASSWORD))


def test_redis_fault_lets_sign_in_through(app, monkeypatch):
    """Test that a Redis fault never locks everybody out."""

    def broken(*args, **kwargs):
        raise ConnectionError("Redis is down")

    monkeypatch.setattr(app.redis, "ttl", broken)
    monkeypatch.setattr(app.redis, "eval", broken)
    assert sign_in(app, ADMIN_EMAIL, "wrong").status_code == 302
    assert signed_in(sign_in(app, ADMIN_EMAIL, ADMIN_PASSWORD))


def test_reset_requests_stop_sending_over_the_limit(app, monkeypatch):
    """Test that the reset form cannot flood an inbox.

    Over the limit, the page shows the same message. No email goes out."""

    import app.auth.routes as routes
    from app.auth import throttle

    # The Email validator refuses the reserved .test domain of the test
    # users. Thus, the lookup is a stub that finds a user for any email.

    class FoundUser:
        class query:
            @staticmethod
            def first():
                return "a user"

            @staticmethod
            def filter_by(**kwargs):
                class Result:
                    @staticmethod
                    def first():
                        return kwargs["email"]

                return Result

    sent = []
    monkeypatch.setattr(routes, "User", FoundUser)
    monkeypatch.setattr(routes, "send_password_reset_email", sent.append)

    for _ in range(throttle.RESET_EMAIL_LIMIT + 2):
        client = app.test_client()
        token = page_csrf_token(client, "/auth/reset-password-request")
        response = client.post(
            "/auth/reset-password-request",
            data={"csrf_token": token, "email": "reader@example.com"},
            follow_redirects=True,
        )
        assert "Check your email" in response.get_data(as_text=True)

    assert sent == ["reader@example.com"] * throttle.RESET_EMAIL_LIMIT


def test_email_variants_share_the_account_lock(app, monkeypatch):
    """Test that every email that reaches 1 account counts against 1 lock.

    MySQL compares emails without accents. Thus, "ÄDMIN@…" signs in to
    the admin account. SQLite does not. Thus, the test stands in for
    the lookup of the database. Before the fix, each variant had its own
    5 guesses."""

    from app.auth import throttle

    variants = ["Ädmin@example.test", "ädmin@example.test", "admín@example.test"]
    variants += ["ADMÍN@example.test", "àdmin@example.test"]
    monkeypatch.setattr(
        throttle,
        "_account_email",
        lambda typed: ADMIN_EMAIL if typed.lower().endswith("@example.test") else None,
    )
    for attempt, variant in enumerate(variants):
        sign_in(app, variant, "wrong", f"198.51.100.{attempt + 1}")

    response = sign_in(app, ADMIN_EMAIL, ADMIN_PASSWORD, "198.51.100.99")
    assert response.status_code == 429


def test_counter_without_expiry_heals(app):
    """Test that a counter left with no expiry gets one on its next addition.

    A fault between 2 separate calls could leave such a key. It would
    then never expire."""

    from app.auth import throttle

    key = f"{throttle.KEY_PREFIX}reset:address:203.0.113.77"
    app.redis.set(key, 7)
    assert app.redis.ttl(key) == -1
    with app.app_context():
        assert throttle._count(key, throttle.RESET_WINDOW) == 8
    assert 0 < app.redis.ttl(key) <= throttle.RESET_WINDOW.total_seconds()


@pytest.mark.parametrize(
    "target,expected",
    [
        ("/movies?page=2", "/movies?page=2"),
        ("/\\evil.example", "/index"),
        ("\\\\evil.example", "/index"),
        ("http:evil.example", "/index"),
        ("//evil.example", "/index"),
        ("https://evil.example/", "/index"),
        ("/\t/evil.example", "/index"),
    ],
)
def test_sign_in_follows_only_a_local_next_path(app, target, expected):
    """A browser reads "\\" as "/". The next path must never leave the site."""

    from urllib.parse import quote

    client = app.test_client()
    token = page_csrf_token(client, "/auth/login")
    response = client.post(
        f"/auth/login?next={quote(target, safe='')}",
        data={"csrf_token": token, "email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 302
    location = response.headers["Location"]
    assert location == expected or location.endswith(f"//localhost{expected}")


def test_logout_needs_a_post_with_the_csrf_token(app, admin_client):
    """Test that a GET or a POST without the token does not end the session (#401)."""

    assert admin_client.get("/auth/logout").status_code == 405
    assert admin_client.post("/auth/logout").status_code == 302
    assert admin_client.get("/profile").status_code == 200

    page = admin_client.get("/profile").get_data(as_text=True)
    assert 'action="/auth/logout"' in page
    token = page_csrf_token(admin_client, "/profile")
    response = admin_client.post("/auth/logout", data={"csrf_token": token})
    assert response.status_code == 302
    assert admin_client.get("/profile").status_code == 302


def test_history_answers_only_a_get(admin_client):
    assert admin_client.get("/history").status_code == 200
    assert admin_client.post("/history").status_code == 405


def test_a_wrong_password_keeps_the_next_path(app):
    """A mistyped password keeps the next path for the second try (#422).

    A next path that leaves the site does not go into the redirect."""

    from urllib.parse import parse_qs, urlparse

    client = app.test_client()
    token = page_csrf_token(client, "/auth/login?next=/history")
    response = client.post(
        "/auth/login?next=/history",
        data={"csrf_token": token, "email": ADMIN_EMAIL, "password": "wrong"},
    )
    assert response.status_code == 302
    location = urlparse(response.headers["Location"])
    assert location.path == "/auth/login"
    assert parse_qs(location.query)["next"] == ["/history"]

    response = client.post(
        "/auth/login?next=//evil.example",
        data={"csrf_token": token, "email": ADMIN_EMAIL, "password": "wrong"},
    )
    assert "evil" not in response.headers["Location"]

    response = client.post(
        "/auth/login?next=/history",
        data={"csrf_token": token, "email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    assert response.headers["Location"].endswith("/history")


def test_a_sign_in_for_an_unknown_email_costs_one_password_hash(app, monkeypatch):
    """An unknown email and an account with no password take the time of a real check."""

    import app.models as models

    calls = []
    real = models.check_password_hash

    def counted(stored, password):
        calls.append(stored)
        return real(stored, password)

    monkeypatch.setattr(models, "check_password_hash", counted)
    response = sign_in(app, "nobody@example.test", "a-guess", "198.51.100.90")
    assert not signed_in(response)
    assert len(calls) == 1


def test_a_sign_in_upgrades_an_older_password_hash(app):
    from werkzeug.security import generate_password_hash

    from app import db
    from app.models import User
    from tests.conftest import MEMBER_EMAIL, MEMBER_PASSWORD

    with app.app_context():
        member = User.query.filter_by(email=MEMBER_EMAIL).one()
        member.password_hash = generate_password_hash(
            MEMBER_PASSWORD, method="pbkdf2:sha256:500"
        )
        db.session.commit()
    try:
        assert signed_in(sign_in(app, MEMBER_EMAIL, MEMBER_PASSWORD, "198.51.100.91"))
        with app.app_context():
            member = User.query.filter_by(email=MEMBER_EMAIL).one()
            assert not member.password_hash.startswith("pbkdf2:sha256:500$")
            assert member.check_password(MEMBER_PASSWORD)
    finally:
        with app.app_context():
            member = User.query.filter_by(email=MEMBER_EMAIL).one()
            member.set_password(MEMBER_PASSWORD)
            db.session.commit()


def _failed_sign_in(app, peer, chain=None):
    """Post a wrong password from a peer address, with a forwarding chain."""

    client = app.test_client()
    token = page_csrf_token(client, "/auth/login")
    return client.post(
        "/auth/login",
        data={"csrf_token": token, "email": "peer@example.test", "password": "wrong"},
        headers={"X-Forwarded-For": chain} if chain else {},
        environ_base={"REMOTE_ADDR": peer},
    )


def test_only_a_trusted_proxy_gives_the_client_address(app, monkeypatch):
    """A direct peer cannot name a different client with a forwarding chain."""

    from app.auth import throttle

    monkeypatch.setitem(app.config, "TRUSTED_PROXIES", frozenset({"192.0.2.80"}))

    # The reverse proxy sends the chain of CloudFront. The client entry counts.
    _failed_sign_in(app, "192.0.2.80", f"203.0.113.50, {CLOUDFRONT_HOP}")
    assert app.redis.get(f"{throttle.KEY_PREFIX}fail:address:203.0.113.50") == b"1"

    # A host of the same network sends a forged chain. Its own address counts.
    _failed_sign_in(app, "192.0.2.99", f"198.51.100.1, {CLOUDFRONT_HOP}")
    assert app.redis.get(f"{throttle.KEY_PREFIX}fail:address:192.0.2.99") == b"1"
    assert app.redis.get(f"{throttle.KEY_PREFIX}fail:address:198.51.100.1") is None

    # A short chain from that host does not skip the lock per address.
    _failed_sign_in(app, "192.0.2.99", CLOUDFRONT_HOP)
    assert app.redis.get(f"{throttle.KEY_PREFIX}fail:address:192.0.2.99") == b"2"


def test_with_no_trusted_proxy_setting_each_peer_is_trusted(app, monkeypatch):
    """The setting is empty by default. Then the chain of each peer counts, as before."""

    from app.auth import throttle

    monkeypatch.setitem(app.config, "TRUSTED_PROXIES", frozenset())
    _failed_sign_in(app, "192.0.2.99", f"203.0.113.60, {CLOUDFRONT_HOP}")
    assert app.redis.get(f"{throttle.KEY_PREFIX}fail:address:203.0.113.60") == b"1"


def test_the_trusted_proxy_setting_reads_a_list_of_addresses(monkeypatch):
    import config

    assert config._address_set(" 192.0.2.80 , 192.0.2.81,, ") == frozenset(
        {"192.0.2.80", "192.0.2.81"}
    )
    assert config._address_set(None) == frozenset()
