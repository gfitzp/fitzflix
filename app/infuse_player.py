"""Remote playback in Infuse on each user's Apple TV (#192).

Infuse never implemented Plex Companion — no :32500, no playMedia — so
the app.plex_player path can't target it. What Infuse does have is
TMDB-keyed deep links (infuse://movie/{tmdb_id}?play, tvOS 8.2.3+),
and the Apple TV will open one on Infuse's behalf over Apple's own
Companion protocol via pyatv, waking the box and launching Infuse from
cold if need be. With the Plex share in Library Mode the film plays
straight away; in Direct mode (no local library index for the link to
resolve against) Infuse lands on the film's TMDB page and the viewer
picks it from Search — accepted trade-off, Direct mode keeps Plex's
quick profile switching. Either way Infuse reports the watch back to
Plex, so the diary still sees it.

Companion requires a one-time PIN pairing per device. A pairing
session can't span web requests — gunicorn runs six workers, and
begin()/finish() must happen on the one object that showed the PIN —
so pairing runs as a user-request queue task that holds the session
open in a single worker process while the PIN crosses over from the
web form through Redis. The credentials string that pairing yields is
stored on the user row and is all later connections need.

The Apple TV silently drops mDNS from off-subnet sources (the server
lives on the DMZ VLAN), so there is no discovery: the user enters the
address themselves, Companion port included — 49152 is only pyatv's
default knock, and e.g. the living-room box answers on 49153. The
port is found from a machine on the Apple TV's own network with
`dns-sd -B _companion-link._tcp` + `dns-sd -L`.
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

# pyatv's default Companion knock, appended when the user enters a bare
# address; the real port is per-device (see the module docstring)

COMPANION_PORT = 49152

CONNECT_TIMEOUT = 20
PIN_WAIT_SECONDS = 120

PAIR_STATE_KEY = "fitzflix:infuse-pair:{user_id}:state"
PAIR_PIN_KEY = "fitzflix:infuse-pair:{user_id}:pin"
PAIR_ATTEMPT_KEY = "fitzflix:infuse-pair:{user_id}:attempt"
PAIR_TTL = 300

# MediaInfo's commercial name for a DD+ Atmos track, as stored in
# file_audio_track.codec. Mirrors app.atmos.EAC3_ATMOS_CODEC rather
# than importing it — app.atmos builds the process app at import time,
# which web routes and tests importing this module must not trigger

EAC3_ATMOS_CODEC = "Dolby Digital Plus with Dolby Atmos"


def infuse_only_formats(files):
    """The format names among these files that only play correctly in
    Infuse — the Plex tvOS app passes through neither E-AC-3 Atmos nor
    Dolby Vision Profile 8, so a user with both apps configured gets
    pointed at Infuse for films carrying either."""

    reasons = []
    if any((file.dolby_vision_profile or "").startswith("8") for file in files):
        reasons.append("Dolby Vision Profile 8")
    if any(
        track.codec == EAC3_ATMOS_CODEC for file in files for track in file.audiotrack
    ):
        reasons.append("E-AC-3 Atmos")
    return reasons


def _device_config(address, credentials=None):
    """A pyatv config for the Apple TV at ip:port — built by hand
    because mDNS discovery never crosses the DMZ boundary."""

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
    """Connect over Companion and open the TMDB-keyed Infuse link —
    never the raw-URL play form, which would bypass Plex and lose the
    diary entry."""

    loop = asyncio.get_running_loop()
    atv = await pyatv.connect(_device_config(address, credentials), loop)
    try:
        await atv.apps.launch_app(f"infuse://movie/{tmdb_id}?play")
    finally:
        atv.close()


def play_movie(movie, user):
    """Open the movie in Infuse on the user's Apple TV. Returns
    (ok, message); every failure mode gets a message fit to show on
    the movie page."""

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
# The web process only enqueues, hands the PIN across, and reads the
# outcome; the Companion session itself lives in pair_task's worker.


def _state_key(user_id):
    """This user's pairing-state Redis key."""

    return PAIR_STATE_KEY.format(user_id=user_id)


def _pin_key(user_id):
    """This user's pairing-PIN Redis key."""

    return PAIR_PIN_KEY.format(user_id=user_id)


def _attempt_key(user_id):
    """This user's current pairing-attempt token Redis key."""

    return PAIR_ATTEMPT_KEY.format(user_id=user_id)


def start_pairing(user_id, address):
    """Kick off PIN pairing with the Apple TV at ip:port; the TV shows
    its PIN within a few seconds of the task starting. Returns False
    without enqueueing while an attempt is already awaiting its PIN —
    a second begin() makes the Apple TV drop the first session, which
    then dies with "not connected" (live incident, 2026-08-26).

    Each attempt carries a token: the task only writes state, consumes
    the PIN, or stores credentials while its token is still the
    current one, so a stale task (late off the queue, or superseded
    after its state expired) can't clobber a newer attempt's outcome.
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
    """Whether a pairing awaits its PIN (shows the Profile PIN form)."""

    state = current_app.redis.get(_state_key(user_id))
    return (state or b"").decode() in ("queued", "show-pin")


def submit_pin(user_id, pin):
    """Hand the PIN from the Profile form over to the pairing task."""

    current_app.redis.set(_pin_key(user_id), str(pin), ex=PAIR_TTL)


def pairing_outcome(user_id, wait_seconds=20):
    """The pairing task's verdict, polled for up to wait_seconds after
    the PIN lands: (ok, message), or (None, message) when the task
    still hasn't finished — success normally takes a second or two."""

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
    """Hold a Companion pairing session open in this worker: begin()
    puts the PIN on the TV screen, the web form drops it into Redis,
    finish() trades it for credentials stored on the user row."""

    from app import get_app

    app = get_app()
    with app.app_context():
        asyncio.run(_pair(app, user_id, address, attempt))


async def _pair(app, user_id, address, attempt=None):
    """pair_task's body: the Companion session, the Redis PIN wait,
    and the credentials write — every exit leaves a state the Profile
    page can report. All of it is fenced on the attempt token: a task
    that is no longer the user's current attempt bows out silently
    instead of eating the newer attempt's PIN or overwriting its
    outcome (attempt=None, from a legacy enqueue, always owns)."""

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
