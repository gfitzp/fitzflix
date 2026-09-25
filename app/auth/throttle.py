"""Throttle failed sign-ins and password-reset requests (#263).

The counters live in Redis. There are 2 kinds of sign-in lock:

- A lock per account. It stops a slow guess at 1 password from many
  addresses. It does not depend on the client address. Thus, a forged
  X-Forwarded-For does not get past it. The key is a hash of the
  typed email, so an unknown email locks the same way as a real one.
  The lock does not tell an attacker which accounts exist.
- A lock per client address. It stops 1 address that tries many
  accounts. Each new lock of the same address in 1 day is 2 times as
  long as the one before, up to 1 day. When the address is not known,
  this lock does not apply. Then the lock per account still applies.

A password-reset request counts per address and per email. After the
limit, the page shows the usual message, but no email goes out.

If Redis fails, the throttle lets the request through and logs a
warning. A Redis fault must not lock everybody out.
"""

import hashlib

from datetime import timedelta

from flask import current_app, request

KEY_PREFIX = "fitzflix:auth:"

# Sign-in failures in 1 window. After the limit, the account or the
# address is locked.

LOGIN_WINDOW = timedelta(minutes=15)
ACCOUNT_FAILURE_LIMIT = 5
ADDRESS_FAILURE_LIMIT = 20

# The account lock is short and fixed. Somebody who types the email of
# a real user can lock that user out. A short lock limits that harm.

ACCOUNT_LOCK = timedelta(minutes=15)
ADDRESS_LOCK_MAX = timedelta(days=1)

# Password-reset requests in 1 window.

RESET_WINDOW = timedelta(hours=1)
RESET_ADDRESS_LIMIT = 5
RESET_EMAIL_LIMIT = 3


def client_address():
    """Return the address of the client, or None if it is not known.

    A request through CloudFront carries PROXY_HOPS entries of
    X-Forwarded-For, or more if the client added its own. ProxyFix then
    puts the address that CloudFront saw into remote_addr. A request
    with no such header came directly, for example on the LAN. Its
    remote_addr is the client. A request with fewer entries than
    PROXY_HOPS came through fewer hops than the config expects. Then
    remote_addr is a CloudFront server, not the client. The result is
    None. Thus, the lock per address does not lock the visitors of that
    server together."""

    chain = [
        entry
        for entry in request.headers.get("X-Forwarded-For", "").split(",")
        if entry.strip()
    ]
    if chain and len(chain) < current_app.config["PROXY_HOPS"]:
        return None
    return request.remote_addr


def _email_hash(email):
    """Return a short hash of an email. Redis never holds the email itself."""

    normalized = (email or "").strip().lower()
    return hashlib.sha256(normalized.encode()).hexdigest()[:32]


def _key(*parts):
    """Return the Redis key for these parts."""

    return KEY_PREFIX + ":".join(parts)


def _count(key, window):
    """Add 1 to a counter and return the new value.

    The first addition starts the window. The counter expires at the
    end of the window."""

    redis = current_app.redis
    value = redis.incr(key)
    if value == 1:
        redis.expire(key, int(window.total_seconds()))
    return value


def login_lock_seconds(email):
    """Return the seconds left on a lock of this sign-in, or 0."""

    address = client_address()
    try:
        redis = current_app.redis
        remaining = [redis.ttl(_key("lock", "account", _email_hash(email)))]
        if address is not None:
            remaining.append(redis.ttl(_key("lock", "address", address)))
    except Exception as e:
        current_app.logger.warning(f"Sign-in throttle unavailable: {e}")
        return 0
    return max([0] + [seconds for seconds in remaining if seconds > 0])


def record_login_failure(email):
    """Count a failed sign-in, and lock the account or the address at the limit."""

    address = client_address()
    account = _email_hash(email)

    # An audit line for each failure. The forwarding chain shows the
    # proxy hops, which decide the x_for value of ProxyFix.

    chain = request.headers.get("X-Forwarded-For", "none")
    current_app.logger.info(
        f"Sign-in failed for account {account[:8]} from {address} "
        f"(forwarded for: {chain})"
    )
    try:
        redis = current_app.redis
        failures = _count(_key("fail", "account", account), LOGIN_WINDOW)
        if failures >= ACCOUNT_FAILURE_LIMIT:
            redis.set(
                _key("lock", "account", account),
                1,
                ex=int(ACCOUNT_LOCK.total_seconds()),
            )
            redis.delete(_key("fail", "account", account))
            current_app.logger.warning(
                f"Sign-in: locked account {account[:8]} for "
                f"{ACCOUNT_LOCK} after {failures} failures (last from {address})"
            )

        if address is None:
            return
        failures = _count(_key("fail", "address", address), LOGIN_WINDOW)
        if failures >= ADDRESS_FAILURE_LIMIT:
            strikes = _count(_key("strikes", "address", address), ADDRESS_LOCK_MAX)
            lock = min(LOGIN_WINDOW * 2 ** (strikes - 1), ADDRESS_LOCK_MAX)
            redis.set(
                _key("lock", "address", address),
                1,
                ex=int(lock.total_seconds()),
            )
            redis.delete(_key("fail", "address", address))
            current_app.logger.warning(
                f"Sign-in: locked address {address} for {lock} after "
                f"{failures} failures (lock {strikes} today)"
            )
    except Exception as e:
        current_app.logger.warning(f"Sign-in throttle unavailable: {e}")


def record_login_success(email):
    """Clear the failure count of the account.

    The count of the address stays. Otherwise an attacker with 1 valid
    account could sign in between guesses and never reach the limit of
    the address."""

    try:
        current_app.redis.delete(_key("fail", "account", _email_hash(email)))
    except Exception as e:
        current_app.logger.warning(f"Sign-in throttle unavailable: {e}")


def allow_reset_request(email):
    """Count a password-reset request. Return False when it is over a limit."""

    address = client_address()
    try:
        by_address = 0
        if address is not None:
            by_address = _count(_key("reset", "address", address), RESET_WINDOW)
        by_email = _count(_key("reset", "email", _email_hash(email)), RESET_WINDOW)
    except Exception as e:
        current_app.logger.warning(f"Reset throttle unavailable: {e}")
        return True
    if by_address > RESET_ADDRESS_LIMIT or by_email > RESET_EMAIL_LIMIT:
        current_app.logger.warning(
            f"Password reset: request from {address} over the limit, no email sent"
        )
        return False
    return True
