"""Test the fresh-install nuisances of #501."""

import logging


def test_a_missing_library_folder_is_a_warning_not_an_error(
    app, monkeypatch, tmp_path, caplog
):
    from app import dvr

    library = tmp_path / "library"
    monkeypatch.setitem(app.config, "LIBRARY_DIR", str(library))
    monkeypatch.setitem(app.config, "MOVIE_LIBRARY", str(library / "Movies"))
    monkeypatch.setitem(app.config, "TV_LIBRARY", str(library / "TV Shows"))
    with app.app_context(), caplog.at_level(logging.WARNING):
        assert dvr.build_channel_lineups() is True
    records = [r for r in caplog.records if "DVR:" in r.getMessage()]
    assert records and all(r.levelno == logging.WARNING for r in records)
    assert "do not exist" in records[0].getMessage()


def test_a_dead_share_is_still_an_error(app, monkeypatch, tmp_path, caplog):
    from app import dvr, maintenance

    volumes = tmp_path / "Volumes"
    monkeypatch.setattr(maintenance, "VOLUMES_ROOT", str(volumes))
    monkeypatch.setitem(app.config, "LIBRARY_DIR", str(volumes))
    monkeypatch.setitem(app.config, "MOVIE_LIBRARY", str(volumes / "Movies"))
    monkeypatch.setitem(app.config, "TV_LIBRARY", str(volumes / "TV Shows"))
    with app.app_context(), caplog.at_level(logging.WARNING):
        assert dvr.build_channel_lineups() is True
    assert any(
        r.levelno == logging.ERROR and "offline" in r.getMessage()
        for r in caplog.records
    )


def test_a_manual_layout_expects_only_one_worker_for_each_queue(app, monkeypatch):
    from app import maintenance

    monkeypatch.setattr(maintenance, "_supervisor_status", lambda config: None)
    expected = maintenance.expected_workers(app.config)
    assert set(expected) == set(maintenance.EXPECTED_WORKERS)
    assert set(expected.values()) == {1}

    monkeypatch.setattr(
        maintenance,
        "_supervisor_status",
        lambda config: {"fitzflix:fitzflix-sql_00": ("RUNNING", 1)},
    )
    assert maintenance.expected_workers(app.config) == maintenance.EXPECTED_WORKERS


def test_check_config_skips_the_settings_read_before_the_first_migration(
    app, monkeypatch, caplog
):
    import sqlalchemy

    from app import check_config, settings

    class NoTables:
        def has_table(self, name):
            return False

    def no_read(*args, **kwargs):
        raise AssertionError("read the settings table before it exists")

    monkeypatch.setattr(sqlalchemy, "inspect", lambda engine: NoTables())
    monkeypatch.setattr(settings, "get_setting", no_read)
    monkeypatch.setitem(app.config, "AWS_BUCKET", None)
    with caplog.at_level(logging.WARNING):
        check_config(app)
    assert "Cannot read the page settings" not in caplog.text
