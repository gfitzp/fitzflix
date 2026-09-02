"""Play films remotely in Infuse on the Apple TV of each user (#192).

Infuse never implemented Plex Companion. It has no :32500 and no
playMedia. Thus, the app.plex_player path cannot target it. Infuse does
have TMDB-keyed deep links (infuse://movie/{tmdb_id}?play, tvOS 8.2.3
and later). The Apple TV opens such a link for Infuse over the
Companion protocol of Apple, through pyatv. If necessary, this wakes
the box and launches Infuse from cold. With the Plex share in Library
Mode, the film plays immediately. In Direct mode, there is no local
library index for the link to resolve against. Then Infuse goes to the
TMDB page of the film, and the viewer picks it from Search. This is an
accepted trade-off. Direct mode keeps the quick profile switch of Plex.
In both modes, Infuse reports the watch back to Plex. Thus, the diary
still sees it.

Companion requires a one-time PIN pairing per device. A pairing session
cannot span web requests. gunicorn runs 6 workers, and begin() and
finish() must occur on the one object that showed the PIN. Thus, the
pairing runs as a user-request queue task. The task holds the session
open in one worker process while the PIN crosses over from the web
form through Redis. The pairing gives a credentials string. Fitzflix
stores that string on the user row. Later connections need only that
string.

The Apple TV silently drops mDNS from off-subnet sources. The server
lives on the DMZ VLAN. Thus, there is no discovery. The user enters the
address, with the Companion port included. 49152 is only the default
knock of pyatv. For example, the living-room box answers on 49153. To
find the port, run `dns-sd -B _companion-link._tcp` and `dns-sd -L` on
a machine on the network of the Apple TV.
"""

import asyncio
import secrets
import time

from flask import current_app

import pyatv
from pyatv import conf, exceptions
from pyatv.const import PairingRequirement, Protocol

from app import db
from app.models import User

# The default Companion knock of pyatv. Fitzflix appends it when the user
# enters a bare address. The real port is per device (see the module
# docstring).

COMPANION_PORT = 49152

CONNECT_TIMEOUT = 20
PIN_WAIT_SECONDS = 120

PAIR_STATE_KEY = "fitzflix:infuse-pair:{user_id}:state"
PAIR_PIN_KEY = "fitzflix:infuse-pair:{user_id}:pin"
PAIR_ATTEMPT_KEY = "fitzflix:infuse-pair:{user_id}:attempt"
PAIR_TTL = 300

# The commercial name that MediaInfo gives a DD+ Atmos track, as stored
# in file_audio_track.codec. This mirrors app.atmos.EAC3_ATMOS_CODEC. It
# does not import it, because app.atmos builds the process app at import
# time. The web routes and the tests that import this module must not
# start that build.

EAC3_ATMOS_CODEC = "Dolby Digital Plus with Dolby Atmos"


def infuse_only_formats(files):
    """Return the formats in these files that play correctly only in Infuse.

    The Plex tvOS app does not pass through E-AC-3 Atmos or Dolby Vision
    Profile 8. Thus, Fitzflix points a user with both apps configured at
    Infuse for a film that carries one of them."""

    reasons = []
    if any((file.dolby_vision_profile or "").startswith("8") for file in files):
        reasons.append("Dolby Vision Profile 8")
    if any(
        track.codec == EAC3_ATMOS_CODEC for file in files for track in file.audiotrack
    ):
        reasons.append("E-AC-3 Atmos")
    return reasons


def _device_config(address, credentials=None):
    """Return a pyatv config for the Apple TV at ip:port.

    This function builds the config by hand because mDNS discovery never
    crosses the DMZ boundary."""

    host, _, port = address.strip().rpartition(":")
    device = conf.AppleTV(host.strip("[]"), "Apple TV")
    device.add_service(
        conf.ManualService(
            "fitzflix-atv",
            Protocol.Companion,
            int(port),
            {},
            credentials=credentials,
            pairing_requirement=PairingRequirement.Mandatory,
        )
    )
    return device


async def _launch(address, credentials, tmdb_id):
    """Connect over Companion and open the TMDB-keyed Infuse link.

    Never use the raw-URL play form. That form bypasses Plex and loses
    the diary entry."""

    loop = asyncio.get_running_loop()
    atv = await pyatv.connect(_device_config(address, credentials), loop)
    try:
        await atv.apps.launch_app(f"infuse://movie/{tmdb_id}?play")
    finally:
        atv.close()


def play_movie(movie, user):
    """Open the movie in Infuse on the Apple TV of the user.

    This function returns (ok, message). Every failure mode gets a
    message that the movie page can show."""

    if not user.infuse_player_configured:
        return False, "Pair your Apple TV for Infuse on your Profile page first."
    if not movie.tmdb_id:
        return False, "Infuse links films by TMDB id, and this one doesn't have one."

    try:
        asyncio.run(
            asyncio.wait_for(
                _launch(
                    user.infuse_player_address,
                    user.infuse_player_credentials,
                    movie.tmdb_id,
                ),
                timeout=CONNECT_TIMEOUT,
            )
        )
    except exceptions.AuthenticationError:
        return False, (
            "Your Apple TV rejected the stored pairing — re-pair it on "
            "your Profile page."
        )
    except Exception:
        current_app.logger.warning(
            "Infuse launch failed for movie %s at %s",
            movie.id,
            user.infuse_player_address,
            exc_info=True,
        )
        return False, ("Couldn't reach your Apple TV — is it awake and on the network?")
    return True, (
        "Opened in Infuse — if it doesn't start on its own, pick the "
        "film from Search on the page that opens."
    )


# --- Pairing: web-side helpers -------------------------------------------
#
# The web process only enqueues the task, passes the PIN across, and reads
# the result. The Companion session itself lives in the worker of
# pair_task.


def _state_key(user_id):
    """Return the Redis key for the pairing state of this user."""

    return PAIR_STATE_KEY.format(user_id=user_id)


def _pin_key(user_id):
    """Return the Redis key for the pairing PIN of this user."""

    return PAIR_PIN_KEY.format(user_id=user_id)


def _attempt_key(user_id):
    """Return the Redis key for the current pairing-attempt token of this user."""

    return PAIR_ATTEMPT_KEY.format(user_id=user_id)


def start_pairing(user_id, address):
    """Start the PIN pairing with the Apple TV at ip:port.

    The TV shows its PIN some seconds after the task starts. If an
    attempt already waits for its PIN, this function returns False and
    does not enqueue. A second begin() makes the Apple TV drop the first
    session. That session then dies with "not connected" (live incident,
    2026-08-26).

    Each attempt carries a token. The task writes state, consumes the
    PIN, or stores credentials only while its token is the current one.
    Thus, a stale task cannot overwrite the result of a newer attempt. A
    task is stale when it comes late off the queue, or when a new attempt
    replaced it after its state expired.
    """

    redis = current_app.redis
    if pairing_pending(user_id):
        return False
    attempt = secrets.token_hex(8)
    redis.set(_attempt_key(user_id), attempt, ex=PAIR_TTL)
    redis.set(_state_key(user_id), "queued", ex=PAIR_TTL)
    redis.delete(_pin_key(user_id))
    current_app.request_queue.enqueue(
        "app.infuse_player.pair_task",
        args=(user_id, address, attempt),
        job_timeout=PIN_WAIT_SECONDS + 120,
        description=f"Pairing Apple TV at {address} for Infuse playback",
    )
    return True


def pairing_pending(user_id):
    """Return True if a pairing waits for its PIN (the Profile PIN form shows)."""

    state = current_app.redis.get(_state_key(user_id))
    return (state or b"").decode() in ("queued", "show-pin")


def submit_pin(user_id, pin):
    """Pass the PIN from the Profile form to the pairing task."""

    current_app.redis.set(_pin_key(user_id), str(pin), ex=PAIR_TTL)


def pairing_outcome(user_id, wait_seconds=20):
    """Return the result of the pairing task after the PIN arrives.

    This function polls for up to wait_seconds. It returns (ok, message).
    It returns (None, message) if the task has not finished. Success
    normally takes 1 or 2 seconds."""

    redis = current_app.redis
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        state = (redis.get(_state_key(user_id)) or b"").decode()
        if state == "ok":
            redis.delete(_state_key(user_id), _pin_key(user_id))
            return True, "Paired — your play buttons can now open films in Infuse."
        if state.startswith("error:"):
            redis.delete(_state_key(user_id), _pin_key(user_id))
            return False, state[len("error:") :]
        if not state:
            return False, "The pairing timed out — try again."
        time.sleep(0.5)
    return None, "Still pairing — give it a moment and reload this page."


# --- Pairing: the queue task ---------------------------------------------


def pair_task(user_id, address, attempt=None):
    """Hold a Companion pairing session open in this worker.

    begin() puts the PIN on the TV screen. The web form puts the PIN
    into Redis. finish() trades the PIN for credentials. Fitzflix stores
    the credentials on the user row."""

    from app import get_app

    app = get_app()
    with app.app_context():
        asyncio.run(_pair(app, user_id, address, attempt))


async def _pair(app, user_id, address, attempt=None):
    """Run the body of pair_task.

    The body is the Companion session, the wait for the PIN in Redis,
    and the credentials write. Every exit leaves a state that the
    Profile page can report. The attempt token fences all of it. A task
    that is no longer the current attempt of the user stops silently.
    It does not consume the PIN of the newer attempt, and it does not
    overwrite its result. A task with attempt=None, from a legacy
    enqueue, always owns the attempt."""

    state_key = _state_key(user_id)
    pin_key = _pin_key(user_id)

    def owns():
        if attempt is None:
            return True
        current = app.redis.get(_attempt_key(user_id))
        return (current or b"").decode() == attempt

    def report(state):
        if owns():
            app.redis.set(state_key, state, ex=PAIR_TTL)

    pairing = None
    try:
        if not owns():
            return
        loop = asyncio.get_running_loop()
        pairing = await pyatv.pair(_device_config(address), Protocol.Companion, loop)
        await pairing.begin()
        report("show-pin")

        for _ in range(PIN_WAIT_SECONDS):
            if not owns():
                return
            pin = app.redis.get(pin_key)
            if pin:
                break
            await asyncio.sleep(1)
        else:
            report("error:No PIN arrived in time — start the pairing again.")
            return

        if not owns():
            return
        pairing.pin(int(pin.decode()))
        await pairing.finish()
        if not pairing.has_paired:
            report("error:The Apple TV refused that PIN — start the pairing again.")
            return

        if not owns():
            return
        user = db.session.get(User, user_id)
        user.infuse_player_address = address
        user.infuse_player_credentials = str(pairing.service.credentials)
        db.session.commit()
        report("ok")
    except exceptions.PairingError:
        app.logger.warning("Infuse pairing with %s failed", address, exc_info=True)
        report(
            "error:The Apple TV didn't accept the pairing — wrong PIN, or "
            "its PIN dialog expired first. Start again and enter the PIN "
            "promptly."
        )
    except Exception:
        app.logger.warning("Infuse pairing with %s failed", address, exc_info=True)
        report(
            "error:Pairing failed — check the address, make sure the "
            "Apple TV is awake, and try again."
        )
    finally:
        if pairing is not None:
            await pairing.close()
