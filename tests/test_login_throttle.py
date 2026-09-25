"""Test the throttle of failed sign-ins and password-reset requests (#263)."""

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
