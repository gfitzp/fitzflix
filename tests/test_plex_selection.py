"""Test the sync of the Plex stream selection to the default tracks of the file (#652).

Plex picks the streams that it plays from its own selection per account.
Fitzflix makes that selection match the file: the default audio track,
and the default subtitle track or none."""

from datetime import timedelta


def _stream(kind, stream_id, index, default=False, selected=False, channels=2):
    stream = {"streamType": kind, "id": stream_id, "index": index, "channels": channels}
    if default:
        stream["default"] = True
    if selected:
        stream["selected"] = True
    return stream


def _part(part_id, streams):
    return {"id": part_id, "Stream": streams}


def _item(rating_key, part, kind="movie"):
    return {"ratingKey": rating_key, "type": kind, "Media": [{"Part": [part]}]}


class FakePlex:
    """A Plex with sections, items per account, and a record of the writes.

    `items` maps a token (None is the owner) to {rating key: item}. The
    `selected` flags of an item are the flags of that account."""

    def __init__(self, items, sections=None, leaves=None):
        self.items = items
        self.sections = sections or []
        self.leaves = leaves or {}
        self.writes = []
        self.reads = []

    def get(self, path, params=None, token=None):
        if path == "/library/sections":
            return {
                "MediaContainer": {
                    "Directory": [
                        {"key": key, "type": kind, "title": title}
                        for key, kind, title in self.sections
                    ]
                }
            }
        parts = path.split("/")
        if parts[2] == "sections":
            keys = sorted(self.items[None])
            return {
                "MediaContainer": {
                    "totalSize": len(keys),
                    "Metadata": [{"ratingKey": key} for key in keys],
                }
            }
        rating_key = parts[3]
        if path.endswith("/allLeaves"):
            return {
                "MediaContainer": {
                    "Metadata": [{"ratingKey": key} for key in self.leaves[rating_key]]
                }
            }
        self.reads.append((token, rating_key))
        account = self.items.get(token)
        if account is None:
            raise RuntimeError("401 for this token")
        return {"MediaContainer": {"Metadata": [account[rating_key]]}}

    def write(self, method, path, params=None, token=None):
        assert method == "PUT", method
        self.writes.append((token, path.split("/")[3], params))
        return {}


def _wire(app, monkeypatch, fake, home=()):
    import app.plex_selection as plex_selection

    monkeypatch.setitem(app.config, "PLEX_URL", "http://plex.test")
    monkeypatch.setitem(app.config, "PLEX_TOKEN", "owner-token")
    monkeypatch.setattr(plex_selection, "plex_get", fake.get)
    monkeypatch.setattr(plex_selection, "plex_write", fake.write)
    monkeypatch.setattr(plex_selection, "home_user_tokens", lambda: list(home))
    return plex_selection


def test_the_file_asks_for_its_default_audio_and_subtitle():
    import app.plex_selection as plex_selection

    part = _part(
        7,
        [
            _stream(2, 11, 1, default=True, channels=1),
            _stream(2, 12, 2, channels=6),
            _stream(3, 21, 3),
            _stream(3, 22, 4, default=True),
        ],
    )
    assert plex_selection.wanted_streams(part) == (11, 22)

    # No flagged track: the first audio track, and no subtitle.

    part = _part(
        8, [_stream(2, 12, 2, channels=6), _stream(2, 11, 1), _stream(3, 21, 3)]
    )
    assert plex_selection.wanted_streams(part) == (11, 0)

    # No audio at all: nothing to select.

    assert plex_selection.wanted_streams(_part(9, [_stream(3, 21, 1)])) == (None, 0)


def test_align_writes_only_when_the_selection_differs(app, monkeypatch):
    fake = FakePlex({})
    plex_selection = _wire(app, monkeypatch, fake)

    # Plex selected the 5.1 remix. The file flags the mono track.

    stale = _part(
        7,
        [
            _stream(2, 11, 1, default=True, channels=1),
            _stream(2, 12, 2, selected=True, channels=6),
            _stream(3, 21, 3, selected=True),
        ],
    )
    with app.app_context():
        assert plex_selection.align_part(stale, token="home-token") is True
    assert fake.writes == [
        ("home-token", "7", {"audioStreamID": 11, "subtitleStreamID": 0, "allParts": 1})
    ]

    # The selection matches the file: no write.

    fresh = _part(
        8,
        [
            _stream(2, 11, 1, default=True, selected=True, channels=1),
            _stream(2, 12, 2, channels=6),
        ],
    )
    with app.app_context():
        assert plex_selection.align_part(fresh) is False
    assert len(fake.writes) == 1


def test_items_are_aligned_for_the_owner_and_each_home_user(app, monkeypatch):
    """A show expands to its episodes. Each account gets its own read and write."""

    mono_first = lambda selected: _part(  # noqa: E731
        70,
        [
            _stream(2, 11, 1, default=True, selected=selected == 11, channels=1),
            _stream(2, 12, 2, selected=selected == 12, channels=6),
        ],
    )
    fake = FakePlex(
        {
            None: {
                "300": _item("300", mono_first(11)),
                "401": _item("401", mono_first(12), kind="episode"),
                "400": {"ratingKey": "400", "type": "show"},
            },
            "anne-token": {
                "300": _item("300", mono_first(12)),
                "401": _item("401", mono_first(11), kind="episode"),
            },
        },
        leaves={"400": ["401"]},
    )
    plex_selection = _wire(app, monkeypatch, fake, home=[("Anne", "anne-token")])

    assert plex_selection.align_plex_items(["300", "400"]) is True

    # The owner needed the episode set. Anne needed the movie set.

    expected_writes = [
        (None, "70", {"audioStreamID": 11, "subtitleStreamID": 0, "allParts": 1}),
        (
            "anne-token",
            "70",
            {"audioStreamID": 11, "subtitleStreamID": 0, "allParts": 1},
        ),
    ]
    assert sorted(fake.writes, key=str) == sorted(expected_writes, key=str)
    # The owner reads each key once more to learn its type.

    expected_reads = [
        (None, "300"),
        (None, "400"),
        (None, "300"),
        (None, "401"),
        ("anne-token", "300"),
        ("anne-token", "401"),
    ]
    assert sorted(fake.reads, key=str) == sorted(expected_reads, key=str)


def test_the_sweep_reads_settled_items_for_the_owner_only(app, monkeypatch):
    """An item with one audio stream and no subtitles cannot differ for anyone.

    The sweep reads it for the owner and skips it for each Home user.
    An account that cannot read the library is skipped after its first
    failures."""

    settled = _part(1, [_stream(2, 11, 1, selected=True)])
    choice = _part(
        2,
        [
            _stream(2, 21, 1, default=True, channels=1),
            _stream(2, 22, 2, selected=True, channels=2),
        ],
    )
    fake = FakePlex(
        {
            None: {"1": _item("1", settled), "2": _item("2", choice)},
            "kelly-token": {"1": _item("1", settled), "2": _item("2", choice)},
        },
        sections=[("5", "movie", "Movies"), ("9", "photo", "Photos")],
    )
    plex_selection = _wire(
        app,
        monkeypatch,
        fake,
        home=[("Kelly", "kelly-token"), ("Guest", "no-access-token")],
    )
    monkeypatch.setattr(plex_selection, "NO_ACCESS_FAILURES", 1)

    assert plex_selection.sync_plex_stream_selections() is True

    assert fake.reads == [
        (None, "1"),
        (None, "2"),
        ("kelly-token", "2"),
        ("no-access-token", "2"),
    ]
    assert fake.writes == [
        (None, "2", {"audioStreamID": 21, "subtitleStreamID": 0, "allParts": 1}),
        (
            "kelly-token",
            "2",
            {"audioStreamID": 21, "subtitleStreamID": 0, "allParts": 1},
        ),
    ]


def test_enqueue_align_waits_and_collapses_a_repeat(app, monkeypatch):
    fake = FakePlex({})
    plex_selection = _wire(app, monkeypatch, fake)

    with app.app_context():
        assert plex_selection.enqueue_align("218257") is True
        assert plex_selection.enqueue_align("218257") is False

        registry = app.maintenance_queue.scheduled_job_registry
        job = app.maintenance_queue.fetch_job(
            plex_selection.safe_job_id("plex_select:218257")
        )
        assert job.func_name == "app.plex_selection.align_plex_items"
        assert job.args == (["218257"],)
        assert job.id in registry.get_job_ids()
        due = registry.get_scheduled_time(job.id)
        assert due is not None
        assert (
            timedelta(minutes=1)
            < due - due.__class__.now(due.tzinfo)
            <= timedelta(minutes=plex_selection.WEBHOOK_DELAY_MINUTES)
        )
